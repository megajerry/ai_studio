"""Commit-visibility of ``events.seq`` under concurrent, out-of-order commits.

``events.seq`` is a ``GENERATED ... AS IDENTITY`` value: drawn at INSERT but only
visible at COMMIT (see ``runtime/migrations/0004_events_seq.sql`` +
``runtime/events.py``). So a lower seq can become visible *after* a higher one has
already been consumed. A plain ``since_seq`` cursor consumer that advanced past the
higher seq then **permanently skips** the lower one — a real defect (a lost
``task.stuck`` → a stuck task never re-decomposed; ADR-0023 R2).

These tests prove, against a real Postgres:

1. BEFORE — a plain ``since_seq`` read (``lookback=0``) permanently loses a seq
   whose transaction commits out-of-order (below an already-advanced cursor).
2. AFTER — a bounded ``lookback`` overlap re-observes and delivers that low seq
   (ordering holds, no duplicates within a read).
3. A full replay (``since_seq=0``) is always complete (no regression), even with a
   real rollback-burned gap in the log.
4. End-to-end: ``dispatch_replans`` loses an out-of-order ``task.stuck`` with the
   plain read but recovers it (idempotently) with the lookback overlap.

Each writer owns its OWN connection (psycopg connections are not thread-safe and we
deliberately hold one transaction open), loops are absent/bounded, and the module
SKIPS cleanly when no ``DATABASE_URL`` is reachable — matching the other DB tests.

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_events_seq_visibility_db.py
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from runtime import db
from runtime.event_types import EVENT_TASK_STUCK
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import build_database_url
from runtime.roles.pm import REPLAN_TASK_TYPE
from runtime.scheduler import dispatch_replans
from runtime.tasks import claim_task, enqueue_task

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)

#: A lookback comfortably larger than the tiny out-of-order burst these tests set up.
LOOKBACK = 50


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure schema exists
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    # Unique workstream per test → isolation without touching shared rows.
    return f"seqvis-{uuid4().hex[:12]}"


# --- helpers ----------------------------------------------------------------


def _fresh(conn):
    """End any open implicit transaction so the next read sees other writers' commits."""
    conn.commit()


def _max_seq(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(seq), 0) AS s FROM events")
        s = int(cur.fetchone()["s"])
    _fresh(conn)
    return s


def _raw_conn() -> psycopg.Connection:
    """A second raw connection (its own transaction) for a held-open writer."""
    return psycopg.connect(build_database_url())


def _insert_stuck(cur, ws: str, *, task_id=None) -> int:
    """Insert a ``task.stuck`` event on ``cur``'s transaction; return its drawn seq.

    Mirrors what ``escalate_stuck_task`` emits (via ``append_event``) but on a
    caller-controlled transaction so we can hold the commit open."""
    cur.execute(
        "INSERT INTO events (task_id, workstream, type, payload) "
        "VALUES (%s, %s, %s, %s) RETURNING seq",
        (task_id, ws, EVENT_TASK_STUCK, Jsonb({"stall_reason": "no_progress"})),
    )
    row = cur.fetchone()
    # Robust to both dict_row (runtime.db.connect) and tuple (raw) row factories.
    return int(row["seq"] if isinstance(row, dict) else row[0])


def _out_of_order_commit(conn, ws: str, *, task_low=None, task_high=None):
    """Set up the hazard: writer A draws a LOW seq and holds it open; writer B draws
    a HIGHER seq and commits. Returns ``(seq_low, seq_high, connA)`` with A STILL
    OPEN (caller must commit + close it)."""
    connA = _raw_conn()
    connA.autocommit = False
    curA = connA.cursor()
    seq_low = _insert_stuck(curA, ws, task_id=task_low)  # UNCOMMITTED (held open)

    connB = _raw_conn()
    connB.autocommit = True
    with connB.cursor() as curB:
        seq_high = _insert_stuck(curB, ws, task_id=task_high)  # COMMITTED
    connB.close()

    assert seq_low < seq_high, "writer A must have drawn the lower seq"
    return seq_low, seq_high, connA


# ---------------------------------------------------------------------------
# 1. BEFORE — plain since_seq (lookback=0) permanently loses the low seq
# ---------------------------------------------------------------------------


def test_plain_since_seq_loses_out_of_order_commit(conn, ws):
    base = _max_seq(conn)
    seq_low, seq_high, connA = _out_of_order_commit(conn, ws)
    try:
        # Consumer reads: only the committed HIGH seq is visible → cursor advances
        # PAST the (still-uncommitted) low seq.
        evs = read_events(conn, workstream=ws, since_seq=base)  # lookback=0 (default)
        seen = [e.seq for e in evs]
        assert seen == [seq_high]
        cursor = max([base, *seen])

        # A commits its LOW seq AFTER the consumer already advanced past it.
        connA.commit()
        _fresh(conn)

        # Next read from the advanced cursor never sees the low seq → LOST.
        evs2 = read_events(conn, workstream=ws, since_seq=cursor)
        seen2 = [e.seq for e in evs2]
        assert seq_low not in seen and seq_low not in seen2, (
            "expected the plain since_seq read to permanently skip the out-of-order "
            "low seq (the confirmed defect)"
        )
    finally:
        connA.close()


# ---------------------------------------------------------------------------
# 2. AFTER — a bounded lookback overlap recovers the low seq
# ---------------------------------------------------------------------------


