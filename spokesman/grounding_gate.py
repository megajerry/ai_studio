"""The Spokesman verify-or-refuse grounding gate (ADR-0021, S2).

Doctrine: *everything communicated to the human MUST be grounded.* The Spokesman
relays a factual claim ONLY after it independently verifies the claim's evidence
against the source of truth (the live runtime DB). Unverifiable claims are
withheld and proof is requested from the originating agent; a claim the source of
truth *contradicts* is a **fabrication** — the worst offense — and triggers the
zero-tolerance penalty (permanent relay revocation + escalation). Judgments /
recommendations may pass but are always labelled, never dressed up as fact.

This module is the *verification engine* + the *relay decision*:

- :func:`verify_claim` resolves each :class:`~runtime.grounding.EvidenceRef`
  against source of truth and returns a per-claim :class:`ClaimVerdict`.
- :func:`relay_claims` records provenance for every claim, applies the verdict,
  and decides what (if anything) is sent — enforcing the relay-permission gate,
  the proof-request path, the zero-tolerance fabrication penalty, and the
  verifier-chain cascade.

Scope + honesty (read this before trusting a verdict)
-----------------------------------------------------
The gate performs **structural verification only**: it checks that each evidence
ref *resolves* to a real referent in the source of truth and, where the claim
declares an ``expected`` value, that the referent's actual value *matches* it. It
does **NOT** prove semantic entailment — i.e. that the (natural-language)
``statement`` is genuinely supported by the resolved evidence. A grounded but
mis-described referent (right row, wrong prose) can still pass structurally. The
go-live design leaves a clean seam (:func:`verify_claim` returns the full per-ref
resolution) for a model-judge entailment step to be layered on top; that model
call is deliberately NOT built here (ADR-0021 S2 scope).

Verdict rules (be honest about what each proves):

- **VERIFIED**  — every ref resolves AND matches its ``expected`` where given.
- **UNVERIFIABLE** — some ref cannot be resolved (missing proof, or the DB was
  unreachable), OR a ref's ``expected`` is **ill-formed / uninterpretable** for
  its :class:`EvidenceKind` (see below). This is an honest "couldn't confirm",
  NOT a fabrication: the claim is withheld and proof is requested from the
  originator.
- **REJECTED (fabrication)** — a ref *resolves*, its ``expected`` is a
  **well-formed** assertion for its kind, and the referent's actual value
  CONTRADICTS it. The agent asserted something the source of truth actively
  contradicts.

Ill-formed ``expected`` is NOT fabrication (ADR-0021 follow-up)
--------------------------------------------------------------
A contradiction can only be declared against an ``expected`` the resolver can
actually *interpret* as an assertion for that kind (a task status from the
canonical set; a ``col=val`` on an existing column for ``db_row``; a numeric
count for the count ``metric`` queries; a 64-char sha256 for a ``file``). If
``expected`` cannot be parsed as a valid assertion for the kind (wrong syntax,
unknown column, non-status for a ``task``, non-numeric metric, non-hash file),
the ref is **malformed** → the claim is UNVERIFIABLE (proof requested), never
``contradicted``. Rationale: an honest agent that merely *mis-formats* its
evidence spec must not be branded a fabricator and permanently revoked — the gate
must distinguish "you lied" from "you wrote the spec wrong". Only a well-formed,
genuinely-contradicted ``expected`` still triggers the zero-tolerance penalty.

Fail-closed (ADR-0017): if source of truth cannot be reached, every ref is
treated as unresolved → the claim is UNVERIFIABLE → it is NEVER relayed as fact.
The gate degrades (logs, no crash) and never leaks a raw driver error.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import UUID

import psycopg

from runtime.grounding import Claim, EvidenceKind, EvidenceRef
from runtime.models import TaskStatus
from runtime.trust import (
    STRIKE_FABRICATION,
    VERIFICATION_REJECTED,
    VERIFICATION_UNVERIFIABLE,
    VERIFICATION_VERIFIED,
    is_relay_allowed,
    record_claim_from,
    record_proof_request,
    record_strike,
    set_claim_verification,
)

logger = logging.getLogger("spokesman.grounding_gate")

#: The identity stamped as ``verified_by`` on every verdict — the gate itself.
GATE_IDENTITY = "spokesman-gate"

#: Tables the ``db_row`` resolver may read. The table name cannot be a bound
#: parameter, so it is validated against this allowlist before interpolation — a
#: hostile ``table`` value can never reach the query (mirrors the grab-path
#: allowlist discipline in :mod:`runtime.tasks`).
_READABLE_TABLES: frozenset[str] = frozenset({
    "tasks", "events", "comms_claims", "identity_trust",
    "task_transitions", "approvals", "budgets",
})

#: The canonical task-lifecycle statuses (ADR-0015). A ``task`` ref's ``expected``
#: is only interpretable as a status assertion if it is one of these bare values;
#: anything else (a ``db_row``-style ``status=merged``, garbage) is *malformed* and
#: routes to UNVERIFIABLE, never to a fabrication verdict.
_TASK_STATUSES: frozenset[str] = frozenset(s.value for s in TaskStatus)

#: Named, read-only metric queries the ``metric`` resolver may run. A metric ref's
#: ``locator`` must be one of these names — arbitrary SQL is never executed. Each
#: returns a single scalar column ``v`` (rendered to text for comparison against
#: the claim's ``expected``). Widening the surface = adding a vetted entry here.
_METRIC_QUERIES: dict[str, str] = {
    "tasks_total": "SELECT count(*)::text AS v FROM tasks",
    "tasks_merged_total": "SELECT count(*)::text AS v FROM tasks WHERE status = 'merged'",
    "events_total": "SELECT count(*)::text AS v FROM events",
    "comms_claims_total": "SELECT count(*)::text AS v FROM comms_claims",
}


# --- per-ref resolution result ----------------------------------------------


@dataclass(frozen=True)
class RefResolution:
    """The outcome of resolving one :class:`EvidenceRef` against source of truth.

    ``resolved`` — the referent was found in source of truth AND its ``expected``
    (if any) was a well-formed assertion the resolver could check and it matched.
    ``contradicted`` — the referent was found, ``expected`` was a *well-formed*
    assertion for the kind, and the actual value differs (this is the fabrication
    signal). ``malformed`` — the referent may or may not exist, but ``expected``
    is *ill-formed / uninterpretable* for the kind (wrong syntax, unknown column,
    non-status task, …); the assertion cannot be checked, so this routes to
    UNVERIFIABLE (proof requested), NEVER to fabrication. ``malformed`` implies
    ``resolved=False`` and ``contradicted=False``. ``actual`` + ``note`` are
    LOCAL-ONLY diagnostics (they may carry real values) used to compose the local
    ``reason``; they are NEVER placed on the event wire.
    """

    kind: str
    locator: str
    resolved: bool
    contradicted: bool = False
    malformed: bool = False
    actual: Optional[str] = None
    note: str = ""


@dataclass(frozen=True)
class ClaimVerdict:
    """The gate's verdict for one factual claim + the per-ref resolutions."""

    status: str  # VERIFICATION_VERIFIED | _UNVERIFIABLE | _REJECTED
    refs: list[RefResolution] = field(default_factory=list)
    reason: str = ""


