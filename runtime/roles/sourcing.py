"""Sourcing role — research models/pricing → propose a registry update (ADR-0005).

The Sourcing agent is Productivity's *keep-the-catalog-current* half (ADR-0005).
Model options reprice + reshuffle roughly monthly, so picking and pricing models is
a continuous job. This role researches credible sources (LMArena, provider
pricing/docs) and proposes a registry update **through the normal PR + review
loop** — so model choices stay traceable and human-approvable and never drift
silently. It is the runtime analogue of opening a PR: it does NOT touch GitHub;
it produces a **reviewable candidate** artifact plus the ADR-0005 approval
envelope, and never mutates the live registry directly.

It acts through exactly the sanctioned seams — never agent-direct (architecture
§9, CLAUDE.md invariants 1-3):

- **Search only via the gateway** — ``search(conn, role="sourcing", query=…)``
  (:mod:`runtime.search.gateway`): policy-gated on ``net.fetch``, cached, keyless
  dry-run. The Sourcing agent NEVER fetches the network itself, and a role lacking
  ``net.fetch`` is DENIED (nothing fetched, nothing cached).
- **Model call only via ``call_model``** — a routed/costed/logged, traceability-
  only dry-run synthesis step whose text does NOT decide the proposal (the
  candidate specs + the envelope decision are derived **deterministically** so the
  loop is reproducible keyless).
- **Any file write via the policy-gated tool layer** — the candidate proposal is
  written via ``invoke(role="sourcing", tool_name="filesystem", op="write", …)``
  to a review path (``proposals/models.candidate.yaml`` under the confined tool
  root; git-ignored). A role without ``fs.write`` is DENIED (nothing written).

**Evidence over claims (ADR-0014).** A candidate's ``provenance`` is derived from
the *search results* the gateway returned (their URLs) + the sourcing date — never
copied from a bare model/payload claim. The proposed prices come from the task's
candidate list, but the provenance that makes them reviewable is grounded in what
was actually gathered.

**Approval envelope (ADR-0005/0006).** Each candidate is classified against the
CURRENT registry:

- a **new provider**, a **new tier** (objective/scope-affecting), or a
  **budget-increasing** swap (a higher input/output price than the tier's current
  reference) → 🛑 a real :func:`runtime.approvals.request_approval` ("adopt model
  registry update"); the proposal awaits a human.
- an **in-band swap** within the cost/quality band (a known provider, same tier,
  price ≤ the current reference) → **auto-adopt + 📣** (emit ``sourcing.autoadopted``).

The whole proposal takes the stricter path if ANY candidate needs approval.

Invariants it upholds:

- **No loop.** A sourcing task enqueues nothing — it produces the candidate +
  the envelope decision and stops. There is no sourcing-of-a-sourcing.
- **Events leak nothing.** ``sourcing.*`` events carry model ids, counts, a
  provenance **hash**, and the decision — never a secret/API key, and never the
  raw provenance URLs (only their hash). ``search`` emits its own dims-only events.
- **Never mutates the live registry.** It only writes the candidate proposal to a
  review path; adopting it is a separate, reviewed step.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, Field

from ..approvals import request_approval as _request_approval
from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..model.call import call_model as _call_model
from ..model.registry import ModelSpec, Registry, Tier, load_registry
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..search import SearchResult, search as _search
from ..tools import ToolRegistry

log = logging.getLogger("runtime.roles.sourcing")

#: Role event: a candidate registry update was proposed (counts/ids/hash/decision).
EVENT_SOURCING_PROPOSED = "sourcing.proposed"

#: 📣 Role event: an in-band swap was auto-adopted (within the cost/quality band).
EVENT_SOURCING_AUTOADOPTED = "sourcing.autoadopted"

#: The queue task types the worker dispatches to :func:`run_sourcing`.
SOURCING_TASK_TYPES = ("research.models", "sourcing")

#: The role name the policy gate checks (must be granted ``net.fetch`` + ``fs.write``).
ROLE = "sourcing"

#: The 🛑 tier marker used for the "adopt model registry update" approval (ADR-0006).
SOURCING_APPROVAL_TIER = "🛑"

#: Hard cap on candidates sourced per task — bounds fan-out (one search each).
MAX_CANDIDATES = 8

#: Default number of search hits gathered per candidate (the gateway caps + caches).
DEFAULT_RESULTS = 5

#: Default review path (under the confined tool root; git-ignored). NOT the live
#: registry — adopting a candidate is a separate, reviewed step.
DEFAULT_PROPOSALS_PATH = "proposals/models.candidate.yaml"

#: Live registry filenames the candidate must NEVER overwrite (belt-and-suspenders;
#: the confined tool already can't reach them unless mis-rooted at the runtime dir).
_LIVE_REGISTRY_NAMES = frozenset({"models.yaml", "models.local.yaml", "models.example.yaml"})

#: Decisions a proposal / candidate can carry.
DECISION_APPROVAL = "approval"  # 🛑 new provider / new tier / budget-increasing
DECISION_AUTOADOPT = "autoadopt"  # 📣 in-band swap within the cost/quality band

# Sourcing persona. The digest is titles/urls only (no snippets), so no fetched
# body text reaches the prompt. Its completion is logged for traceability but does
# NOT decide the proposal (candidates + the envelope decision are deterministic).
_SOURCING_PROMPT = (
    "You are the studio Model Sourcing agent. From credible sources (LMArena, "
    "provider pricing/docs), assess the proposed model catalog update and note the "
    "cost/quality trade-offs. Candidates: {ids}. Sources ({count}): {digest}"
)


class CandidateDecision(BaseModel):
    """One proposed :class:`ModelSpec` + its envelope classification."""

    spec: ModelSpec
    decision: str  # DECISION_APPROVAL | DECISION_AUTOADOPT
    reason: str


class SourcingResult(BaseModel):
    """What one sourcing task produced (returned to the worker for the task result).

    Carries ids / counts / a provenance **hash** / the decision — never the raw
    provenance URLs, a secret, or an API key (invariants 5 & 6).
    """

    candidate_count: int
    model_ids: list[str] = Field(default_factory=list)
    provenance_hash: str
    #: Proposal-level decision: DECISION_APPROVAL if ANY candidate needs it, else autoadopt.
    decision: str
    reasons: list[str] = Field(default_factory=list)
    #: Candidate-write outcome: "off" | "executed" | "denied" | "pending".
    candidate_status: str = "off"
    #: Path (tool-root-relative) of the written candidate proposal, if written.
    candidate_path: Optional[str] = None
    #: Set when the envelope raised a 🛑 approval (id only).
    approval_id: Optional[str] = None
    #: True when the whole proposal was auto-adopted in-band (📣).
    autoadopted: bool = False
    #: Providers not present in the current registry (each forces 🛑).
    new_providers: list[str] = Field(default_factory=list)
    #: Candidate ids whose price would increase the budget (each forces 🛑).
    budget_increasing_ids: list[str] = Field(default_factory=list)


def _default_candidates(registry: Registry) -> list[dict]:
    """Fall back to re-sourcing the CURRENT registry (refresh provenance).

    With no candidates in the payload, the agent re-sources the live catalog at the
    same prices — an in-band no-op refresh that auto-adopts, so the role always has
    something reviewable to produce.
    """
    return [
        {
            "id": s.id,
            "provider": s.provider,
            "tier": s.tier.value,
            "price_in": s.price_in,
            "price_out": s.price_out,
            "context_window": s.context_window,
            "task_fit": list(s.task_fit),
        }
        for s in registry.models.values()
    ]


def _resolve_candidates(task: Task, registry: Registry) -> list[dict]:
    """Resolve the proposed candidate model dicts from the task payload.

    Accepts ``candidates`` or ``models`` (a list of dicts). Empty/absent → a
    refresh of the current registry. Bounded to :data:`MAX_CANDIDATES`.
    """
    payload = task.payload or {}
    raw = payload.get("candidates") or payload.get("models")
    if not isinstance(raw, list) or not raw:
        raw = _default_candidates(registry)
    return [c for c in raw if isinstance(c, dict)][:MAX_CANDIDATES]


def _provenance_from(results: list[SearchResult]) -> list[str]:
    """The URLs the gateway actually returned — the evidence a candidate cites."""
    return [r.url for r in results[:DEFAULT_RESULTS] if getattr(r, "url", "")]


def _sources_digest(results: list[SearchResult]) -> str:
    """A compact titles/urls-only digest for the synthesis prompt (no snippets)."""
    if not results:
        return "(no sources)"
    return "; ".join(f"{r.title} <{r.url}>" for r in results[:DEFAULT_RESULTS])


def synthesize_candidate(
    candidate: dict,
    results: list[SearchResult],
    *,
    sourced_on: str,
) -> ModelSpec:
    """Build an evidence-grounded :class:`ModelSpec` from a candidate + its sources.

    Pure + deterministic (no DB/model/network). The ``provenance`` is derived from
    the *search results* (their URLs) + the sourcing date — NEVER copied from a
    bare ``provenance`` claim in ``candidate`` (evidence over claims, ADR-0014).
    """
    urls = _provenance_from(results)
    provenance = "sourcing: " + ("; ".join(urls[:3]) if urls else "(no corroborating source)")
    return ModelSpec(
        id=str(candidate["id"]),
        provider=str(candidate["provider"]),
        tier=Tier(candidate["tier"]),
        price_in=float(candidate["price_in"]),
        price_out=float(candidate["price_out"]),
        context_window=int(candidate.get("context_window", 0) or 0),
        cache_read_multiplier=float(
            candidate.get("cache_read_multiplier", ModelSpec.model_fields["cache_read_multiplier"].default)
        ),
        task_fit=list(candidate.get("task_fit", []) or []),
        provenance=provenance,
        provenance_date=sourced_on,
    )


def classify_candidate(
    spec: ModelSpec,
    registry: Registry,
    *,
    scope_affecting: bool = False,
) -> tuple[str, str]:
    """Classify one candidate against the current registry (the ADR-0005 envelope).

    Pure + deterministic. Returns ``(decision, reason)`` where ``decision`` is
    :data:`DECISION_APPROVAL` (🛑) or :data:`DECISION_AUTOADOPT` (📣):

    - a new provider, a new tier, or an explicit ``scope_affecting`` change →
      approval (objective/scope-affecting);
    - a higher input OR output price than the tier's current reference →
      approval (budget-increasing);
    - otherwise an in-band swap → auto-adopt.
    """
    if scope_affecting:
        return DECISION_APPROVAL, f"{spec.id}: objective/scope-affecting change"

    known_providers = {s.provider for s in registry.models.values()}
    if spec.provider not in known_providers:
        return DECISION_APPROVAL, f"{spec.id}: new provider {spec.provider!r} (needs approval)"

    ref = registry.get(spec.id)
    if ref is None:
        same = [
            s for s in registry.models.values()
            if s.provider == spec.provider and s.tier is spec.tier
        ]
        if same:
            ref = min(same, key=lambda s: s.price_in + s.price_out)
        else:
            tier_specs = registry.by_tier(spec.tier)
            if not tier_specs:
                return DECISION_APPROVAL, f"{spec.id}: introduces new tier {spec.tier.value!r} (scope)"
            ref = min(tier_specs, key=lambda s: s.price_in + s.price_out)

    if spec.price_in > ref.price_in or spec.price_out > ref.price_out:
        return (
            DECISION_APPROVAL,
            f"{spec.id}: budget-increasing vs {ref.id} "
            f"(in {ref.price_in}->{spec.price_in}, out {ref.price_out}->{spec.price_out})",
        )
    return DECISION_AUTOADOPT, f"{spec.id}: in-band swap within {ref.id}'s cost/quality band"


def provenance_hash(specs: list[ModelSpec]) -> str:
    """A stable, PII/secret-free digest of the proposal's provenance + ids.

    Over the sorted ``id|provenance`` pairs (not the date), so an event can cite a
    proposal's provenance without emitting the raw URLs (invariants 5 & 6).
    """
    material = "\n".join(sorted(f"{s.id}|{s.provenance}" for s in specs))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def render_candidate_yaml(
    decisions: list[CandidateDecision],
    *,
    proposal_decision: str,
    prov_hash: str,
    sourced_on: str,
) -> str:
    """Render the reviewable candidate proposal as YAML (the runtime PR analogue).

    A ``models:`` block of the proposed specs plus a header noting the date,
    decision, and provenance hash. NOT the live registry — review + adopt is a
    separate step.
    """
    models = []
    for d in decisions:
        m = d.spec.model_dump(mode="json")
        m["_decision"] = d.decision  # per-spec envelope outcome, for the reviewer
        models.append(m)
    body = yaml.safe_dump({"models": models}, sort_keys=False, allow_unicode=True)
    header = (
        "# AI Studio — PROPOSED model registry update (CANDIDATE; NOT the live registry).\n"
        f"# Produced by the Sourcing agent (ADR-0005) on {sourced_on}.\n"
        "# Review via the normal PR + review loop before adopting into runtime/models.yaml.\n"
        f"# decision: {proposal_decision}   provenance_hash: {prov_hash}\n"
        f"# candidates: {len(decisions)}\n"
    )
    return header + body


def _write_candidate(
    conn: Any,
    task: Task,
    content: str,
    path: str,
    *,
    tool_registry: ToolRegistry,
    policy: Optional[PolicyConfig],
    sink: EventSink,
    invoke_fn: Callable[..., Any],
) -> tuple[str, Optional[str]]:
    """Write the candidate proposal via the policy-gated filesystem tool.

    Returns ``(status, path)`` where ``status`` is the invoke outcome
    (``"executed"`` | ``"denied"`` | ``"pending"``). A role without ``fs.write`` is
    DENIED (nothing written) — a safe, logged no-op.
    """
    result = invoke_fn(
        role=ROLE,
        tool_name="filesystem",
        registry=tool_registry,
        config=policy,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        op="write",
        path=path,
        content=content,
    )
    status = getattr(result.status, "value", str(result.status))
    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and getattr(result.result, "ok", False)
    )
    return status, (path if wrote else None)


def run_sourcing(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    model_registry: Optional[Registry] = None,
    tool_registry: Optional[ToolRegistry] = None,
    policy: Optional[PolicyConfig] = None,
    k: int = DEFAULT_RESULTS,
    proposals_path: str = DEFAULT_PROPOSALS_PATH,
    search: Callable[..., list[SearchResult]] = _search,
    call_model: Callable[..., Any] = _call_model,
    invoke_fn: Callable[..., Any] = invoke,
    request_approval: Callable[..., Any] = _request_approval,
) -> SourcingResult:
    """Service one sourcing task: research → propose candidate → (🛑 | auto+📣).

    Resolves candidate model specs from ``task.payload`` (``candidates`` /
    ``models``; absent → a refresh of the current registry), gathers corroborating
    sources for each through the **policy-gated cached search gateway** (``net.fetch``,
    keyless dry-run), grounds each candidate's provenance in those sources, runs a
    traceability-only dry-run synthesis ``call_model``, classifies each candidate
    against the current registry (the ADR-0005 approval envelope), writes the
    reviewable candidate proposal via the policy-gated filesystem tool, and applies
    the envelope: a new-provider / new-tier / budget-increasing proposal raises a
    real 🛑 ``request_approval`` ("adopt model registry update"); an all-in-band
    proposal auto-adopts + emits 📣 ``sourcing.autoadopted``. Emits
    ``sourcing.proposed`` (ids / counts / provenance-hash / decision). Enqueues
    NOTHING (no loop) and NEVER mutates the live registry.

    Raises :class:`~runtime.search.SearchDenied` if the role lacks ``net.fetch`` —
    a genuine misconfiguration surfaced to the caller (nothing fetched/cached).
    ``search`` / ``call_model`` / ``invoke_fn`` / ``request_approval`` are injectable
    for tests; ``policy`` gates both the search and the candidate write.
    """
    sink = sink or NullEventSink()
    registry = model_registry or load_registry()
    scope_affecting = bool((task.payload or {}).get("scope_affecting"))
    sourced_on = date.today().isoformat()

    base = proposals_path.rsplit("/", 1)[-1]
    if base in _LIVE_REGISTRY_NAMES:
        raise ValueError(
            f"refusing to write a candidate to a live registry name: {proposals_path!r}"
        )

    candidates = _resolve_candidates(task, registry)

    # 1. Gather corroborating sources per candidate via the gateway ONLY (policy →
    #    cache → provider → cache; keyless). Provenance is grounded in what the
    #    gateway returned, never a bare claim.
    decisions: list[CandidateDecision] = []
    all_results: list[SearchResult] = []
    for cand in candidates:
        query = (
            f"{cand.get('id', '')} {cand.get('provider', '')} model pricing per token "
            "LMArena benchmark"
        ).strip()
        results = search(
            conn, ROLE, query, k=k, sink=sink,
            policy=policy, workstream=task.workstream, task_id=task.id,
        )
        all_results.extend(results)
        spec = synthesize_candidate(cand, results, sourced_on=sourced_on)
        decision, reason = classify_candidate(spec, registry, scope_affecting=scope_affecting)
        decisions.append(CandidateDecision(spec=spec, decision=decision, reason=reason))

    specs = [d.spec for d in decisions]
    prov_hash = provenance_hash(specs)
    model_ids = [s.id for s in specs]

    # 2. Traceability-only synthesis (dry-run, keyless). Digest is titles/urls only;
    #    the completion does NOT decide the proposal.
    call_model(
        role=ROLE,
        task_type="research",
        messages=[
            {
                "role": "user",
                "content": _SOURCING_PROMPT.format(
                    ids=", ".join(model_ids) or "(none)",
                    count=len(all_results),
                    digest=_sources_digest(all_results),
                ),
            }
        ],
        registry=model_registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    # 3. The proposal takes the stricter path if ANY candidate needs approval.
    new_providers = sorted(
        {d.spec.provider for d in decisions
         if d.decision == DECISION_APPROVAL and "new provider" in d.reason}
    )
    budget_increasing = [
        d.spec.id for d in decisions
        if d.decision == DECISION_APPROVAL and "budget-increasing" in d.reason
    ]
    needs_approval = any(d.decision == DECISION_APPROVAL for d in decisions)
    proposal_decision = DECISION_APPROVAL if needs_approval else DECISION_AUTOADOPT
    reasons = [d.reason for d in decisions]

    # 4. Write the reviewable candidate proposal via the policy-gated tool (never the
    #    live registry). Denied cleanly when the role lacks fs.write (safe no-op).
    candidate_status = "off"
    candidate_path: Optional[str] = None
    if tool_registry is not None:
        content = render_candidate_yaml(
            decisions, proposal_decision=proposal_decision,
            prov_hash=prov_hash, sourced_on=sourced_on,
        )
        candidate_status, candidate_path = _write_candidate(
            conn, task, content, proposals_path,
            tool_registry=tool_registry, policy=policy, sink=sink, invoke_fn=invoke_fn,
        )

    # 5. Apply the approval envelope.
    approval_id: Optional[str] = None
    autoadopted = False
    if needs_approval:
        # 🛑 adopt model registry update — a human decides (ADR-0005/0006). This is
        # the runtime analogue of a PR awaiting review; nothing is adopted yet.
        if conn is not None:
            approval = request_approval(
                conn,
                task_id=task.id,
                role=ROLE,
                tool="registry",
                capabilities=["spend.money"],
                tier=SOURCING_APPROVAL_TIER,
                reason="adopt model registry update: " + "; ".join(reasons[:4]),
                sink=sink,
                workstream=task.workstream,
            )
            approval_id = str(approval.id) if approval is not None else None
    else:
        # 📣 in-band swap — auto-adopted within the approved cost/quality band.
        autoadopted = True
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_SOURCING_AUTOADOPTED,
                task_id=task.id,
                payload={
                    "model_ids": model_ids,
                    "candidate_count": len(decisions),
                    "provenance_hash": prov_hash,
                    "decision": proposal_decision,
                },
            )
        )

    # 6. Emit sourcing.proposed — ids / counts / provenance-hash / decision only.
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_SOURCING_PROPOSED,
            task_id=task.id,
            payload={
                "model_ids": model_ids,
                "candidate_count": len(decisions),
                "provenance_hash": prov_hash,
                "decision": proposal_decision,
                "new_provider_count": len(new_providers),
                "budget_increasing_count": len(budget_increasing),
                "candidate_written": bool(candidate_path),
                "autoadopted": autoadopted,
            },
        )
    )

    return SourcingResult(
        candidate_count=len(decisions),
        model_ids=model_ids,
        provenance_hash=prov_hash,
        decision=proposal_decision,
        reasons=reasons,
        candidate_status=candidate_status,
        candidate_path=candidate_path,
        approval_id=approval_id,
        autoadopted=autoadopted,
        new_providers=new_providers,
        budget_increasing_ids=budget_increasing,
    )
