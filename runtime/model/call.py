"""The single instrumented model-call site (ADR-0012).

:func:`call_model` is THE place a model is ever called. Everything an agent needs
from an LLM goes through it; **agents never call a provider adapter directly**
(that would bypass routing, cost accounting, and the event log — the exact
ad-hoc call ADR-0012 forbids). One call does, in order:

    1. route  — pick a model from the registry policy (emits ``model.routed``).
    2. budget — with a ``conn``, gate the call against the workstream's caps
                (:func:`runtime.budget.enforce`): if a real (or dry-run) call would
                exceed a cap it emits ``budget.exceeded``, raises a 🛑 approval, and
                raises :class:`~runtime.budget.OverBudget` — the call does NOT run.
    3. select — pick the provider adapter, or dry-run if forced / key absent.
    4. complete — run the provider (dry-run needs no network/key). If the provider
                signals a recoverable failure (``ProviderFallback``) we reassign to
                the next (typically pricier) model in the tier chain and **re-run
                the step-2 budget gate against that FALLBACK spec** before retrying,
                so a fallback that would breach the cap is blocked too (not merely
                accounted post-hoc). An in-budget fallback proceeds unchanged.
    5. cost   — compute cost = tokens × registry price (:func:`registry.cost_usd`).
    6. emit   — a ``model.call`` event: model, provider, role, task_id, in/out/
                cached tokens, cost_usd, latency_ms.
    7. account — if a ``conn`` + ``task_id`` are given, add the call's tokens to
                that task's ``spent_tokens`` (budget telemetry).

Dry-run is the default whenever ``MODELS_DRY_RUN=1`` or the selected model's
provider has no wired adapter / no key present, so the whole path runs keyless.
Secrets are read only inside providers and are never placed on the event.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional
from uuid import UUID

from ..budget import PURPOSE_NORMAL
from ..enforce import EventSink, NullEventSink
from ..models import make_event
from ..event_types import EVENT_MODEL_CALL, EVENT_MODEL_CALL_FAILED
from ..policy import BudgetContext
from .providers import (
    Completion,
    DryRunProvider,
    Message,
    Provider,
    ProviderFallback,
    get_adapter,
)
from .registry import ModelSpec, Registry, Usage, cost_usd, load_registry
from .router import next_candidate, route

#: The instrumented model-call event type (``model.call``, ADR-0012) is imported
#: from the canonical :mod:`runtime.event_types`.

_DRY_RUN_ENV = "MODELS_DRY_RUN"


def _dry_run_forced() -> bool:
    return os.environ.get(_DRY_RUN_ENV, "").strip() in {"1", "true", "yes", "on"}


def _emit_call_failed(
    sink: EventSink,
    exc: BaseException,
    spec: ModelSpec,
    provider: Provider,
    role: str,
    task_type: str,
    task_id: Optional[UUID],
    workstream: str,
    trace_id: Optional[str],
    span_id: Optional[str],
) -> None:
    """Emit a BODY-FREE ``model.call.failed`` for a provider death (ADR-0023).

    Carries ONLY the error CLASS name + model/provider/role/task_id so an API-error
    death becomes attributable telemetry (R3 consumes it) — NEVER the exception
    message, prompt, response, or any secret text (invariants 5 & 6). We use
    ``type(exc).__name__`` and deliberately NOT ``str(exc)``, which can echo request
    bodies or keys.
    """
    sink.emit(
        make_event(
            workstream=workstream,
            type=EVENT_MODEL_CALL_FAILED,
            task_id=task_id,
            trace_id=trace_id,
            span_id=span_id,
            payload={
                "error_type": type(exc).__name__,
                "model": spec.id,
                "provider": provider.name,
                "role": role,
                "task_type": task_type,
                "task_id": str(task_id) if task_id else None,
            },
        )
    )


def select_provider(spec: ModelSpec, *, force_dry_run: bool = False) -> Provider:
    """Pick the provider that will serve ``spec``.

    Dry-run wins when it is forced (arg or ``MODELS_DRY_RUN``), when no adapter is
    wired for the model's provider, or when the adapter's key is absent. This is
    what makes the studio run fully keyless while real providers activate the
    moment a key appears.
    """
    if force_dry_run or _dry_run_forced():
        return DryRunProvider()
    adapter = get_adapter(spec.provider)
    if adapter is None or not adapter.available():
        return DryRunProvider()
    return adapter


def call_model(
    role: str,
    task_type: str,
    messages: list[Message],
    *,
    quality: str = "standard",
    registry: Optional[Registry] = None,
    budget_ctx: Optional[BudgetContext] = None,
    latency: Optional[str] = None,
    task_id: Optional[UUID] = None,
    conn: Any = None,
    sink: Optional[EventSink] = None,
    workstream: str = "productivity",
    purpose: str = PURPOSE_NORMAL,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    force_dry_run: bool = False,
    **opts: Any,
) -> Completion:
    """Route, call, cost, and instrument a single model call (see module docstring).

    Returns the provider :class:`Completion`. Emits ``model.routed`` (via the
    router) and ``model.call`` through ``sink`` (defaults to a no-op sink;
    production passes a :class:`~runtime.enforce.DbEventSink`). If ``conn`` and
    ``task_id`` are both given, the call's total tokens are added to the task's
    ``spent_tokens``; without a ``conn`` the DB step is simply skipped, so the
    wrapper is fully usable with no database.

    ``purpose`` (``normal`` (default) | ``wind_down`` | ``escalation``) is threaded
    into :func:`runtime.budget.enforce`: the graduated engine (ADR-0022 C1) permits
    a ``wind_down`` / ``escalation`` call to spend the reserve buffer near the cap
    while withholding a ``normal`` one, so a role that is winding down / escalating
    for more budget can still act BEFORE breaching. Existing callers omit it and get
    ``normal`` — behavior-preserving.
    """
    if registry is None:
        registry = load_registry()
    sink = sink or NullEventSink()

    # 1. route (emits model.routed). May raise OverBudget — surfaced to caller.
    spec = route(
        task_type,
        quality,
        registry=registry,
        budget_ctx=budget_ctx,
        latency=latency,
        sink=sink,
        workstream=workstream,
        task_id=task_id,
        trace_id=trace_id,
        span_id=span_id,
    )

    # 2. budget — gate against the workstream's real accrued spend BEFORE spending.
    #    No-op without a conn or when the workstream has no cap set; over cap it
    #    raises budget.OverBudget + a 🛑 approval (ADR-0006) — the call never runs.
    #    When it ALLOWS, enforce RESERVES this call's estimate (ADR-0016) so
    #    concurrent gates see it and can't collectively breach the cap; that
    #    reservation is provisional and MUST be released once real spend is recorded
    #    (or the call fails/aborts), so we track every reservation made and release
    #    them all in the finally below — no leaked reservation shrinks the cap.
    reservations: list[tuple[float, int]] = []
    try:
        if conn is not None:
            from ..budget import enforce as _enforce_budget
            from ..budget import estimate_call_io_tokens

            # Price input AND output tokens separately (output usually bills higher):
            # a pre-call figure that priced the whole token sum at the input rate
            # systematically under-counted and could let a call slip past a cap.
            est_input, est_output = estimate_call_io_tokens(messages)
            est_tokens = est_input + est_output
            est_usd = cost_usd(
                spec, Usage(input_tokens=est_input, output_tokens=est_output)
            )
            # Raises OverBudget when blocked (nothing reserved → nothing to release);
            # on ALLOW it reserved (est_usd, est_tokens) — record it for release.
            _enforce_budget(
                conn,
                workstream,
                est_usd=est_usd,
                est_tokens=est_tokens,
                purpose=purpose,
                role=role,
                task_id=task_id,
                sink=sink,
            )
            reservations.append((est_usd, est_tokens))

        # 3. select provider (dry-run if forced / no adapter / no key).
        provider = select_provider(spec, force_dry_run=force_dry_run)

        # 4. complete, timing the call for latency telemetry. If the provider signals
        #    a recoverable failure (ProviderFallback — e.g. the Cursor CLI hangs and
        #    hits its timeout), walk the routed tier's data-driven chain to the next
        #    present model (a metered fallback) and retry there ONCE, so coding/agentic
        #    work is never blocked on the flat-rate substrate. This is pure provider
        #    dispatch: routing/cost/budget accounting below still use the model that
        #    actually served the call.
        start = time.monotonic()
        try:
            completion = provider.complete(spec.id, messages, **opts)
        except ProviderFallback:
            fallback = next_candidate(registry, spec.tier, spec.id)
            if fallback is None:
                raise
            spec = fallback
            # Re-gate the retry against the FALLBACK spec's cost before spending on it
            # (behavior change — see module docstring step 4 / ADR-0022/0006). The
            # pre-spend enforce above ran against the ROUTED spec; a ProviderFallback
            # reassigns to a (typically pricier) fallback model, so without re-checking
            # a fallback that breaches the cap would run and only be accounted after
            # the fact. Re-running enforce here gates it exactly like a first-choice
            # over-cap call: it emits budget.exceeded, raises the 🛑 "raise budget"
            # approval, and raises OverBudget — the retry never runs. A fallback that
            # is within budget is unaffected (enforce simply returns and it proceeds).
            if conn is not None:
                est_usd = cost_usd(
                    spec, Usage(input_tokens=est_input, output_tokens=est_output)
                )
                _enforce_budget(
                    conn,
                    workstream,
                    est_usd=est_usd,
                    est_tokens=est_tokens,
                    purpose=purpose,
                    role=role,
                    task_id=task_id,
                    sink=sink,
                )
                reservations.append((est_usd, est_tokens))
            provider = select_provider(spec, force_dry_run=force_dry_run)
            try:
                completion = provider.complete(spec.id, messages, **opts)
            except Exception as exc:
                # The metered fallback also died — attributable telemetry, then re-raise.
                _emit_call_failed(sink, exc, spec, provider, role, task_type, task_id,
                                  workstream, trace_id, span_id)
                raise
        except Exception as exc:
            # A provider death that is NOT the recoverable ProviderFallback signal: emit
            # body-free failure telemetry (R3) before re-raising. Success path unchanged.
            _emit_call_failed(sink, exc, spec, provider, role, task_type, task_id,
                              workstream, trace_id, span_id)
            raise
        latency_ms = int((time.monotonic() - start) * 1000)

        # 5. cost = tokens × registry price (single source of cost truth).
        cost = cost_usd(spec, completion.usage)

        # 6. emit the model.call event. Only numbers + ids — never prompt/secret text.
        sink.emit(
            make_event(
                workstream=workstream,
                type=EVENT_MODEL_CALL,
                task_id=task_id,
                trace_id=trace_id,
                span_id=span_id,
                payload={
                    "model": spec.id,
                    "provider": provider.name,
                    "role": role,
                    "task_type": task_type,
                    "task_id": str(task_id) if task_id else None,
                    "input_tokens": completion.usage.input_tokens,
                    "output_tokens": completion.usage.output_tokens,
                    "cached_tokens": completion.usage.cached_tokens,
                    "cost_usd": cost,
                    "latency_ms": latency_ms,
                },
            )
        )

        # 7. account: add this call's tokens to the task's running spend (if wired).
        if conn is not None and task_id is not None:
            from ..tasks import add_spent_tokens

            add_spent_tokens(conn, task_id, completion.usage.total_tokens)

        return completion
    finally:
        # Release every in-flight reservation this call made (ADR-0016): the real
        # spend is now recorded in the model.call event (source of truth) — or the
        # call failed/aborted and never spent. Either way the provisional cushion
        # must be returned so it doesn't permanently shrink the cap. Runs on the
        # success path, on OverBudget from the fallback re-gate, and on a provider
        # death alike. (An OverBudget from the FIRST enforce reserved nothing, so
        # `reservations` is empty and this is a no-op.)
        if conn is not None and reservations:
            from ..budget import release_reservation

            for r_usd, r_tokens in reservations:
                release_reservation(
                    conn, workstream, est_usd=r_usd, est_tokens=r_tokens
                )
