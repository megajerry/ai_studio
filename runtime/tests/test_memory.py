"""Memory subsystem tests.

Pure-logic tests (embedding math, cosine, scope predicate) need NO database. The
DB round-trip / scope-isolation / vector-search tests use a live Postgres and
SKIP cleanly (never error, never hang) when none is reachable — a short-timeout
probe decides at collection time. With DATABASE_URL set they MUST run and pass.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.events import read_events
from runtime.memory import (
    GLOBAL_WORKSTREAM,
    MemoryItem,
    MemoryLayer,
    Scope,
    add_lesson,
    cosine,
    dryrun_vector,
    embed,
    in_scope,
    l2_normalize,
    recall,
    recall_lessons,
    remember,
    scope_where,
)
from runtime.memory.api import EVENT_MEMORY_RECALLED, EVENT_MEMORY_REMEMBERED
from runtime.memory.embed import EMBED_DIM
from runtime.migrate import migrate

# ===========================================================================
# Pure logic — no DB
# ===========================================================================


def test_dryrun_embedding_is_deterministic():
    # force_dry_run pins the offline embedder so this pure-logic test never hits
    # the network even if a real embedding key is present in the environment.
    a = embed("the quick brown fox", force_dry_run=True)
    b = embed("the quick brown fox", force_dry_run=True)
    assert a == b
    assert len(a) == EMBED_DIM


def test_dryrun_embedding_is_l2_normalized():
    vec = dryrun_vector("some memorable content here")
    norm = sum(x * x for x in vec) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-9)


def test_dryrun_embedding_similar_text_is_closer():
    base = embed("the cat sat on the mat", force_dry_run=True)
    similar = embed("the cat sat on a mat", force_dry_run=True)
    different = embed("quantum chromodynamics field equations", force_dry_run=True)
    assert cosine(base, similar) > cosine(base, different)


def test_empty_text_embeds_to_zero_vector():
    vec = dryrun_vector("")
    assert vec == [0.0] * EMBED_DIM


def test_l2_normalize_zero_vector_is_safe():
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # Degenerate inputs never raise.
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def _item(layer, ws, project=None, episode=None) -> MemoryItem:
    return MemoryItem(layer=layer, workstream=ws, project=project, episode=episode, text="x")


def test_scope_predicate_episode_isolation():
    scope = Scope(workstream="A", project="p1", episode="e1")
    assert in_scope(_item(MemoryLayer.EPISODE, "A", "p1", "e1"), MemoryLayer.EPISODE, scope)
    # Different episode / project / workstream all excluded.
    assert not in_scope(_item(MemoryLayer.EPISODE, "A", "p1", "e2"), MemoryLayer.EPISODE, scope)
    assert not in_scope(_item(MemoryLayer.EPISODE, "A", "p2", "e1"), MemoryLayer.EPISODE, scope)
    assert not in_scope(_item(MemoryLayer.EPISODE, "B", "p1", "e1"), MemoryLayer.EPISODE, scope)


def test_scope_predicate_layer_does_not_bleed():
    # A project-layer recall must NOT see episode-layer rows in the same project.
    scope = Scope(workstream="A", project="p1")
    assert not in_scope(_item(MemoryLayer.EPISODE, "A", "p1", "e1"), MemoryLayer.PROJECT, scope)
    assert in_scope(_item(MemoryLayer.PROJECT, "A", "p1"), MemoryLayer.PROJECT, scope)


def test_scope_predicate_knowledge_and_global():
    scope = Scope(workstream="A")
    assert in_scope(_item(MemoryLayer.KNOWLEDGE, "A"), MemoryLayer.KNOWLEDGE, scope)
    assert not in_scope(_item(MemoryLayer.KNOWLEDGE, "B"), MemoryLayer.KNOWLEDGE, scope)
    glob = _item(MemoryLayer.KNOWLEDGE, GLOBAL_WORKSTREAM)
    assert not in_scope(glob, MemoryLayer.KNOWLEDGE, scope)
    assert in_scope(glob, MemoryLayer.KNOWLEDGE, scope, include_global_knowledge=True)


def test_scope_predicate_longterm_is_global():
    scope = Scope(workstream="anything")
    assert in_scope(_item(MemoryLayer.LONGTERM, "other"), MemoryLayer.LONGTERM, scope)


def test_scope_where_pins_layer_and_columns():
    where, params = scope_where(MemoryLayer.EPISODE, Scope(workstream="A", project="p", episode="e"))
    assert where.startswith("layer = %s")
    assert params == ["episode", "A", "p", "e"]
    where_lt, params_lt = scope_where(MemoryLayer.LONGTERM, Scope(workstream="A"))
    assert params_lt == ["longterm"]  # global: only the layer is pinned


def test_recall_min_score_excludes_below_floor(monkeypatch):
    # Pure filter test: a fake store returns known scores; a below-floor item is
    # dropped. No DB — the event append is stubbed and embed is forced offline.
    from runtime.memory import api as memory_api

    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.setattr(memory_api, "append_event", lambda *a, **k: None)

    high = MemoryItem(layer=MemoryLayer.KNOWLEDGE, workstream="A", text="relevant")
    low = MemoryItem(layer=MemoryLayer.KNOWLEDGE, workstream="A", text="irrelevant")

    class FakeStore:
        def upsert(self, item):  # pragma: no cover - unused here
            return item

        def search(self, **kwargs):
            return [(high, 0.9), (low, 0.05)]

    scope = Scope(workstream="A")
    # No floor (default) → both returned: behavior-preserving.
    both = recall(None, scope, MemoryLayer.KNOWLEDGE, "q", store=FakeStore())
    assert {i.text for i in both} == {"relevant", "irrelevant"}
    # Floor between the two scores → the below-floor item is excluded.
    filtered = recall(None, scope, MemoryLayer.KNOWLEDGE, "q", min_score=0.2, store=FakeStore())
    assert [i.text for i in filtered] == ["relevant"]


# ===========================================================================
# DB round-trip — live Postgres (skips cleanly when absent)
# ===========================================================================

pytestmark_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0005 (and prior) are applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    # Unique workstream per test → isolation without deleting shared rows.
    return f"memtest-{uuid4().hex[:12]}"


@pytestmark_db
def test_remember_recall_round_trip(conn, ws):
    remember(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "deploy pipeline uses blue-green")
    got = recall(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "how do we deploy", k=5)
    assert len(got) == 1
    assert got[0].text == "deploy pipeline uses blue-green"
    assert got[0].id is not None
    assert got[0].created_at is not None


@pytestmark_db
def test_remember_emits_event_without_text(conn, ws):
    item = remember(
        conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT,
        "a secret-sounding detail", metadata={"k": "v"},
    )
    events = read_events(conn, workstream=ws)
    remembered = [e for e in events if e.type == EVENT_MEMORY_REMEMBERED]
    assert len(remembered) == 1
    payload = remembered[0].payload
    # Counts/ids only — never the text or the embedding.
    assert payload["item_id"] == str(item.id)
    assert payload["dims"] == EMBED_DIM
    assert payload["layer"] == "project"
    assert "a secret-sounding detail" not in str(payload)
    assert "embedding" not in payload


@pytestmark_db
def test_recall_emits_count_only(conn, ws):
    remember(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "one")
    remember(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "two")
    recall(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "query", k=5)
    recalled = [e for e in read_events(conn, workstream=ws) if e.type == EVENT_MEMORY_RECALLED]
    assert recalled[-1].payload["count"] == 2
    assert recalled[-1].payload["k"] == 5


@pytestmark_db
def test_scope_isolation_across_workstreams(conn):
    ws_a = f"memtest-{uuid4().hex[:12]}"
    ws_b = f"memtest-{uuid4().hex[:12]}"
    remember(conn, Scope(workstream=ws_a, project="p"), MemoryLayer.PROJECT, "secret A memory")
    remember(conn, Scope(workstream=ws_b, project="p"), MemoryLayer.PROJECT, "secret B memory")
    # Workstream A recalls its own project memory only — B's is invisible.
    got_a = recall(conn, Scope(workstream=ws_a, project="p"), MemoryLayer.PROJECT, "secret", k=10)
    texts_a = {i.text for i in got_a}
    assert "secret A memory" in texts_a
    assert "secret B memory" not in texts_a


@pytestmark_db
def test_scope_isolation_episode_not_visible_at_project(conn, ws):
    # An episode memory must NOT surface in a broader project-layer recall.
    remember(
        conn, Scope(workstream=ws, project="p1", episode="ep1"),
        MemoryLayer.EPISODE, "episode-only detail",
    )
    remember(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "project-level fact")
    got = recall(conn, Scope(workstream=ws, project="p1"), MemoryLayer.PROJECT, "detail", k=10)
    texts = {i.text for i in got}
    assert "project-level fact" in texts
    assert "episode-only detail" not in texts


@pytestmark_db
def test_episode_isolation_across_episodes(conn, ws):
    remember(conn, Scope(workstream=ws, project="p", episode="e1"), MemoryLayer.EPISODE, "from episode one")
    remember(conn, Scope(workstream=ws, project="p", episode="e2"), MemoryLayer.EPISODE, "from episode two")
    got = recall(conn, Scope(workstream=ws, project="p", episode="e1"), MemoryLayer.EPISODE, "from", k=10)
    texts = {i.text for i in got}
    assert texts == {"from episode one"}


@pytestmark_db
def test_brute_force_returns_nearest(conn, ws):
    scope = Scope(workstream=ws, project="p")
    remember(conn, scope, MemoryLayer.PROJECT, "the database migration runner is idempotent")
    remember(conn, scope, MemoryLayer.PROJECT, "the weather in spain is mostly sunny")
    remember(conn, scope, MemoryLayer.PROJECT, "cats are independent animals")
    got = recall(conn, scope, MemoryLayer.PROJECT, "database migration idempotency", k=1)
    assert len(got) == 1
    assert got[0].text == "the database migration runner is idempotent"


@pytestmark_db
def test_lessons_corpus_workstream_and_global(conn):
    ws_a = f"memtest-{uuid4().hex[:12]}"
    ws_b = f"memtest-{uuid4().hex[:12]}"
    add_lesson(conn, ws_a, "always run migrations before tests")
    add_lesson(conn, ws_a, "check the heartbeat threshold", global_lesson=True)
    # ws_b recalls the global lesson but not ws_a's private one.
    got_b = recall_lessons(conn, ws_b, "migrations heartbeat", k=10)
    texts_b = {i.text for i in got_b}
    assert "check the heartbeat threshold" in texts_b
    assert "always run migrations before tests" not in texts_b
    # Excluding global scopes to the workstream only.
    got_b_local = recall_lessons(conn, ws_b, "migrations heartbeat", k=10, include_global=False)
    assert got_b_local == []


@pytestmark_db
def test_longterm_is_global(conn):
    ws_a = f"memtest-{uuid4().hex[:12]}"
    ws_b = f"memtest-{uuid4().hex[:12]}"
    remember(conn, Scope(workstream=ws_a), MemoryLayer.LONGTERM, "the studio is local-first")
    got = recall(conn, Scope(workstream=ws_b), MemoryLayer.LONGTERM, "local first", k=10)
    assert any(i.text == "the studio is local-first" for i in got)


@pytestmark_db
def test_migration_idempotent(conn):
    # 0005 already applied by the fixture; re-running applies nothing new.
    assert "0005_memory.sql" not in migrate(conn)


@pytestmark_db
def test_write_scope_validation(conn, ws):
    with pytest.raises(ValueError):
        remember(conn, Scope(workstream=ws), MemoryLayer.EPISODE, "needs project+episode")
    with pytest.raises(ValueError):
        remember(conn, Scope(workstream=ws), MemoryLayer.PROJECT, "needs project")