# --- individual resolvers (each does ONE read; all raise → caught + fail-closed) ---


def _resolve_event(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """``event`` — the event exists (by ``seq`` or ``id``); if ``expected`` is
    given it must equal the event ``type``. Locator: ``seq:<n>`` / ``id:<uuid>`` /
    a bare uuid / a bare integer seq."""
    loc = ref.locator.strip()
    seq: Optional[int] = None
    eid: Optional[UUID] = None
    if loc.startswith("seq:"):
        seq = int(loc[4:])
    elif loc.startswith("id:"):
        eid = UUID(loc[3:])
    else:
        try:
            eid = UUID(loc)
        except ValueError:
            seq = int(loc)  # bare seq (raises → caught upstream → unresolved)
    with conn.cursor() as cur:
        if seq is not None:
            cur.execute("SELECT type FROM events WHERE seq = %s", (seq,))
        else:
            cur.execute("SELECT type FROM events WHERE id = %s", (eid,))
        row = cur.fetchone()
    if row is None:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="no such event")
    actual = row["type"]
    if ref.expected is not None and actual != ref.expected:
        return RefResolution(ref.kind.value, ref.locator, resolved=True,
                             contradicted=True, actual=actual,
                             note=f"event type is {actual!r}, expected {ref.expected!r}")
    return RefResolution(ref.kind.value, ref.locator, resolved=True, actual=actual)


