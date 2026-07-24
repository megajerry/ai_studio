"""Live-DB tests for the Spokesman verify-or-refuse grounding gate (ADR-0021, S2).

Exercise the verification engine + relay decision against a real Postgres and SKIP
cleanly when none is reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest spokesman/tests/test_grounding_gate.py

Covered:
- a grounded claim whose evidence resolves+matches → VERIFIED + relayed (comms_claims
  + comms.claim_verified recorded);
- a non-resolvable ref → UNVERIFIABLE, NOT sent, comms.proof_requested emitted;
- a fabricated claim (ref resolves but contradicts expected) → REJECTED, NOT sent,
  record_strike fires → identity revoked, is_relay_allowed now False, 🚨 escalation;
- verifier-chain cascade: the task's approver is also struck;
- a revoked identity is blocked from relaying;
- fail-closed on DB-unreachable: nothing is relayed as fact;
- events stay body-free (no statement text on the wire).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.events import read_events
from runtime.event_types import (
    EVENT_COMMS_CLAIM_VERIFIED,
    EVENT_COMMS_FABRICATION_DETECTED,
    EVENT_COMMS_PROOF_REQUESTED,
)
from runtime.grounding import Claim, EvidenceKind, EvidenceRef
from runtime.migrate import migrate
from runtime.models import TaskStatus
from runtime.tasks import complete_task, enqueue_task, grab_task, start_task, transition
from runtime.trust import (
    COMMS_WORKSTREAM,
    TRUST_REVOKED,
    get_trust,
    is_relay_allowed,
)

from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.classify import Notifier
from spokesman.grounding_gate import relay_claims, verify_claim

from .conftest import API_TOKEN, make_settings

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
    return f"gate-{uuid4().hex[:10]}"


@pytest.fixture
def ident() -> str:
    return f"role/gate-{uuid4().hex[:10]}"


class RecordingClient:
    """Dry-run stand-in WhatsApp client: records sends, never calls out."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        self.sent.append(text)
        return {"dry_run": True, "to": to, "text": text}


def _notifier() -> tuple[Notifier, RecordingClient]:
    client = RecordingClient()
    return Notifier(client), client  # type: ignore[arg-type]


def _merged_task(conn, ws: str):
    """Drive one task all the way to ``merged`` and return it."""
    t = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    grab_task(conn, worker_id="w-gate", workstream=ws)
    start_task(conn, t.id, "w-gate")
    complete_task(conn, t.id, result={"ok": True}, status=TaskStatus.MERGED)
    # Commit so a SEPARATE connection (e.g. the /notify endpoint's) sees the merge.
    conn.commit()
    return t


def _events_for_claim(conn, claim_id) -> list:
    return [e for e in read_events(conn, workstream=COMMS_WORKSTREAM)
            if e.payload.get("claim_id") == str(claim_id)]


# --- verify_claim engine (unit-ish, live DB) --------------------------------


def test_verify_resolves_and_matches(conn, ws):
    t = _merged_task(conn, ws)
    claim = Claim(statement="task merged",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="merged")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "verified"
    assert all(r.resolved and not r.contradicted for r in verdict.refs)


