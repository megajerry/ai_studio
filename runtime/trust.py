"""The single guarded writer for the TRUST LEDGER + human-facing CLAIMS (ADR-0021).

Everything the studio says to the human must be grounded in verifiable evidence,
the Spokesman is ultimately accountable for it, and fabrication (false info
relayed as fact) is the worst offense — it carries a zero-tolerance penalty. This
module is the S1 FOUNDATION: it durably records claims + their evidence + their
verification verdict, and it keeps the trust ledger (who may speak to the human).

Discipline (mirrors :func:`runtime.tasks.transition` + :mod:`runtime.trajectory`):

- **Single guarded writer.** ALL writes to ``identity_trust`` / ``comms_claims`` go
  through this module — there are no ad-hoc INSERT/UPDATEs of these tables
  elsewhere. Each write runs in one transaction so the row write + its body-free
  event are atomic (psycopg nests the event append as a savepoint).
- **Body-free events (invariants 5 & 6).** Every emitted ``comms.*`` / ``trust.*``
  event carries ONLY ids / identity / status / kind / strikes counts — NEVER the
  claim ``statement`` text. That body lives in the LOCAL ``comms_claims`` table
  only (the ADR-0020 discipline).
- **Injectable ``now``.** Every write accepts ``now`` (a ``datetime``); when given
  it fixes the timestamp (``COALESCE(%s, now())``) so tests are deterministic,
  otherwise the DB clock is the source of truth (as elsewhere in the repo).
- **Zero tolerance.** A single ``fabrication`` strike permanently revokes the
  identity's human-relay capability + quarantines/revokes its trust state — it is
  never undone here (recovery is a deliberate human act, out of S1 scope).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Union
from uuid import UUID

import psycopg
from pydantic import BaseModel
from psycopg.types.json import Jsonb

from .event_types import (
    EVENT_COMMS_CLAIM_REJECTED,
    EVENT_COMMS_CLAIM_VERIFIED,
    EVENT_COMMS_FABRICATION_DETECTED,
    EVENT_COMMS_PROOF_REQUESTED,
    EVENT_TRUST_CAPABILITY_REVOKED,
    EVENT_TRUST_STRIKE,
)
from .events import append_event
from .grounding import Claim, EvidenceRef
from .models import make_event

# --- Vocabulary (closed sets, mirrored by the migration CHECK constraints) ---

TRUST_TRUSTED = "trusted"
TRUST_QUARANTINED = "quarantined"
TRUST_REVOKED = "revoked"
TRUST_STATES: frozenset[str] = frozenset({TRUST_TRUSTED, TRUST_QUARANTINED, TRUST_REVOKED})

VERIFICATION_VERIFIED = "verified"
VERIFICATION_REJECTED = "rejected"
VERIFICATION_UNVERIFIABLE = "unverifiable"
VERIFICATION_STATUSES: frozenset[str] = frozenset(
    {VERIFICATION_VERIFIED, VERIFICATION_REJECTED, VERIFICATION_UNVERIFIABLE}
)

#: The zero-tolerance strike kind: a fabrication permanently revokes relay.
STRIKE_FABRICATION = "fabrication"

#: Human-facing comms + trust provenance events are not naturally workstream-scoped
#: (they concern an *identity*, not a vertical), so they log under the Spokesman's
#: own workstream. The events table requires a non-empty workstream.
COMMS_WORKSTREAM = "spokesman"

_TRUST_COLUMNS = (
    "identity, trust_state, human_relay_allowed, strikes, last_strike_at, "
    "created_at, updated_at"
)
_CLAIM_COLUMNS = (
    "id, message_ref, originating_identity, statement, evidence, is_judgment, "
    "verification_status, verified_by, reason, seq, created_at"
)


# --- Row models -------------------------------------------------------------


class IdentityTrust(BaseModel):
    """A persisted trust-ledger row for one role / agent-workflow-identity."""

    identity: str
    trust_state: str
    human_relay_allowed: bool
    strikes: int
    last_strike_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class CommsClaim(BaseModel):
    """A persisted human-facing claim + its evidence + verification verdict."""

    id: UUID
    message_ref: Optional[str] = None
    originating_identity: str
    statement: str
    evidence: Any = None
    is_judgment: bool = False
    verification_status: Optional[str] = None
    verified_by: Optional[str] = None
    reason: Optional[str] = None
    seq: int
    created_at: datetime


def _to_trust(row: dict) -> IdentityTrust:
    return IdentityTrust.model_validate(dict(row))


def _to_claim(row: dict) -> CommsClaim:
    return CommsClaim.model_validate(dict(row))


def _emit(conn: psycopg.Connection, *, type: str, **payload: Any) -> None:
    """Append a BODY-FREE ``comms.*`` / ``trust.*`` event (ids/identity/status/kind
    /counts only — NEVER the claim statement text). Runs inside the caller's open
    transaction (psycopg nests it as a savepoint) so the row write + event are
    atomic and replayable (invariants 5 & 6)."""
    append_event(conn, make_event(workstream=COMMS_WORKSTREAM, type=type, payload=payload))


def _normalize_evidence(evidence: Any) -> list[dict]:
    """Coerce ``evidence`` (a list of :class:`EvidenceRef`, dicts, or a
    :class:`Claim`-style payload) into a plain JSON-serializable list of dicts."""
    if evidence is None:
        return []
    out: list[dict] = []
    for item in evidence:
        if isinstance(item, EvidenceRef):
            out.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            out.append(item)
        else:
            raise TypeError(f"evidence item must be an EvidenceRef or dict, got {type(item)!r}")
    return out


# --- trust ledger -----------------------------------------------------------


def get_trust(conn: psycopg.Connection, identity: str) -> IdentityTrust:
    """Return the trust record for ``identity``, auto-creating a ``trusted`` row on
    first read.

    First contact with an identity is trusted-by-default (open until it earns a
    strike). The create is an idempotent ``INSERT ... ON CONFLICT DO NOTHING`` so
    concurrent first reads never collide, then the row is read back. Emits no event
    (a default-trusted row carries no signal; only strikes/revocations do).
    """
    if not identity or not identity.strip():
        raise ValueError("identity must be non-empty")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO identity_trust (identity) VALUES (%s) "
                "ON CONFLICT (identity) DO NOTHING",
                (identity,),
            )
            cur.execute(
                f"SELECT {_TRUST_COLUMNS} FROM identity_trust WHERE identity = %s",
                (identity,),
            )
            row = cur.fetchone()
    return _to_trust(row)


def is_relay_allowed(conn: psycopg.Connection, identity: str) -> bool:
    """True iff ``identity`` may currently relay to the human.

    Reads the ledger (auto-creating a trusted row on first contact) and requires
    BOTH ``human_relay_allowed`` AND a non-fenced ``trust_state`` — neither
    ``revoked`` (a revoked identity can never speak to the human again) NOR
    ``quarantined``. This mirrors the claim-side fence in
    :func:`runtime.tasks.grab_task` (ADR-0021), so a quarantined identity is
    symmetrically barred from BOTH claiming work and relaying to the human,
    regardless of the flag.
    """
    rec = get_trust(conn, identity)
    return rec.human_relay_allowed and rec.trust_state not in (
        TRUST_REVOKED,
        TRUST_QUARANTINED,
    )


# --- claims -----------------------------------------------------------------


def record_claim(
    conn: psycopg.Connection,
    *,
    originating_identity: str,
    statement: str,
    evidence: Any,
    is_judgment: bool = False,
    message_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> UUID:
    """Persist one human-facing claim (status pending/NULL until checked); return its id.

    ``evidence`` is a list of :class:`~runtime.grounding.EvidenceRef` (or plain
    dicts) stored verbatim in the ``evidence`` JSONB column. The ``statement`` body
    is written to the LOCAL ``comms_claims`` table ONLY — never to the event log.
    A gapless monotonic ``seq`` (from ``comms_claims_seq``) is assigned so claims
    replay in creation order (mirrors ``events.seq``). No event is emitted here: a
    claim only becomes wire-visible once the Spokesman gate verifies/rejects it.
    """
    if not originating_identity or not originating_identity.strip():
        raise ValueError("originating_identity must be non-empty")
    if not statement or not statement.strip():
        raise ValueError("claim statement must be non-empty")
    ev = _normalize_evidence(evidence)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO comms_claims
                    (message_ref, originating_identity, statement, evidence,
                     is_judgment, seq, created_at)
                VALUES (%s, %s, %s, %s, %s, nextval('comms_claims_seq'),
                        COALESCE(%s::timestamptz, now()))
                RETURNING {_CLAIM_COLUMNS}
                """,
                (message_ref, originating_identity, statement, Jsonb(ev),
                 is_judgment, now),
            )
            claim = _to_claim(cur.fetchone())
    return claim.id