def _resolve_task(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """``task`` — the task exists; if ``expected`` is given it must be a **bare
    status** from the canonical lifecycle set and equal the task ``status``.
    Locator: a task uuid, optionally ``task:<uuid>``.

    ``expected`` validation (ADR-0021 follow-up): a task-status assertion is only
    interpretable as a bare status in :data:`_TASK_STATUSES`. Anything else — a
    ``db_row``-style ``"status=merged"``, or any non-status string — is
    *malformed* → UNVERIFIABLE, never a contradiction. Only a well-formed status
    that differs from the actual status is a fabrication."""
    loc = ref.locator.strip()
    if loc.startswith("task:"):
        loc = loc[5:]
    tid = UUID(loc)  # raises on a non-uuid → caught upstream → unresolved
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    if row is None:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="no such task")
    actual = row["status"]
    if ref.expected is not None:
        want = ref.expected.strip()
        if want not in _TASK_STATUSES:
            return RefResolution(ref.kind.value, ref.locator, resolved=False,
                                 malformed=True, actual=actual,
                                 note=f"task expected {ref.expected!r} is not a known "
                                      "status (expected a bare lifecycle status)")
        if actual != want:
            return RefResolution(ref.kind.value, ref.locator, resolved=True,
                                 contradicted=True, actual=actual,
                                 note=f"task status is {actual!r}, expected {want!r}")
    return RefResolution(ref.kind.value, ref.locator, resolved=True, actual=actual)


def _parse_expected_fields(
    expected: Optional[str],
) -> tuple[list[tuple[str, str]], Optional[str]]:
    """Parse a ``db_row`` ``expected`` of ``"col=val[,col2=val2...]"`` into pairs.

    Returns ``(fields, error)``. ``error`` is a non-None note when ``expected`` is
    *malformed* (a segment without ``=``, or a segment with an empty column name);
    such an ``expected`` cannot be interpreted as a column assertion, so the
    caller routes it to UNVERIFIABLE rather than treating it as a contradiction.
    Does NOT raise (ADR-0021 follow-up: mis-formatting is not fabrication)."""
    out: list[tuple[str, str]] = []
    if not expected:
        return out, None
    for part in expected.split(","):
        if "=" not in part:
            return [], f"db_row expected must be col=val, got {part.strip()!r}"
        col, _, val = part.partition("=")
        col = col.strip()
        if not col:
            return [], f"db_row expected has an empty column name in {part.strip()!r}"
        out.append((col, val.strip()))
    return out, None


