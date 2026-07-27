"""Live-DB test: ``runtime.demo`` leaves NO residue in its own workstreams (ADR-0028).

The demo seeds ``work.*`` tasks + events + trajectories + budgets + experiments +
memory across ~9 synthetic workstreams. Its ``main()`` finally-block self-cleanup
must delete exactly those (and only those), so ``python -m runtime.demo`` stays a
safe go-live smoke test even against the LIVE studio DB. SKIP cleanly when no DB is
reachable (and the repo-root guard skips this against a non-disposable DB).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    AI_STUDIO_TEST_DB=1 pytest runtime/tests/test_demo_cleanup_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db, demo
from runtime.migrate import migrate
from runtime.tasks import enqueue_task

pytestmark = pytest.mark.skipif(
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


def _rows_in(conn, workstreams: list[str]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'workstream'"
        )
        tables = [r["table_name"] for r in cur.fetchall()]
        total = 0
        for t in tables:
            cur.execute(
                f"SELECT count(*) AS n FROM {t} WHERE workstream = ANY(%s)",
                (workstreams,),
            )
            total += int(cur.fetchone()["n"])
    conn.commit()
    return total


def _all_table_counts(conn) -> dict[str, int]:
    """Row counts for EVERY base table — so residue in a table WITHOUT a
    ``workstream`` column (e.g. ``approvals``, ``task_transitions``,
    ``search_cache``) is caught, not silently ignored."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
        tables = [r["table_name"] for r in cur.fetchall()]
        counts: dict[str, int] = {}
        for t in tables:
            cur.execute(f"SELECT count(*) AS n FROM {t}")
            counts[t] = int(cur.fetchone()["n"])
    conn.commit()
    return counts


def test_cleanup_helper_removes_only_its_own_workstreams(conn):
    """Scoped delete wipes the target workstream and leaves a sibling untouched."""
    mine = f"demo-clean-{uuid4().hex[:8]}"
    other = f"demo-keep-{uuid4().hex[:8]}"
    enqueue_task(conn, workstream=mine, type="work.demo", payload={})
    enqueue_task(conn, workstream=mine, type="work.demo", payload={})
    enqueue_task(conn, workstream=other, type="work.demo", payload={})
    conn.commit()

    assert _rows_in(conn, [mine]) >= 2
    removed = demo._cleanup_workstreams(conn, [mine])

    assert removed >= 2
    assert _rows_in(conn, [mine]) == 0          # its own rows gone
    assert _rows_in(conn, [other]) >= 1         # sibling untouched
    # Cleanup the sibling we created so THIS test also leaves no residue.
    demo._cleanup_workstreams(conn, [other])
    assert _rows_in(conn, [other]) == 0


def test_full_demo_run_leaves_zero_residue_and_exits_zero(conn):
    """End-to-end: run demo.main(); assert exit 0 and ZERO net rows in ANY table.

    Counts ALL base tables before/after (not just workstream-scoped ones), so a leak
    in ``approvals`` / ``task_transitions`` / ``search_cache`` FAILS here. Also asserts
    the ``approvals`` delta specifically = 0 (the reviewer-flagged regression).
    """
    before = _all_table_counts(conn)
    rc = demo.main()
    after = _all_table_counts(conn)
    created = list(demo._created_workstreams)

    assert rc == 0
    assert created, "demo should have recorded its workstreams"
    # Explicit approvals delta = 0 (the specific leak the reviewer caught).
    assert after.get("approvals", 0) - before.get("approvals", 0) == 0, (
        "demo leaked approvals rows"
    )
    # No table gained rows — literally zero net residue in ANY table.
    grew = {t: (before.get(t, 0), after.get(t, 0))
            for t in after if after[t] > before.get(t, 0)}
    assert grew == {}, f"demo left residue in table(s): {grew}"
    # And its own workstream-scoped rows are gone too.
    assert _rows_in(conn, created) == 0, "demo left residue in its own workstreams"