def test_lookback_recovers_out_of_order_commit(conn, ws):
    base = _max_seq(conn)
    seq_low, seq_high, connA = _out_of_order_commit(conn, ws)
    try:
        # Same first pass: consumer sees the high seq, advances its cursor past the
        # (uncommitted) low seq.
        evs = read_events(conn, workstream=ws, since_seq=base, lookback=LOOKBACK)
        seen = [e.seq for e in evs]
        assert seen == [seq_high]
        cursor = max([base, *seen])

        # A commits its low seq out-of-order.
        connA.commit()
        _fresh(conn)

        # The lookback overlap re-scans behind the cursor → the low seq is DELIVERED.
        evs2 = read_events(conn, workstream=ws, since_seq=cursor, lookback=LOOKBACK)
        seen2 = [e.seq for e in evs2]
        assert seq_low in seen2, "lookback read must recover the out-of-order low seq"

        # Ordering holds (ascending seq) and no duplicates within the read.
        assert seen2 == sorted(seen2)
        assert len(seen2) == len(set(seen2))

        # Across both passes every event was delivered at least once, none lost.
        delivered = set(seen) | set(seen2)
        assert {seq_low, seq_high} <= delivered
    finally:
        connA.close()


# ---------------------------------------------------------------------------
# 3. Full replay (since_seq=0) stays complete — even with a burned gap
# ---------------------------------------------------------------------------


def test_full_replay_since_seq_zero_complete_despite_burned_gap(conn, ws):
    # Two committed events straddling a ROLLBACK-burned seq (identity values are
    # burned on rollback → a legitimate hole in the log).
    with conn.cursor() as cur:
        s1 = _insert_stuck(cur, ws)
    _fresh(conn)

    burn = _raw_conn()
    burn.autocommit = False
    with burn.cursor() as cur:
        burned = _insert_stuck(cur, ws)
    burn.rollback()  # burns `burned` permanently
    burn.close()

    with conn.cursor() as cur:
        s2 = _insert_stuck(cur, ws)
    _fresh(conn)

    assert s1 < burned < s2
    # Full replay reads every COMMITTED event for the workstream, in seq order, and
    # is not tripped up by the hole (no stall, no phantom row).
    seqs = [e.seq for e in read_events(conn, workstream=ws, since_seq=0)]
    assert seqs == [s1, s2]
    assert burned not in seqs
    # since_seq=None behaves identically (whole log).
    seqs_none = [e.seq for e in read_events(conn, workstream=ws)]
    assert seqs_none == [s1, s2]


# ---------------------------------------------------------------------------
# 4. End-to-end: dispatch_replans loses then recovers an out-of-order task.stuck
# ---------------------------------------------------------------------------


def _has_replan(conn, stuck_task_id) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tasks WHERE type = %s AND payload->>'stuck_task_id' = %s LIMIT 1",
            (REPLAN_TASK_TYPE, str(stuck_task_id)),
        )
        found = cur.fetchone() is not None
    _fresh(conn)
    return found


def test_dispatch_replans_recovers_out_of_order_stuck(conn, ws):
    # Two real in-progress tasks that will go stuck.
    t_low = enqueue_task(conn, workstream=ws, type="work.task", payload={"goal": "low"})
    t_high = enqueue_task(conn, workstream=ws, type="work.task", payload={"goal": "high"})
    _fresh(conn)
    claim_task(conn, worker_id="w1", workstream=ws)
    claim_task(conn, worker_id="w1", workstream=ws)
    _fresh(conn)

    base = _max_seq(conn)
    # t_low's task.stuck draws the LOWER seq but is held open; t_high's commits.
    seq_low, seq_high, connA = _out_of_order_commit(
        conn, ws, task_low=t_low.id, task_high=t_high.id
    )
    try:
        # Pass 1 (plain, lookback=0): only t_high's stuck is visible → dispatched;
        # cursor advances past t_low's uncommitted seq.
        cursor, ids = dispatch_replans(conn, since_seq=base, lookback=0)
        _fresh(conn)
        assert _has_replan(conn, t_high.id)
        assert not _has_replan(conn, t_low.id)  # not visible yet
        assert cursor >= seq_high

        # t_low's task.stuck commits out-of-order (below the advanced cursor).
        connA.commit()
        _fresh(conn)

        # Pass 2 (plain, lookback=0): the classic bug — t_low is skipped, never dispatched.
        cursor_plain, ids_plain = dispatch_replans(conn, since_seq=cursor, lookback=0)
        _fresh(conn)
        assert not _has_replan(conn, t_low.id), (
            "plain since_seq dispatch loses the out-of-order task.stuck"
        )

        # Pass 3 (commit-safe overlap): the low task.stuck is re-observed → dispatched.
        cursor_safe, ids_safe = dispatch_replans(conn, since_seq=cursor, lookback=LOOKBACK)
        _fresh(conn)
        assert _has_replan(conn, t_low.id), (
            "lookback dispatch must recover the out-of-order task.stuck"
        )

        # Idempotency: re-running does NOT enqueue a second replan for t_low.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM tasks WHERE type = %s "
                "AND payload->>'stuck_task_id' = %s",
                (REPLAN_TASK_TYPE, str(t_low.id)),
            )
            n_before = int(cur.fetchone()["n"])
        _fresh(conn)
        dispatch_replans(conn, since_seq=cursor, lookback=LOOKBACK)
        _fresh(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM tasks WHERE type = %s "
                "AND payload->>'stuck_task_id' = %s",
                (REPLAN_TASK_TYPE, str(t_low.id)),
            )
            n_after = int(cur.fetchone()["n"])
        _fresh(conn)
        assert n_before == 1 and n_after == 1, "replan dispatch must stay idempotent"
    finally:
        connA.close()
