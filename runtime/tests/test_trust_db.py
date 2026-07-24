"""Live-DB tests for the trust ledger + claims writer (ADR-0021).

Exercise the single guarded writer (:mod:`runtime.trust`) against a real Postgres
and SKIP cleanly when none is reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_trust_db.py

Covered: migration applies (tables/columns/indexes/CHECKs); trust roundtrip +
auto-create; zero-tolerance — one fabrication strike flips trust_state=revoked +
human_relay_allowed=false and is_relay_allowed stays False thereafter; claim
record + verify/reject emit the right body-free events; the body-free sentinel
proof (no statement text ever reaches the event log); and the policy relay
capability + gate reflecting the ledger. Keyless/dry-run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from runtime import db
from runtime.capabilities import Capability
from runtime.events import read_events
from runtime.grounding import Claim, EvidenceKind, EvidenceRef
from runtime.migrate import migrate
from runtime.policy import human_relay_permitted
from runtime.trust import (
    COMMS_WORKSTREAM,
    STRIKE_FABRICATION,
    TRUST_REVOKED,
    TRUST_TRUSTED,
    VERIFICATION_REJECTED,
    VERIFICATION_UNVERIFIABLE,
    VERIFICATION_VERIFIED,
    get_claim,
    get_trust,
    is_relay_allowed,
    record_claim,
    record_claim_from,
    record_strike,
    set_claim_verification,
)
from runtime.event_types import (
    EVENT_COMMS_CLAIM_REJECTED,
    EVENT_COMMS_CLAIM_VERIFIED,
    EVENT_COMMS_FABRICATION_DETECTED,
    EVENT_TRUST_CAPABILITY_REVOKED,
    EVENT_TRUST_STRIKE,
)

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0012 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ident() -> str:
    return f"role/test-{uuid4().hex[:10]}"


# --- migration idempotency + shape ------------------------------------------


def test_migration_is_idempotent_and_shaped(conn):
    migrate(conn)
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.identity_trust') AS t")
        assert cur.fetchone()["t"] == "identity_trust"
        cur.execute("SELECT to_regclass('public.comms_claims') AS t")
        assert cur.fetchone()["t"] == "comms_claims"
        cur.execute("SELECT to_regclass('public.comms_claims_seq') AS s")
        assert cur.fetchone()["s"] == "comms_claims_seq"
        for idx in ("comms_claims_identity_idx", "comms_claims_status_idx",
                    "identity_trust_state_idx"):
            cur.execute("SELECT to_regclass(%s) AS i", (f"public.{idx}",))
            assert cur.fetchone()["i"] == idx
    conn.commit()


# --- trust ledger roundtrip + auto-create -----------------------------------


def test_get_trust_auto_creates_trusted_row(conn, ident):
    rec = get_trust(conn, ident)
    assert rec.identity == ident
    assert rec.trust_state == TRUST_TRUSTED
    assert rec.human_relay_allowed is True
    assert rec.strikes == 0
    assert rec.last_strike_at is None
    # First contact is trusted → relay allowed.
    assert is_relay_allowed(conn, ident) is True


# --- zero-tolerance fabrication penalty -------------------------------------


def test_one_fabrication_strike_permanently_revokes_relay(conn, ident):
    assert is_relay_allowed(conn, ident) is True  # starts allowed

    t0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
    rec = record_strike(conn, ident, kind=STRIKE_FABRICATION, now=t0,
                        detail="claimed a merge that never happened")
    # A SINGLE strike flips everything, permanently.
    assert rec.trust_state == TRUST_REVOKED
    assert rec.human_relay_allowed is False
    assert rec.strikes == 1
    assert rec.last_strike_at == t0

    # ...and stays revoked on read + via the relay gate thereafter.
    again = get_trust(conn, ident)
    assert again.trust_state == TRUST_REVOKED and again.human_relay_allowed is False
    assert is_relay_allowed(conn, ident) is False

    # Idempotent-safe: re-striking accrues another strike, stays revoked.
    rec2 = record_strike(conn, ident, kind=STRIKE_FABRICATION)
    assert rec2.strikes == 2
    assert rec2.trust_state == TRUST_REVOKED and rec2.human_relay_allowed is False
    assert is_relay_allowed(conn, ident) is False


def test_fabrication_strike_emits_escalation_and_revocation_events(conn, ident):
    record_strike(conn, ident, kind=STRIKE_FABRICATION, claim_id=uuid4())
    types = {e.type for e in read_events(conn, workstream=COMMS_WORKSTREAM)}
    assert EVENT_COMMS_FABRICATION_DETECTED in types  # 🚨 escalate
    assert EVENT_TRUST_STRIKE in types
    assert EVENT_TRUST_CAPABILITY_REVOKED in types


def test_non_fabrication_strike_does_not_revoke_relay(conn, ident):
    rec = record_strike(conn, ident, kind="late_delivery")
    assert rec.strikes == 1
    assert rec.trust_state == TRUST_TRUSTED       # relay untouched
    assert rec.human_relay_allowed is True
    assert is_relay_allowed(conn, ident) is True


# --- claim record + verification --------------------------------------------


def test_record_claim_and_verify_persists_and_emits(conn, ident):
    ref = EvidenceRef(kind=EvidenceKind.TASK, locator="task:abc", expected="merged")
    cid = record_claim(conn, originating_identity=ident,
                       statement="task abc merged", evidence=[ref])
    claim = get_claim(conn, cid)
    assert claim is not None
    assert claim.originating_identity == ident
    assert claim.statement == "task abc merged"
    assert claim.verification_status is None          # pending until checked
    assert claim.is_judgment is False
    assert claim.evidence == [{"kind": "task", "locator": "task:abc", "expected": "merged"}]
    assert claim.seq >= 1

    updated = set_claim_verification(conn, cid, VERIFICATION_VERIFIED,
                                     verified_by="spokesman", reason="row present")
    assert updated is not None and updated.verification_status == VERIFICATION_VERIFIED
    assert updated.verified_by == "spokesman"

    verified_events = [e for e in read_events(conn, workstream=COMMS_WORKSTREAM)
                       if e.type == EVENT_COMMS_CLAIM_VERIFIED
                       and e.payload.get("claim_id") == str(cid)]
    assert len(verified_events) == 1
    assert verified_events[0].payload["identity"] == ident
    assert verified_events[0].payload["status"] == VERIFICATION_VERIFIED


def test_record_claim_from_typed_claim(conn, ident):
    claim = Claim(statement="spend is $3", is_judgment=False,
                  evidence=[EvidenceRef(kind=EvidenceKind.METRIC, locator="sum(cost)")])
    cid = record_claim_from(conn, ident, claim, message_ref="msg-1")
    row = get_claim(conn, cid)
    assert row is not None and row.message_ref == "msg-1"
    assert row.evidence == [{"kind": "metric", "locator": "sum(cost)", "expected": None}]


def test_reject_emits_rejected_event(conn, ident):
    cid = record_claim(conn, originating_identity=ident, statement="deploy succeeded",
                       evidence=[EvidenceRef(kind=EvidenceKind.EVENT, locator="seq:9")])
    set_claim_verification(conn, cid, VERIFICATION_REJECTED, verified_by="spokesman",
                           reason="no such event")
    rejected = [e for e in read_events(conn, workstream=COMMS_WORKSTREAM)
                if e.type == EVENT_COMMS_CLAIM_REJECTED
                and e.payload.get("claim_id") == str(cid)]
    assert len(rejected) == 1


def test_unverifiable_persists_without_wire_event(conn, ident):
    cid = record_claim(conn, originating_identity=ident, statement="probably fine",
                       evidence=[EvidenceRef(kind=EvidenceKind.FILE, locator="a.py:1")])
    set_claim_verification(conn, cid, VERIFICATION_UNVERIFIABLE, verified_by="spokesman")
    assert get_claim(conn, cid).verification_status == VERIFICATION_UNVERIFIABLE
    # An honest "couldn't confirm" is NOT a rejection → no verified/rejected event.
    for e in read_events(conn, workstream=COMMS_WORKSTREAM):
        if e.payload.get("claim_id") == str(cid):
            assert e.type not in (EVENT_COMMS_CLAIM_VERIFIED, EVENT_COMMS_CLAIM_REJECTED)


# --- body-free sentinel proof -----------------------------------------------


def test_events_are_body_free(conn, ident):
    """The claim `statement` text must NEVER reach the append-only event log.

    Seed a claim whose statement carries a unique sentinel, verify + reject +
    strike, then assert the sentinel appears in NO emitted event payload.
    """
    secret = f"SECRET_STMT_{uuid4().hex}"
    cid = record_claim(conn, originating_identity=ident, statement=f"claim {secret}",
                       evidence=[EvidenceRef(kind=EvidenceKind.TASK, locator=f"task:{secret}")])
    set_claim_verification(conn, cid, VERIFICATION_VERIFIED, verified_by="spokesman",
                           reason=f"reason {secret}")
    # A fabrication strike referencing the claim, with a sentinel detail (local only).
    record_strike(conn, ident, claim_id=cid, kind=STRIKE_FABRICATION,
                  detail=f"detail {secret}")

    events = [e for e in read_events(conn, workstream=COMMS_WORKSTREAM)
              if e.payload.get("claim_id") == str(cid) or e.payload.get("identity") == ident]
    assert events, "expected some comms/trust events for this identity"
    for e in events:
        blob = str(e.payload)
        assert secret not in blob, f"body/detail leaked in {e.type}: {blob}"
        for banned in ("statement", "evidence", "reason", "detail"):
            assert banned not in e.payload, f"{banned} leaked in {e.type}"

    # The sentinel DOES live in the local DB row (that is where bodies belong).
    row = get_claim(conn, cid)
    assert secret in row.statement


# --- policy relay capability + gate -----------------------------------------


def test_policy_capability_constant_present():
    assert Capability.COMMS_HUMAN_RELAY.value == "comms.human_relay"


def test_relay_gate_reflects_the_ledger(conn, ident):
    # Fresh identity → permitted; after a fabrication strike → denied.
    assert human_relay_permitted(conn, ident) is True
    record_strike(conn, ident, kind=STRIKE_FABRICATION)
    assert human_relay_permitted(conn, ident) is False
    assert human_relay_permitted(conn, ident) == is_relay_allowed(conn, ident)
