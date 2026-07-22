"""Lesson-injection tests (the deterministic apply-the-lesson step, ADR-0003).

Pure-logic tests (compose + behavior-preserving guards, injected fake recall) need
NO database. The live-DB tests exercise the real workstream-scoped recall path and
SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from runtime import db
from runtime.memory import add_lesson, recall_lessons
from runtime.migrate import migrate
from runtime.roles.lessons import compose_lessons, inject_lessons


# ===========================================================================
# Pure logic — no DB
# ===========================================================================


def test_compose_lessons_adds_bounded_delimited_section():
    base = "You are the PM."
    out = compose_lessons(base, ["always write the marker", "keep retries bounded"])
    assert base in out
    assert "### Lessons" in out
    assert "- always write the marker" in out
    assert "- keep retries bounded" in out


def test_compose_lessons_is_bounded_by_limit():
    lessons = [f"lesson {i}" for i in range(10)]
    out = compose_lessons("base", lessons, limit=2)
    assert out.count("- lesson") == 2


def test_compose_lessons_no_lessons_is_unchanged():
    assert compose_lessons("base prompt", []) == "base prompt"
    assert compose_lessons("base prompt", ["   ", ""]) == "base prompt"


def test_inject_lessons_no_conn_is_unchanged():
    # Behavior-preserving: no conn → base prompt returned verbatim (no recall).
    assert inject_lessons("base", None, "ws", "query") == "base"


def test_inject_lessons_no_workstream_is_unchanged():
    assert inject_lessons("base", object(), "", "query") == "base"


def test_inject_lessons_uses_recalled_lessons_via_injected_recall():
    def fake_recall(conn, workstream, query, k=5):
        assert workstream == "ws-a"  # scope is passed through
        return [SimpleNamespace(text="write the success marker up front")]

    out = inject_lessons("base", object(), "ws-a", "q", recall=fake_recall)
    assert "### Lessons" in out
    assert "- write the success marker up front" in out


def test_inject_lessons_empty_recall_is_unchanged():
    out = inject_lessons("base", object(), "ws", "q", recall=lambda *a, **k: [])
    assert out == "base"


def test_inject_lessons_recall_error_is_swallowed():
    def boom(*a, **k):
        raise RuntimeError("db down")

    assert inject_lessons("base", object(), "ws", "q", recall=boom) == "base"


# ===========================================================================
# Live DB — real workstream-scoped recall (skips cleanly when absent)
# ===========================================================================

pytestmark_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytestmark_db
def test_inject_lessons_scoped_and_bounded_live(conn):
    ws_a = f"lesson-{uuid4().hex[:12]}"
    ws_b = f"lesson-{uuid4().hex[:12]}"
    # Seed more lessons than the injection cap, all in ws_a.
    for i in range(5):
        add_lesson(conn, ws_a, f"ws-a lesson number {i} about verification markers")
    add_lesson(conn, ws_b, "ws-b private lesson about deployment")

    out = inject_lessons(_base(), conn, ws_a, "verification markers", k=5, limit=3)
    assert "### Lessons" in out
    # Bounded: at most `limit` lessons injected.
    assert out.count("\n- ") == 3
    # Scoped: ws_b's private lesson never leaks into ws_a's prompt.
    assert "deployment" not in out


@pytestmark_db
def test_inject_lessons_private_lesson_never_leaks_across_workstreams(conn):
    # A fresh workstream (no private lessons) never sees another's private lesson,
    # even though the shared global corpus may contribute lessons by design.
    ws_a = f"lesson-{uuid4().hex[:12]}"
    ws_b = f"lesson-{uuid4().hex[:12]}"
    add_lesson(conn, ws_a, "ws-a-only secret about the frobnicator subsystem")
    out_b = inject_lessons(_base(), conn, ws_b, "frobnicator subsystem", k=5)
    assert "frobnicator" not in out_b


def _base() -> str:
    return "You are the studio PM. Define ONE checkable success criterion."
