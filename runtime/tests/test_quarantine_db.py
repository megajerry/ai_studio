"""Live-DB tests for the task-claim quarantine guard (ADR-0021, S2).

A ``revoked`` / ``quarantined`` identity in the trust ledger is fenced out of the
task queue as well as the human-relay path: its untrusted output must not re-enter
the studio under a new task. The guard is additive + behavior-preserving — an
identity with NO ledger row is trusted-by-default and claims normally. SKIP
cleanly when no DATABASE_URL is reachable.

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_quarantine_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.migrate import migrate
from runtime.models import TaskStatus
from runtime.tasks import enqueue_task, grab_task
from runtime.trust import STRIKE_FABRICATION, record_strike

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


@pytest.fixture
def ws() -> str:
    return f"quar-{uuid4().hex[:10]}"


def test_trusted_identity_grabs_normally(conn, ws):
    """An unknown (ledger-less) worker is trusted-by-default and claims as before."""
    t = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    grabbed = grab_task(conn, worker_id=f"role/fresh-{uuid4().hex[:8]}", workstream=ws)
    assert grabbed is not None and grabbed.id == t.id
    assert grabbed.status is TaskStatus.CLAIMED


def test_revoked_identity_is_blocked_from_grabbing(conn, ws):
    revoked = f"role/bad-{uuid4().hex[:8]}"
    # One fabrication strike → permanently revoked.
    record_strike(conn, revoked, kind=STRIKE_FABRICATION, detail="fabricated a claim")

    t = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    # The revoked identity cannot claim (fail closed) — the task stays up_for_grabs.
    assert grab_task(conn, worker_id=revoked, workstream=ws) is None

    # A trusted worker still grabs the very same task (guard is targeted, not global).
    grabbed = grab_task(conn, worker_id=f"role/ok-{uuid4().hex[:8]}", workstream=ws)
    assert grabbed is not None and grabbed.id == t.id


def test_quarantined_identity_is_blocked_from_grabbing(conn, ws):
    quarantined = f"role/quar-{uuid4().hex[:8]}"
    # Directly set a quarantined ledger row (a non-fabrication strike leaves relay
    # intact but we set the state explicitly to exercise the quarantine branch).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO identity_trust (identity, trust_state) VALUES (%s, 'quarantined') "
            "ON CONFLICT (identity) DO UPDATE SET trust_state = 'quarantined'",
            (quarantined,),
        )
    conn.commit()

    enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    assert grab_task(conn, worker_id=quarantined, workstream=ws) is None
