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


# --- ill-formed `expected` is UNVERIFIABLE, never fabrication (ADR-0021 follow-up) ---


def test_verify_malformed_task_expected_is_unverifiable_not_contradiction(conn, ws):
    """A `task` ref whose `expected` is a db_row-style `status=merged` (not a bare
    status) is UNINTERPRETABLE → UNVERIFIABLE, NOT a contradiction — even though
    the resolved actual status `merged` != the literal string `status=merged`."""
    t = _merged_task(conn, ws)  # actually merged
    claim = Claim(statement="the task is merged",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="status=merged")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    ref = verdict.refs[0]
    assert ref.malformed is True
    assert ref.contradicted is False and ref.resolved is False


def test_verify_garbage_task_expected_is_unverifiable(conn, ws):
    t = _merged_task(conn, ws)
    claim = Claim(statement="the task is done",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="totally-bogus")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert verdict.refs[0].malformed is True and not verdict.refs[0].contradicted


def test_verify_wellformed_task_contradiction_still_rejected(conn, ws):
    """A WELL-FORMED bare status that genuinely differs is STILL a contradiction."""
    t = _merged_task(conn, ws)  # merged; claim asserts a valid-but-wrong status
    claim = Claim(statement="task abandoned",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="abandoned")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "rejected"
    assert verdict.refs[0].contradicted and not verdict.refs[0].malformed


def test_verify_malformed_db_row_expected_is_unverifiable(conn, ws):
    """A `db_row` `expected` with no `col=val` syntax is malformed → unverifiable."""
    t = _merged_task(conn, ws)
    claim = Claim(statement="row exists",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW,
                                        locator=f"tasks:{t.id}", expected="merged")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert verdict.refs[0].malformed is True and not verdict.refs[0].contradicted


def test_verify_unknown_db_row_column_is_unverifiable(conn, ws):
    """An `expected` referencing a column that does not exist is malformed."""
    t = _merged_task(conn, ws)
    claim = Claim(statement="row state",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW,
                                        locator=f"tasks:{t.id}", expected="no_such_col=x")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert verdict.refs[0].malformed is True and not verdict.refs[0].contradicted


def test_verify_wellformed_db_row_contradiction_still_rejected(conn, ws):
    """A well-formed `col=val` on an existing column that differs is a contradiction."""
    t = _merged_task(conn, ws)  # status is merged
    claim = Claim(statement="task is abandoned",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW,
                                        locator=f"tasks:{t.id}", expected="status=abandoned")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "rejected"
    assert verdict.refs[0].contradicted and not verdict.refs[0].malformed


def test_verify_db_row_contradiction_beats_malformed_sibling(conn, ws):
    """Intra-ref precedence (ADR-0021 follow-up): within ONE db_row ref, a genuine
    contradiction on a well-formed field WINS over a malformed/unknown-column
    sibling field — the ref is REJECTED (fabrication), not UNVERIFIABLE. Before
    the fix the resolver short-circuited on `no_such_col` and mis-judged this as
    unverifiable, letting a lie dodge the strike."""
    t = _merged_task(conn, ws)  # truly merged
    claim = Claim(statement="the task was abandoned",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW, locator=f"tasks:{t.id}",
                                        expected="no_such_col=x,status=abandoned")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "rejected"
    assert verdict.refs[0].contradicted and not verdict.refs[0].malformed


def test_verify_db_row_contradiction_beats_malformed_sibling_field_order(conn, ws):
    """Same as above but the malformed field comes AFTER the contradicting one —
    the verdict must be identical (field-order independence)."""
    t = _merged_task(conn, ws)  # truly merged
    claim = Claim(statement="the task was abandoned",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW, locator=f"tasks:{t.id}",
                                        expected="status=abandoned,no_such_col=x")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "rejected"
    assert verdict.refs[0].contradicted and not verdict.refs[0].malformed