def record_claim_from(
    conn: psycopg.Connection,
    originating_identity: str,
    claim: Claim,
    *,
    message_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> UUID:
    """Convenience: persist a typed :class:`~runtime.grounding.Claim` (already
    validated — a non-judgment claim is guaranteed to carry evidence)."""
    return record_claim(
        conn,
        originating_identity=originating_identity,
        statement=claim.statement,
        evidence=claim.evidence,
        is_judgment=claim.is_judgment,
        message_ref=message_ref,
        now=now,
    )


def get_claim(conn: psycopg.Connection, claim_id: UUID) -> Optional[CommsClaim]:
    """Fetch one claim by id, or ``None`` if absent."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM comms_claims WHERE id = %s", (claim_id,)
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return _to_claim(row) if row else None


def set_claim_verification(
    conn: psycopg.Connection,
    claim_id: UUID,
    status: str,
    *,
    verified_by: str,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[CommsClaim]:
    """Record the Spokesman gate's verdict on a claim; emit a body-free event.

    ``status`` ∈ ``verified`` | ``rejected`` | ``unverifiable``. Stamps
    ``verified_by`` + optional ``reason`` (local only). Emits
    ``comms.claim_verified`` for ``verified`` and ``comms.claim_rejected`` for
    ``rejected``; ``unverifiable`` (evidence could not be resolved — an honest
    "couldn't confirm", NOT a fabrication) is persisted without a wire event and
    is simply not sendable as fact. Returns the updated claim, or ``None`` if the
    id is unknown.
    """
    if status not in VERIFICATION_STATUSES:
        raise ValueError(
            f"unknown verification status {status!r} (allowed: {sorted(VERIFICATION_STATUSES)})"
        )
    if not verified_by or not verified_by.strip():
        raise ValueError("verified_by must be non-empty")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE comms_claims
                SET verification_status = %s, verified_by = %s, reason = %s
                WHERE id = %s
                RETURNING {_CLAIM_COLUMNS}
                """,
                (status, verified_by, reason, claim_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            claim = _to_claim(row)
        if status == VERIFICATION_VERIFIED:
            _emit(conn, type=EVENT_COMMS_CLAIM_VERIFIED,
                  claim_id=str(claim.id), identity=claim.originating_identity,
                  verified_by=verified_by, status=status)
        elif status == VERIFICATION_REJECTED:
            _emit(conn, type=EVENT_COMMS_CLAIM_REJECTED,
                  claim_id=str(claim.id), identity=claim.originating_identity,
                  verified_by=verified_by, status=status)
        # unverifiable: no wire event (honest non-confirmation, not a rejection).
    return claim


def record_proof_request(
    conn: psycopg.Connection,
    identity: str,
    *,
    claim_id: Union[UUID, str],
) -> None:
    """Emit a BODY-FREE ``comms.proof_requested`` back to the originating identity.

    Used by the Spokesman gate when a factual claim is UNVERIFIABLE (its evidence
    could not be resolved against source of truth — missing proof, NOT a
    fabrication). The claim is withheld (never relayed as fact) and the originator
    is asked to supply resolvable evidence. Like every ``comms.*`` event this is
    body-free: it carries ONLY the ``claim_id`` + ``identity`` — never the claim
    ``statement`` text (invariants 5 & 6). Centralized here so the trust module
    stays the single guarded writer of the ``comms.*`` event stream.
    """
    if not identity or not identity.strip():
        raise ValueError("identity must be non-empty")
    cid = str(claim_id) if claim_id is not None else None
    with conn.transaction():
        _emit(conn, type=EVENT_COMMS_PROOF_REQUESTED, identity=identity, claim_id=cid)


# --- zero-tolerance strikes -------------------------------------------------


def record_strike(
    conn: psycopg.Connection,
    identity: str,
    *,
    claim_id: Optional[Union[UUID, str]] = None,
    kind: str = STRIKE_FABRICATION,
    detail: Optional[str] = None,
    now: Optional[datetime] = None,
) -> IdentityTrust:
    """Record a trust strike against ``identity`` — zero-tolerance for fabrication.

    Increments ``strikes`` and stamps ``last_strike_at``. On a ``fabrication``
    strike (the default, worst offense) it ALSO permanently sets
    ``trust_state='revoked'`` + ``human_relay_allowed=false`` — the identity can
    never speak to the human again — and emits, in one transaction, a body-free
    ``comms.fabrication_detected`` (🚨), a ``trust.strike``, and a
    ``trust.capability_revoked``. A non-fabrication strike records the strike +
    ``trust.strike`` only (relay is left intact). Auto-creates the ledger row if
    absent and is idempotent-safe: re-striking an already-revoked identity simply
    accrues another strike (revocation stays revoked). ``detail`` is a local-only
    note; it is NEVER placed on the wire.
    """
    if not identity or not identity.strip():
        raise ValueError("identity must be non-empty")
    if not kind or not kind.strip():
        raise ValueError("strike kind must be non-empty")
    is_fabrication = kind == STRIKE_FABRICATION
    with conn.transaction():
        with conn.cursor() as cur:
            # Ensure the ledger row exists, then apply the strike atomically.
            cur.execute(
                "INSERT INTO identity_trust (identity) VALUES (%s) "
                "ON CONFLICT (identity) DO NOTHING",
                (identity,),
            )
            if is_fabrication:
                cur.execute(
                    f"""
                    UPDATE identity_trust
                    SET strikes = strikes + 1,
                        last_strike_at = COALESCE(%s::timestamptz, now()),
                        trust_state = 'revoked',
                        human_relay_allowed = false,
                        updated_at = COALESCE(%s::timestamptz, now())
                    WHERE identity = %s
                    RETURNING {_TRUST_COLUMNS}
                    """,
                    (now, now, identity),
                )
            else:
                cur.execute(
                    f"""
                    UPDATE identity_trust
                    SET strikes = strikes + 1,
                        last_strike_at = COALESCE(%s::timestamptz, now()),
                        updated_at = COALESCE(%s::timestamptz, now())
                    WHERE identity = %s
                    RETURNING {_TRUST_COLUMNS}
                    """,
                    (now, now, identity),
                )
            rec = _to_trust(cur.fetchone())

        cid = str(claim_id) if claim_id is not None else None
        if is_fabrication:
            # 🚨 escalate the fabrication, then log the penalty + the permanent
            # capability revocation — all body-free (no statement/detail on the wire).
            _emit(conn, type=EVENT_COMMS_FABRICATION_DETECTED,
                  identity=identity, claim_id=cid, kind=kind, strikes=rec.strikes)
            _emit(conn, type=EVENT_TRUST_STRIKE,
                  identity=identity, claim_id=cid, kind=kind, strikes=rec.strikes)
            _emit(conn, type=EVENT_TRUST_CAPABILITY_REVOKED,
                  identity=identity, claim_id=cid, kind=kind)
        else:
            _emit(conn, type=EVENT_TRUST_STRIKE,
                  identity=identity, claim_id=cid, kind=kind, strikes=rec.strikes)
    return rec


__all__ = [
    "TRUST_TRUSTED",
    "TRUST_QUARANTINED",
    "TRUST_REVOKED",
    "TRUST_STATES",
    "VERIFICATION_VERIFIED",
    "VERIFICATION_REJECTED",
    "VERIFICATION_UNVERIFIABLE",
    "VERIFICATION_STATUSES",
    "STRIKE_FABRICATION",
    "COMMS_WORKSTREAM",
    "IdentityTrust",
    "CommsClaim",
    "get_trust",
    "is_relay_allowed",
    "record_claim",
    "record_claim_from",
    "get_claim",
    "set_claim_verification",
    "record_proof_request",
    "record_strike",
]