def _resolve_db_row(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """``db_row`` — a row at ``table:pk`` exists; if ``expected`` (``col=val,...``)
    is given, every named column must match. Locator: ``table:<pkval>`` or
    ``table:<pkcol>=<pkval>`` (pk column defaults to ``id``). ``table`` is
    allowlist-validated before interpolation; every value is a bound parameter."""
    table, _, rest = ref.locator.strip().partition(":")
    table = table.strip()
    if table not in _READABLE_TABLES:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note=f"table {table!r} not readable")
    if not rest:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="missing pk")
    if "=" in rest:
        pkcol, _, pkval = rest.partition("=")
        pkcol, pkval = pkcol.strip(), pkval.strip()
    else:
        pkcol, pkval = "id", rest.strip()
    if not pkcol.isidentifier():  # pk column is interpolated → must be a plain name
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="invalid pk column")
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE {pkcol} = %s", (pkval,))
        row = cur.fetchone()
    if row is None:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="no such row")
    fields, parse_error = _parse_expected_fields(ref.expected)
    if parse_error is not None:  # ill-formed spec → unverifiable, not fabrication
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             malformed=True, note=parse_error)
    for col, want in fields:
        if col not in row:  # references a column that doesn't exist → uninterpretable
            return RefResolution(ref.kind.value, ref.locator, resolved=False,
                                 malformed=True,
                                 note=f"db_row expected references unknown column {col!r}")
        got = row[col]
        if str(got) != want:
            return RefResolution(ref.kind.value, ref.locator, resolved=True,
                                 contradicted=True, actual=f"{col}={got}",
                                 note=f"{col} is {got!r}, expected {want!r}")
    return RefResolution(ref.kind.value, ref.locator, resolved=True)


def _resolve_metric(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """``metric`` — a whitelisted read query returns a value matching ``expected``.
    Locator must be a key of :data:`_METRIC_QUERIES` (arbitrary SQL is refused).

    ``expected`` validation (ADR-0021 follow-up): every whitelisted metric returns
    an integer count, so a metric assertion is only interpretable as a numeric
    value. A non-numeric ``expected`` is *malformed* → UNVERIFIABLE, never a
    contradiction. A numeric ``expected`` that differs from the actual count is a
    fabrication (compared by integer value, so ``"5"`` == ``" 5 "``)."""
    sql = _METRIC_QUERIES.get(ref.locator.strip())
    if sql is None:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="metric not whitelisted")
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    actual = None if row is None else str(row["v"])
    if actual is None:
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="metric returned no value")
    if ref.expected is not None:
        try:
            want = int(ref.expected.strip())
        except (TypeError, ValueError):  # non-numeric spec for a count metric
            return RefResolution(ref.kind.value, ref.locator, resolved=False,
                                 malformed=True, actual=actual,
                                 note=f"metric expected {ref.expected!r} is not a "
                                      "numeric count")
        if int(actual) != want:
            return RefResolution(ref.kind.value, ref.locator, resolved=True,
                                 contradicted=True, actual=actual,
                                 note=f"metric is {actual!r}, expected {want!r}")
    return RefResolution(ref.kind.value, ref.locator, resolved=True, actual=actual)