def test_verify_unresolvable_is_unverifiable(conn):
    claim = Claim(statement="event happened",
                  evidence=[EvidenceRef(kind=EvidenceKind.EVENT, locator="seq:999999999")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert not verdict.refs[0].resolved


def test_verify_contradiction_is_rejected(conn, ws):
    t = _merged_task(conn, ws)
    claim = Claim(statement="task abandoned",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="abandoned")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "rejected"
    assert verdict.refs[0].resolved and verdict.refs[0].contradicted


# --- relay decision ---------------------------------------------------------


def test_grounded_claim_is_verified_and_relayed(conn, ws, ident):
    t = _merged_task(conn, ws)
    notifier, client = _notifier()
    claim = Claim(statement="the demo task is merged",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="merged")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["relayed"] == ["the demo task is merged"]
    assert result["claims"][0]["status"] == "verified"
    # 'inform' is batched (ADR-0006): queued, not sent to the wire yet.
    assert notifier.pending_count == 1

    cid = result["claims"][0]["claim_id"]
    verified = [e for e in _events_for_claim(conn, cid)
                if e.type == EVENT_COMMS_CLAIM_VERIFIED]
    assert len(verified) == 1
    assert verified[0].payload["identity"] == ident


def test_unverifiable_claim_requests_proof_and_is_withheld(conn, ident):
    notifier, client = _notifier()
    claim = Claim(statement="a deploy event exists",
                  evidence=[EvidenceRef(kind=EvidenceKind.EVENT, locator="seq:999999999")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["relayed"] == []
    assert result["claims"][0]["status"] == "unverifiable"
    assert notifier.pending_count == 0  # nothing queued/sent
    cid = result["claims"][0]["claim_id"]
    proof = [e for e in _events_for_claim(conn, cid)
             if e.type == EVENT_COMMS_PROOF_REQUESTED]
    assert len(proof) == 1 and proof[0].payload["identity"] == ident
    # A proof request is honest missing-proof, NOT a fabrication → identity intact.
    assert is_relay_allowed(conn, ident) is True


def test_fabricated_claim_is_rejected_revoked_and_escalated(conn, ws, ident):
    assert is_relay_allowed(conn, ident) is True
    t = _merged_task(conn, ws)
    notifier, client = _notifier()
    # The task is merged; the claim asserts it was abandoned → source contradicts it.
    claim = Claim(statement="the task was abandoned and lost",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="abandoned")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["blocked"] is True and result["fabrication"] is True
    assert result["relayed"] == []
    # Zero-tolerance: identity permanently revoked, relay denied thereafter.
    rec = get_trust(conn, ident)
    assert rec.trust_state == TRUST_REVOKED and rec.human_relay_allowed is False
    assert is_relay_allowed(conn, ident) is False
    # 🚨 escalation went out immediately (not the fabricated statement itself).
    assert client.sent and "\U0001F6A8" in client.sent[0]
    assert "the task was abandoned" not in client.sent[0]
    # comms.fabrication_detected emitted for this claim.
    cid = result["claims"][0]["claim_id"]
    assert any(e.type == EVENT_COMMS_FABRICATION_DETECTED
               for e in _events_for_claim(conn, cid))


def test_verifier_chain_cascade_strikes_the_approver(conn, ws, ident):
    # Build a task and drive ready_for_review → approved with a known reviewer id.
    reviewer = f"role/reviewer-{uuid4().hex[:8]}"
    t = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    grab_task(conn, worker_id="w-gate", workstream=ws)
    start_task(conn, t.id, "w-gate")
    transition(conn, t.id, TaskStatus.READY_FOR_REVIEW)
    transition(conn, t.id, TaskStatus.APPROVED, agent_id=reviewer, agent_type="reviewer")

    assert is_relay_allowed(conn, reviewer) is True
    notifier, _ = _notifier()
    # Fabricate: the task is 'approved', the claim asserts 'merged' → contradiction.
    claim = Claim(statement="task fully merged and shipped",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="merged")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["fabrication"] is True
    struck = result["claims"][0]["cascade_struck"]
    assert reviewer in struck
    # The approver who passed the fabricated result is now revoked too.
    assert get_trust(conn, reviewer).trust_state == TRUST_REVOKED
    assert is_relay_allowed(conn, reviewer) is False


def test_revoked_identity_cannot_relay(conn, ws, ident):
    t = _merged_task(conn, ws)
    notifier, client = _notifier()
    # First relay a fabrication to get revoked...
    bad = Claim(statement="it was abandoned",
                evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                      locator=f"task:{t.id}", expected="abandoned")])
    relay_claims(conn, notifier, kind="inform", originating_identity=ident, claims=[bad])
    assert is_relay_allowed(conn, ident) is False

    # ...now even a perfectly grounded claim from the same identity is withheld.
    client2 = RecordingClient()
    notifier2 = Notifier(client2)  # type: ignore[arg-type]
    good = Claim(statement="the task is merged",
                 evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                       locator=f"task:{t.id}", expected="merged")])
    result = relay_claims(conn, notifier2, kind="inform",
                          originating_identity=ident, claims=[good])
    assert result["blocked"] is True and result["relayed"] == []
    assert client2.sent == [] and notifier2.pending_count == 0