def test_verify_db_row_all_malformed_fields_still_unverifiable(conn, ws):
    """A ref whose fields are ALL malformed (no genuine contradiction) is STILL
    UNVERIFIABLE — the honest mis-format fix is preserved, no strike."""
    t = _merged_task(conn, ws)
    claim = Claim(statement="row state",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW, locator=f"tasks:{t.id}",
                                        expected="no_such_col=x,also_bogus=y")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert verdict.refs[0].malformed is True and not verdict.refs[0].contradicted


def test_verify_db_row_all_matching_fields_is_verified(conn, ws):
    """All well-formed fields on existing columns that match → VERIFIED."""
    t = _merged_task(conn, ws)
    claim = Claim(statement="task merged in its workstream",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW, locator=f"tasks:{t.id}",
                                        expected=f"status=merged,workstream={ws}")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "verified"
    assert verdict.refs[0].resolved and not verdict.refs[0].contradicted


def test_verify_malformed_metric_expected_is_unverifiable(conn):
    """A non-numeric `expected` on a count metric is malformed → unverifiable."""
    claim = Claim(statement="there are many tasks",
                  evidence=[EvidenceRef(kind=EvidenceKind.METRIC,
                                        locator="tasks_total", expected="lots")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert verdict.refs[0].malformed is True and not verdict.refs[0].contradicted


def test_verify_malformed_file_expected_is_unverifiable(conn, tmp_path):
    """A non-sha256 `expected` on a `file` is malformed → unverifiable (not silently
    passed, not a contradiction)."""
    p = tmp_path / "artifact.txt"
    p.write_text("hello", "utf-8")
    claim = Claim(statement="the file says hello",
                  evidence=[EvidenceRef(kind=EvidenceKind.FILE,
                                        locator=str(p), expected="hello")])
    verdict = verify_claim(conn, claim)
    assert verdict.status == "unverifiable"
    assert verdict.refs[0].malformed is True and not verdict.refs[0].contradicted


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


def test_malformed_expected_is_withheld_not_a_fabrication(conn, ws, ident):
    """CORE REGRESSION (ADR-0021 follow-up): an honest agent that MIS-FORMATS its
    evidence spec must NOT be branded a fabricator. A `task` ref with a malformed
    `expected` (`status=merged` instead of a bare status) on a task that is
    actually `merged` → UNVERIFIABLE (withheld + proof requested), NO strike, NO
    revocation, relay still allowed. (Before the fix this wrongly REJECTED it as a
    fabrication and permanently revoked the identity.)"""
    assert is_relay_allowed(conn, ident) is True
    t = _merged_task(conn, ws)  # truly merged
    notifier, client = _notifier()
    claim = Claim(statement="the task is merged",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="status=merged")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    # Withheld as unverifiable — NOT sent, NOT a fabrication.
    assert result["relayed"] == []
    assert result.get("fabrication", False) is False
    assert result["claims"][0]["status"] == "unverifiable"
    assert result["claims"][0].get("proof_requested") is True
    assert notifier.pending_count == 0
    assert client.sent == []  # no 🚨 escalation

    cid = result["claims"][0]["claim_id"]
    events = _events_for_claim(conn, cid)
    assert any(e.type == EVENT_COMMS_PROOF_REQUESTED for e in events)
    assert not any(e.type == EVENT_COMMS_FABRICATION_DETECTED for e in events)

    # Identity is UNHARMED: no strike, not revoked, still allowed to relay.
    rec = get_trust(conn, ident)
    assert rec.strikes == 0
    assert rec.trust_state != TRUST_REVOKED and rec.human_relay_allowed is True
    assert is_relay_allowed(conn, ident) is True


def test_malformed_metric_expected_is_withheld_not_a_fabrication(conn, ident):
    """Same principle for a `metric`: a non-numeric `expected` is a mis-formatted
    spec → UNVERIFIABLE + proof requested, never a fabrication/strike."""
    assert is_relay_allowed(conn, ident) is True
    notifier, client = _notifier()
    claim = Claim(statement="there are exactly this many tasks",
                  evidence=[EvidenceRef(kind=EvidenceKind.METRIC,
                                        locator="tasks_total", expected="a bunch")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])
    assert result["relayed"] == [] and result.get("fabrication", False) is False
    assert result["claims"][0]["status"] == "unverifiable"
    assert get_trust(conn, ident).strikes == 0
    assert is_relay_allowed(conn, ident) is True


def test_db_row_bundled_lie_is_rejected_revoked_and_escalated(conn, ws, ident):
    """Loophole closed (ADR-0021 follow-up): a db_row claim that bundles a bogus
    column with a genuine lie (`no_such_col=x,status=abandoned`) on a MERGED task
    is a fabrication — the contradiction on `status` wins over the malformed
    `no_such_col`, so the originator IS struck + revoked (it cannot dodge the
    strike by hiding behind a bogus field)."""
    assert is_relay_allowed(conn, ident) is True
    t = _merged_task(conn, ws)  # truly merged
    notifier, client = _notifier()
    claim = Claim(statement="the task was abandoned and lost",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW, locator=f"tasks:{t.id}",
                                        expected="no_such_col=x,status=abandoned")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["blocked"] is True and result["fabrication"] is True
    assert result["relayed"] == []
    rec = get_trust(conn, ident)
    assert rec.trust_state == TRUST_REVOKED and rec.human_relay_allowed is False
    assert is_relay_allowed(conn, ident) is False
    assert client.sent and "\U0001F6A8" in client.sent[0]
    cid = result["claims"][0]["claim_id"]
    assert any(e.type == EVENT_COMMS_FABRICATION_DETECTED
               for e in _events_for_claim(conn, cid))


def test_db_row_only_malformed_is_withheld_not_a_fabrication(conn, ws, ident):
    """Preserves the honest-mis-format fix: a db_row ref with ONLY a malformed
    field (`no_such_col=x`, no genuine contradiction) → UNVERIFIABLE + proof
    requested, NO strike, NO revocation."""
    assert is_relay_allowed(conn, ident) is True
    t = _merged_task(conn, ws)
    notifier, client = _notifier()
    claim = Claim(statement="row state",
                  evidence=[EvidenceRef(kind=EvidenceKind.DB_ROW, locator=f"tasks:{t.id}",
                                        expected="no_such_col=x")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["relayed"] == [] and result.get("fabrication", False) is False
    assert result["claims"][0]["status"] == "unverifiable"
    assert result["claims"][0].get("proof_requested") is True
    assert client.sent == []  # no escalation
    rec = get_trust(conn, ident)
    assert rec.strikes == 0
    assert rec.trust_state != TRUST_REVOKED and rec.human_relay_allowed is True
    assert is_relay_allowed(conn, ident) is True


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


def test_cascade_does_not_double_strike_originator(conn, ws, ident):
    """Regression (F1): an identity that is BOTH the claim's originator AND the
    task's approver must earn exactly ONE strike + ONE comms.fabrication_detected
    for that claim — never a double-count via the verifier-chain cascade."""
    # The SAME identity approves the task AND later fabricates a claim about it.
    t = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    grab_task(conn, worker_id="w-gate", workstream=ws)
    start_task(conn, t.id, "w-gate")
    transition(conn, t.id, TaskStatus.READY_FOR_REVIEW)
    transition(conn, t.id, TaskStatus.APPROVED, agent_id=ident, agent_type="reviewer")

    assert is_relay_allowed(conn, ident) is True
    notifier, _ = _notifier()
    # Fabricate: the task is 'approved', the claim asserts 'merged' → contradiction.
    claim = Claim(statement="task fully merged and shipped",
                  evidence=[EvidenceRef(kind=EvidenceKind.TASK,
                                        locator=f"task:{t.id}", expected="merged")])
    result = relay_claims(conn, notifier, kind="inform",
                          originating_identity=ident, claims=[claim])

    assert result["fabrication"] is True
    # The originator is NOT in the cascade set (it was already struck directly).
    assert ident not in result["claims"][0]["cascade_struck"]
    # Exactly ONE strike — not two.
    assert get_trust(conn, ident).strikes == 1
    # Exactly ONE comms.fabrication_detected event for this claim.
    cid = result["claims"][0]["claim_id"]
    fab = [e for e in _events_for_claim(conn, cid)
           if e.type == EVENT_COMMS_FABRICATION_DETECTED]
    assert len(fab) == 1


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