def _is_sha256_hex(value: str) -> bool:
    """True iff ``value`` is a 64-char lowercase-able hex sha256 digest."""
    v = value.strip().lower()
    return len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def _resolve_file(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """``file`` — a ``path[:line]`` resolves on disk; if ``expected`` is given it
    must be a 64-char hex sha256 and the file's digest must match it. (``conn``
    unused; kept uniform.)

    ``expected`` validation (ADR-0021 follow-up): the only interpretable assertion
    about a file's content is its sha256 digest. A non-hash ``expected`` is
    *malformed* → UNVERIFIABLE (rather than silently ignored/passed), never a
    contradiction. A well-formed hash that mismatches is a fabrication."""
    loc = ref.locator.strip()
    path = loc
    head, sep, tail = loc.rpartition(":")
    if sep and tail.isdigit():  # strip a trailing :line
        path = head
    if not os.path.exists(path):
        return RefResolution(ref.kind.value, ref.locator, resolved=False,
                             note="path does not exist")
    exp = ref.expected
    if exp is not None:
        if not _is_sha256_hex(exp):
            return RefResolution(ref.kind.value, ref.locator, resolved=False,
                                 malformed=True,
                                 note="file expected must be a 64-char sha256 hex digest")
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if digest != exp.strip().lower():
            return RefResolution(ref.kind.value, ref.locator, resolved=True,
                                 contradicted=True, actual=digest,
                                 note="file hash mismatch")
    return RefResolution(ref.kind.value, ref.locator, resolved=True)


def _resolve_artifact(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """``artifact`` — a content-hash pointer into an artifact store. No
    content-addressed store is wired in this repo yet, so an artifact ref cannot
    be resolved → UNVERIFIABLE (honest: missing proof, never a false pass). This
    is the clean seam for a future object-store resolver."""
    return RefResolution(ref.kind.value, ref.locator, resolved=False,
                         note="no artifact store to resolve against")


_RESOLVERS: dict[EvidenceKind, Callable[[psycopg.Connection, EvidenceRef], RefResolution]] = {
    EvidenceKind.EVENT: _resolve_event,
    EvidenceKind.TASK: _resolve_task,
    EvidenceKind.DB_ROW: _resolve_db_row,
    EvidenceKind.METRIC: _resolve_metric,
    EvidenceKind.FILE: _resolve_file,
    EvidenceKind.ARTIFACT: _resolve_artifact,
}


def resolve_ref(conn: psycopg.Connection, ref: EvidenceRef) -> RefResolution:
    """Resolve one evidence ref, fail-CLOSED. Any error (bad locator, DB
    unreachable, aborted txn) → ``resolved=False`` (UNVERIFIABLE, never a pass).
    A read that raised leaves the connection in an aborted state, so we roll back
    before returning so the caller's next write can proceed."""
    fn = _RESOLVERS.get(ref.kind)
    if fn is None:  # pragma: no cover - EvidenceKind is a closed set
        return RefResolution(str(ref.kind), ref.locator, resolved=False,
                             note="unknown evidence kind")
    try:
        res = fn(conn, ref)
        if not conn.autocommit:
            conn.commit()
        return res
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY resolution error
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - best-effort
            pass
        logger.warning("evidence resolution failed (%s:%s): %s",
                       getattr(ref.kind, "value", ref.kind), ref.locator, exc)
        return RefResolution(getattr(ref.kind, "value", str(ref.kind)), ref.locator,
                             resolved=False, note=f"resolution error: {type(exc).__name__}")


def verify_claim(conn: psycopg.Connection, claim: Claim) -> ClaimVerdict:
    """Return the structural verdict for one FACTUAL claim (see module docstring).

    REJECTED only if a ref is *contradicted* — i.e. it resolved and its
    **well-formed** ``expected`` differs from the actual value. A ref whose
    ``expected`` is *malformed* (uninterpretable for its kind) is NOT a
    contradiction: it routes to UNVERIFIABLE (proof requested), so a mis-formatted
    spec is never punished as fabrication. Else VERIFIED iff every ref resolved;
    else UNVERIFIABLE. A judgment should never reach here (judgments are relayed
    labelled, not verified) — if one does it is treated as UNVERIFIABLE (no
    evidence to structurally confirm)."""
    resolutions = [resolve_ref(conn, r) for r in claim.evidence]
    if any(r.contradicted for r in resolutions):
        status = VERIFICATION_REJECTED
        bad = next(r for r in resolutions if r.contradicted)
        reason = f"contradicted: {bad.note}"
    elif resolutions and all(r.resolved for r in resolutions):
        status = VERIFICATION_VERIFIED
        reason = "all evidence resolved and matched"
    else:
        status = VERIFICATION_UNVERIFIABLE
        problems = "; ".join(r.note for r in resolutions if not r.resolved) or "no evidence"
        if any(r.malformed for r in resolutions):
            reason = f"unverifiable (evidence spec not interpretable): {problems}"
        else:
            reason = f"unresolved: {problems}"
    return ClaimVerdict(status=status, refs=resolutions, reason=reason)


# --- verifier-chain cascade -------------------------------------------------


def _task_ids_from_claim(claim: Claim) -> list[UUID]:
    """Extract referenced task ids from a claim's evidence (``task`` refs and
    ``db_row`` refs on the ``tasks`` table). Best-effort: unparseable locators are
    skipped."""
    ids: list[UUID] = []
    for ref in claim.evidence:
        loc = ref.locator.strip()
        raw: Optional[str] = None
        if ref.kind == EvidenceKind.TASK:
            raw = loc[5:] if loc.startswith("task:") else loc
        elif ref.kind == EvidenceKind.DB_ROW:
            table, _, rest = loc.partition(":")
            if table.strip() == "tasks" and rest and "=" not in rest:
                raw = rest.strip()
        if raw is None:
            continue
        try:
            ids.append(UUID(raw))
        except ValueError:
            continue
    return ids


def cascade_strike_to_approvers(
    conn: psycopg.Connection,
    claim: Claim,
    triggering_claim_id: UUID,
    *,
    originating_identity: Optional[str] = None,
) -> list[str]:
    """Strike the identities that approved a task a fabricated claim references.

    A fabrication that rests on a *task* means the reviewer chain that moved that
    task ``ready_for_review → approved`` passed a fabricated result — so they share
    accountability. We derive that chain from the append-only ``task_transitions``
    telemetry (the ``agent_id`` on the ``ready_for_review → approved`` hop) and
    apply the same zero-tolerance strike to each distinct approver.

    LIMITS (documented, deliberately best-effort):
    - Only the *approval* hop is cascaded — not every agent that touched the task.
    - ``agent_id`` may be NULL (e.g. the internal auto-approve path in
      :func:`runtime.tasks.complete_task` records no agent); such hops cannot be
      attributed and are skipped rather than guessed.
    - The ``originating_identity`` (already struck for the fabrication itself) is
      EXCLUDED from the cascade, so an identity that both originated the claim and
      approved its task earns exactly ONE strike + ONE ``comms.fabrication_detected``
      for that claim (no double-count in the fabrication-rate telemetry).
    - Fail-closed on read error: returns what it could derive, never crashes.
    Returns the list of identities struck by the cascade.
    """
    struck: list[str] = []
    try:
        task_ids = _task_ids_from_claim(claim)
        approvers: set[str] = set()
        with conn.cursor() as cur:
            for tid in task_ids:
                cur.execute(
                    """
                    SELECT DISTINCT agent_id FROM task_transitions
                    WHERE task_id = %s AND from_status = 'ready_for_review'
                      AND to_status = 'approved' AND agent_id IS NOT NULL
                    """,
                    (tid,),
                )
                approvers.update(r["agent_id"] for r in cur.fetchall())
        # The originator was already struck for the fabrication — never double-strike.
        approvers.discard(originating_identity)
        if not conn.autocommit:
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - best-effort, never crash the gate
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("verifier-chain derivation failed: %s", exc)
        return struck

    for approver in sorted(approvers):
        record_strike(conn, approver, claim_id=triggering_claim_id,
                      kind=STRIKE_FABRICATION,
                      detail="cascade: approved a task carrying a fabricated claim")
        struck.append(approver)
    return struck


# --- relay decision ---------------------------------------------------------


def _compose_relay(sendable: list[tuple[str, str]]) -> str:
    """Render the human-facing text for the sendable claims — judgments labelled."""
    lines: list[str] = []
    for label, statement in sendable:
        lines.append(f"[judgment] {statement}" if label == "judgment" else statement)
    return "\n".join(lines)


def relay_claims(
    conn: psycopg.Connection,
    notifier,
    *,
    kind: str,
    originating_identity: str,
    claims: list[Claim],
    message_ref: Optional[str] = None,
) -> dict:
    """Verify-or-refuse gate for one outbound message. Returns a summary dict.

    Flow (per the doctrine):
      1. **Relay-permission gate (fail closed).** If the trust ledger has revoked/
         quarantined ``originating_identity`` — or is unreachable — nothing is
         relayed.
      2. **Per claim:** record provenance (:func:`record_claim_from`), then:
         - *judgment* → relayed **labelled** (never as verified fact);
         - *factual* → structurally verified. VERIFIED → relayed;
           UNVERIFIABLE → withheld + ``comms.proof_requested`` to the originator;
           REJECTED → **fabrication**: strike (permanent revocation), cascade to
           the task's approver chain, and mark the whole batch for withholding.
      3. **Send decision (fail closed on fabrication).** If ANY claim in the batch
         was a fabrication, NOTHING from this now-revoked source is relayed — only
         a 🚨 escalation to the human. Otherwise the verified facts + labelled
         judgments are sent together under ``kind``.
    """
    # 1. Relay-permission gate — fail closed (unreachable ledger ⇒ relay nothing).
    try:
        allowed = is_relay_allowed(conn, originating_identity)
    except Exception as exc:  # noqa: BLE001 - DB unreachable ⇒ fail closed
        logger.warning("relay gate: trust ledger unreachable for %s: %s",
                       originating_identity, exc)
        return {"blocked": True, "relayed": [], "claims": [], "escalated": False,
                "reason": "trust ledger unreachable (fail closed)"}
    if not allowed:
        return {"blocked": True, "relayed": [], "claims": [], "escalated": False,
                "reason": "originating identity is not permitted to relay"}

    results: list[dict] = []
    sendable: list[tuple[str, str]] = []
    fabrication = False

    for claim in claims:
        cid = record_claim_from(conn, originating_identity, claim, message_ref=message_ref)
        if claim.is_judgment:
            # A judgment passes the gate but is ALWAYS labelled — never verified fact.
            results.append({"claim_id": str(cid), "status": "judgment", "sent": True})
            sendable.append(("judgment", claim.statement))
            continue

        verdict = verify_claim(conn, claim)
        if verdict.status == VERIFICATION_VERIFIED:
            set_claim_verification(conn, cid, VERIFICATION_VERIFIED,
                                   verified_by=GATE_IDENTITY, reason=verdict.reason)
            results.append({"claim_id": str(cid), "status": VERIFICATION_VERIFIED,
                            "sent": True})
            sendable.append(("fact", claim.statement))
        elif verdict.status == VERIFICATION_UNVERIFIABLE:
            set_claim_verification(conn, cid, VERIFICATION_UNVERIFIABLE,
                                   verified_by=GATE_IDENTITY, reason=verdict.reason)
            record_proof_request(conn, originating_identity, claim_id=cid)
            results.append({"claim_id": str(cid), "status": VERIFICATION_UNVERIFIABLE,
                            "sent": False, "proof_requested": True})
        else:  # REJECTED — fabrication → zero-tolerance penalty.
            fabrication = True
            set_claim_verification(conn, cid, VERIFICATION_REJECTED,
                                   verified_by=GATE_IDENTITY, reason=verdict.reason)
            record_strike(conn, originating_identity, claim_id=cid,
                          kind=STRIKE_FABRICATION, detail=verdict.reason)
            cascaded = cascade_strike_to_approvers(
                conn, claim, cid, originating_identity=originating_identity)
            results.append({"claim_id": str(cid), "status": VERIFICATION_REJECTED,
                            "sent": False, "cascade_struck": cascaded})

    # 3. Send decision.
    if fabrication:
        # 🚨 Escalate the fabrication and withhold EVERYTHING from this source —
        # it is now revoked; even claims that verified earlier in the batch are not
        # relayed (fail closed). The escalation is body-free: no statement/evidence.
        notifier.notify(
            "alarm",
            f"Fabrication detected from {originating_identity}; relay permanently "
            "revoked. Nothing from this source was sent — see the trust ledger.",
        )
        return {"blocked": True, "fabrication": True, "escalated": True,
                "relayed": [], "claims": results,
                "reason": "fabrication in batch — source revoked, batch withheld"}

    relayed: list[str] = []
    if sendable:
        notifier.notify(kind, _compose_relay(sendable))
        relayed = [statement for _, statement in sendable]
    return {"blocked": False, "fabrication": False, "escalated": False,
            "relayed": relayed, "claims": results}


__all__ = [
    "GATE_IDENTITY",
    "RefResolution",
    "ClaimVerdict",
    "resolve_ref",
    "verify_claim",
    "cascade_strike_to_approvers",
    "relay_claims",
]