def test_labelled_judgment_passes_and_is_labelled(conn, ident):
    notifier, _ = _notifier()
    claim = Claim(statement="we should pause spend next week", is_judgment=True)
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])
    assert result["claims"][0]["status"] == "judgment"
    assert result["relayed"] == ["we should pause spend next week"]


def test_fail_closed_when_db_unreachable(conn, ws, ident):
    """A gate that cannot reach source of truth relays NOTHING as fact."""
    t = _merged_task(conn, ws)
    claim = Claim(statement="the task is merged",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="merged")])
    dead = db.connect()
    dead.close()  # a closed connection stands in for a DB outage
    notifier, client = _notifier()
    result = relay_claims(dead, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])
    assert result["blocked"] is True and result["relayed"] == []
    assert client.sent == [] and notifier.pending_count == 0


# --- /notify endpoint contract ----------------------------------------------


def _client(tmp_path) -> TestClient:
    settings = make_settings(tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(settings, connect=db.connect))


def test_notify_rejects_factual_claim_without_evidence(conn, tmp_path):
    """A non-judgment claim with empty evidence is invalid (Claim validator → 422)."""
    client = _client(tmp_path)
    body = {
        "kind": "inform",
        "originating_identity": "role/x",
        "claims": [{"statement": "everything shipped", "is_judgment": False}],
    }
    resp = client.post("/notify", json=body, headers={"X-Spokesman-Token": API_TOKEN})
    assert resp.status_code == 422


def test_notify_accepts_labelled_judgment(conn, tmp_path):
    client = _client(tmp_path)
    body = {
        "kind": "inform",
        "originating_identity": f"role/j-{uuid4().hex[:8]}",
        "claims": [{"statement": "we should ship on Friday", "is_judgment": True}],
    }
    resp = client.post("/notify", json=body, headers={"X-Spokesman-Token": API_TOKEN})
    assert resp.status_code == 200
    assert resp.json()["relayed"] == ["we should ship on Friday"]


def test_notify_verifies_grounded_claim_end_to_end(conn, ws, tmp_path):
    t = _merged_task(conn, ws)
    client = _client(tmp_path)
    body = {
        "kind": "inform",
        "originating_identity": f"role/e2e-{uuid4().hex[:8]}",
        "claims": [{
            "statement": "the task is merged",
            "is_judgment": False,
            "evidence": [{"kind": "task", "locator": f"task:{t.id}", "expected": "merged"}],
        }],
    }
    resp = client.post("/notify", json=body, headers={"X-Spokesman-Token": API_TOKEN})
    assert resp.status_code == 200
    data = resp.json()
    assert data["relayed"] == ["the task is merged"]
    assert data["claims"][0]["status"] == "verified"


def test_events_stay_body_free(conn, ws, ident):
    """The claim statement text must NEVER reach the append-only event log."""
    secret = f"SECRET_{uuid4().hex}"
    t = _merged_task(conn, ws)
    notifier, _ = _notifier()
    claim = Claim(statement=f"merged {secret}",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="merged")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])
    cid = result["claims"][0]["claim_id"]
    for e in _events_for_claim(conn, cid):
        blob = str(e.payload)
        assert secret not in blob
        for banned in ("statement", "evidence", "reason", "detail"):
            assert banned not in e.payload
