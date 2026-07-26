"""Live-DB tests for the reasoning-trajectory writer (ADR-0020).

Exercise the single guarded writer (:mod:`runtime.trajectory`) against a real
Postgres and SKIP cleanly when none is reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_trajectory_db.py

Covered: verbatim store→read roundtrip; per-trajectory seq monotonicity + no-gap
under repeated add_step (incl. concurrent appends); event body-free proof (emitted
``trajectory.*`` payloads carry no goal/summary/rationale/outcome text); TTL —
``expire_trajectories`` deletes exactly the expired rows for an injected ``now``;
``compact_to_lean`` yields the ``lean`` tier and preserves choice/confidence/refs/
outcome; and the ``tasks.trajectory_id`` outcome-attribution link. Keyless/dry-run.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from runtime import db
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.tasks import enqueue_task
from runtime.trajectory import (
    EVENT_TRAJECTORY_CLOSED,
    RETENTION_LEAN,
    RETENTION_VERBATIM,
    add_step,
    close_trajectory,
    compact_to_lean,
    expire_trajectories,
    get_trajectory,
    list_steps,
    start_trajectory,
)

# Import the event-type constants from the canonical module for the body-free proof.
from runtime.event_types import (
    EVENT_TRAJECTORY_COMPACTED,
    EVENT_TRAJECTORY_EXPIRED,
    EVENT_TRAJECTORY_STARTED,
    EVENT_TRAJECTORY_STEP_ADDED,
)

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0011 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"test-traj-{uuid4().hex[:10]}"


# --- migration idempotency --------------------------------------------------


def test_migration_is_idempotent(conn):
    migrate(conn)
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.trajectories') AS t")
        assert cur.fetchone()["t"] == "trajectories"
        cur.execute("SELECT to_regclass('public.trajectory_steps') AS t")
        assert cur.fetchone()["t"] == "trajectory_steps"
        cur.execute("SELECT to_regclass('public.trajectory_steps_traj_seq_idx') AS i")
        assert cur.fetchone()["i"] == "trajectory_steps_traj_seq_idx"
    conn.commit()


# --- verbatim store → read roundtrip ----------------------------------------


def test_verbatim_roundtrip(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decide the roadmap", context_size_start=1200)
    traj = get_trajectory(conn, tid)
    assert traj is not None
    assert traj.role == "pm" and traj.workstream == ws
    assert traj.goal == "decide the roadmap"          # body persisted verbatim
    assert traj.status == "open"
    assert traj.retention_tier == RETENTION_VERBATIM
    assert traj.context_size_start == 1200
    assert traj.expires_at is None                    # no ttl → never expires

    rationale = "Weighed A vs B vs C.\nChose B because latency dominates cost here."
    step = add_step(
        conn, tid, "decide", "chose option B",
        rationale=rationale,
        options_considered=["A", "B", "C"],
        choice="B", confidence=0.82,
        refs={"critic_verdict": "proceed", "task_ids": []},
        context_size=1500, tokens=350, cost_usd=0.0012, latency_ms=42,
    )
    assert step.seq == 1

    steps = list_steps(conn, tid)
    assert len(steps) == 1
    s = steps[0]
    assert s.step_type == "decide"
    assert s.summary == "chose option B"
    assert s.rationale == rationale                   # FULL verbatim body preserved
    assert s.options_considered == ["A", "B", "C"]
    assert s.choice == "B"
    assert abs(s.confidence - 0.82) < 1e-6
    assert s.refs == {"critic_verdict": "proceed", "task_ids": []}
    assert s.context_size == 1500 and s.tokens == 350 and s.latency_ms == 42
    assert abs(s.cost_usd - 0.0012) < 1e-9

    # add_step tracked the peak context size on the parent.
    assert get_trajectory(conn, tid).context_size_peak == 1500


def test_close_stamps_ended_and_latency(conn, ws):
    t0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
    tid = start_trajectory(conn, "pm", ws, "plan sprint", now=t0)
    add_step(conn, tid, "plan", "drafted plan", now=t0)
    closed = close_trajectory(
        conn, tid, outcome_summary="shipped",
        now=t0 + timedelta(seconds=5),
    )
    assert closed is not None
    assert closed.status == "closed"
    assert closed.outcome_summary == "shipped"
    assert closed.latency_ms == 5000                  # 5s wall-clock from injected now

    # Closing again is a guarded no-op (already closed → None).
    assert close_trajectory(conn, tid) is None


# --- seq monotonicity + no gaps ---------------------------------------------


def test_seq_monotonic_no_gaps(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "long deliberation")
    for i in range(10):
        st = add_step(conn, tid, "observe", f"observation {i}", rationale=f"detail {i}")
        assert st.seq == i + 1                         # strictly monotonic, 1-based
    seqs = [s.seq for s in list_steps(conn, tid)]
    assert seqs == list(range(1, 11))                  # gapless 1..10


def test_seq_no_race_across_concurrent_connections(conn, ws):
    """Two connections appending to the SAME trajectory must not collide on seq.

    add_step locks the parent row FOR UPDATE, so concurrent appends serialize; the
    UNIQUE(trajectory_id, seq) index backstops it. We prove no gap/dup results.
    """
    tid = start_trajectory(conn, "pm", ws, "concurrent appends")
    c2 = db.connect()
    try:
        for i in range(6):
            add_step(conn, tid, "observe", f"a{i}")
            add_step(c2, tid, "observe", f"b{i}")
    finally:
        c2.close()
    seqs = sorted(s.seq for s in list_steps(conn, tid))
    assert seqs == list(range(1, 13))                  # 12 steps, gapless, no dup


def test_seq_gapless_under_threaded_load(conn, ws):
    """REAL threaded stress: N worker threads each append M steps to ONE trajectory
    over SEPARATE connections, released together by a barrier to maximize contention.

    This exercises the ``FOR UPDATE`` lock on the parent trajectory row in
    :func:`add_step` under genuine parallelism (unlike the sequential-interleaved
    ``test_seq_no_race_...`` above). The lock serializes ``max(seq)+1`` assignment so
    the N*M appends land on a gapless, unique ``seq`` 1..N*M with zero errors and zero
    duplicates. Were the lock removed, concurrent ``max(seq)+1`` reads would collide
    and either raise on the ``UNIQUE(trajectory_id, seq)`` index or drop rows — this
    test would then fail (dupes/gaps or a non-empty error list).
    """
    N, M = 8, 12                                       # 8 threads * 12 appends = 96
    tid = start_trajectory(conn, "pm", ws, "threaded appends")
    conn.commit()                                      # publish the parent to peers

    barrier = threading.Barrier(N)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def worker(wid: int) -> None:
        wconn = db.connect()                           # each thread its OWN connection
        try:
            barrier.wait()                             # all fire at once → real contention
            for j in range(M):
                add_step(wconn, tid, "observe", f"w{wid}-{j}")
        except Exception as exc:                       # capture, never swallow silently
            with errors_lock:
                errors.append(exc)
        finally:
            wconn.close()

    with ThreadPoolExecutor(max_workers=N) as pool:
        list(pool.map(worker, range(N)))

    assert errors == [], f"add_step raised under concurrency: {errors!r}"

    seqs = sorted(s.seq for s in list_steps(conn, tid))
    # Gapless, unique, complete: exactly 1..N*M with no dup and no gap.
    assert len(seqs) == N * M, f"expected {N*M} steps, got {len(seqs)}"
    assert len(set(seqs)) == len(seqs), "duplicate seq assigned under concurrency"
    assert seqs == list(range(1, N * M + 1)), "seq not gapless 1..N*M under concurrency"


# --- event body-free proof --------------------------------------------------


def test_events_are_body_free(conn, ws):
    """The append-only event log must carry NO rationale/summary/goal/outcome text.

    Uses a unique secret token in every body field; asserts it never appears in any
    emitted trajectory.* payload, and that only whitelisted keys travel.
    """
    secret = f"SECRET_BODY_{uuid4().hex}"
    tid = start_trajectory(conn, "pm", ws, f"goal {secret}")
    add_step(
        conn, tid, "decide", f"summary {secret}",
        rationale=f"rationale {secret}",
        options_considered=[f"opt {secret}"],
        choice="B", confidence=0.9, refs={"note": f"ref {secret}"},
    )
    close_trajectory(conn, tid, outcome_summary=f"outcome {secret}")
    compact_to_lean(conn, tid)

    traj_events = [
        e for e in read_events(conn, workstream=ws)
        if e.type.startswith("trajectory.")
    ]
    types_seen = {e.type for e in traj_events}
    assert {EVENT_TRAJECTORY_STARTED, EVENT_TRAJECTORY_STEP_ADDED,
            EVENT_TRAJECTORY_CLOSED, EVENT_TRAJECTORY_COMPACTED} <= types_seen

    # Whitelist of keys allowed on the wire — ids/types/seq/step_type/tier/counts.
    allowed = {
        "trajectory_id", "step_id", "seq", "step_type", "role",
        "retention_tier", "steps_compacted",
    }
    for e in traj_events:
        blob = str(e.payload)
        assert secret not in blob, f"body leaked in {e.type}: {blob}"
        # No body-field key ever travels.
        for banned in ("goal", "summary", "rationale", "outcome_summary",
                       "options_considered", "choice", "confidence", "refs"):
            assert banned not in e.payload, f"{banned} leaked in {e.type}"
        assert set(e.payload) <= allowed, f"unexpected keys in {e.type}: {set(e.payload)}"

    # The step_added event does carry the structural facts (ids/seq/type).
    added = next(e for e in traj_events if e.type == EVENT_TRAJECTORY_STEP_ADDED)
    assert added.payload["trajectory_id"] == str(tid)
    assert added.payload["seq"] == 1 and added.payload["step_type"] == "decide"


# --- TTL expiry -------------------------------------------------------------


def test_expire_selects_exactly_expired_rows(conn, ws):
    base = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)
    # expire_trajectories is a GLOBAL TTL sweep; clear any pre-existing rows already
    # expired at `base` first so this test's count is deterministic across re-runs.
    expire_trajectories(conn, now=base)
    # Expired 1h ago (ttl=1s, started 2h before base): expires_at = base-2h+1s < base.
    expired = start_trajectory(conn, "pm", ws, "old", ttl=1, now=base - timedelta(hours=2))
    # Not-yet-expired: ttl 1h from base → expires_at = base + 1h > base.
    live = start_trajectory(conn, "pm", ws, "fresh", ttl=3600, now=base)
    # No ttl → never expires.
    forever = start_trajectory(conn, "pm", ws, "eternal", now=base)
    add_step(conn, expired, "observe", "will be cascaded away", now=base - timedelta(hours=2))

    n = expire_trajectories(conn, now=base)
    assert n == 1                                      # exactly the one expired row
    assert get_trajectory(conn, expired) is None       # deleted...
    with conn.cursor() as cur:                          # ...and its steps cascaded
        cur.execute("SELECT count(*) AS n FROM trajectory_steps WHERE trajectory_id = %s",
                    (expired,))
        assert cur.fetchone()["n"] == 0
    conn.commit()
    assert get_trajectory(conn, live) is not None
    assert get_trajectory(conn, forever) is not None

    # An expired event was emitted for the deleted trajectory (body-free).
    exp_events = [e for e in read_events(conn, workstream=ws)
                  if e.type == EVENT_TRAJECTORY_EXPIRED]
    assert len(exp_events) == 1
    assert exp_events[0].payload == {"trajectory_id": str(expired)}


# --- verbatim → lean rotation -----------------------------------------------


def test_compact_to_lean_preserves_outcome_fields(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decide")
    long_rationale = ("First line is the gist.\n" + "verbose detail line\n" * 50)
    add_step(
        conn, tid, "decide", "chose B",
        rationale=long_rationale,
        options_considered=["A", "B"], choice="B", confidence=0.77,
        refs={"critic_verdict": "proceed"},
    )
    close_trajectory(conn, tid, outcome_summary="B worked out")

    before = list_steps(conn, tid)[0]
    assert get_trajectory(conn, tid).retention_tier == RETENTION_VERBATIM

    traj = compact_to_lean(conn, tid)
    assert traj is not None and traj.retention_tier == RETENTION_LEAN
    # Outcome-relevant trajectory field preserved.
    assert traj.outcome_summary == "B worked out"

    after = list_steps(conn, tid)[0]
    # rationale distilled (shrunk to first line) ...
    assert after.rationale == "First line is the gist."
    assert len(after.rationale) < len(before.rationale)
    # ... but outcome-relevant fields untouched.
    assert after.choice == "B"
    assert abs(after.confidence - 0.77) < 1e-6
    assert after.refs == {"critic_verdict": "proceed"}
    assert after.options_considered == ["A", "B"]
    assert after.summary == "chose B"


def test_compact_to_lean_custom_distill_fn(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decide")
    add_step(conn, tid, "plan", "s", rationale="keep me short please")
    compact_to_lean(conn, tid, distill_fn=lambda r: "[distilled]" if r else r)
    assert list_steps(conn, tid)[0].rationale == "[distilled]"


# --- outcome-attribution link (tasks.trajectory_id) -------------------------


def test_trajectory_id_links_tasks(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decompose into work")
    t1 = enqueue_task(conn, workstream=ws, type="work.a", payload={})
    t2 = enqueue_task(conn, workstream=ws, type="work.b", payload={})
    # The single guarded task writer doesn't set trajectory_id; the decomposition
    # link is stamped here (a decompose step records the link + the task ids).
    with conn.cursor() as cur:
        cur.execute("UPDATE tasks SET trajectory_id = %s WHERE id = ANY(%s)",
                    (tid, [t1.id, t2.id]))
    conn.commit()
    add_step(conn, tid, "decompose", "spawned 2 work items",
             refs={"task_ids": [str(t1.id), str(t2.id)]})

    # The attribution join: which tasks did this decision create?
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tasks WHERE trajectory_id = %s ORDER BY type", (tid,))
        linked = {r["id"] for r in cur.fetchall()}
    conn.commit()
    assert linked == {t1.id, t2.id}


def test_get_and_list_missing(conn, ws):
    missing = uuid4()
    assert get_trajectory(conn, missing) is None
    assert list_steps(conn, missing) == []
    assert close_trajectory(conn, missing) is None
    assert compact_to_lean(conn, missing) is None
