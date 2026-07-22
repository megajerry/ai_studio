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
    4. complete — run the provider (dry-run needs no network/key).
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

from ..enforce import EventSink, NullEventSink
from ..models import make_event
from ..policy import BudgetContext
from .providers import Completion, DryRunProvider, Message, Provider, get_adapter
from .registry import ModelSpec, Registry, Usage, cost_usd, load_registry
from .router import route

#: Canonical event type for one instrumented model call (ADR-0012).
EVENT_MODEL_CALL = "model.call"

_DRY_RUN_ENV = "MODELS_DRY_RUN"


def _dry_run_forced() -> bool:
    return os.environ.get(_DRY_RUN_ENV, "").strip() in {"1", "true", "yes", "on"}


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
    if conn is not None:
        from ..budget import enforce as _enforce_budget
        from ..budget import estimate_call_tokens

        est_tokens = estimate_call_tokens(messages)
        est_usd = cost_usd(spec, Usage(input_tokens=est_tokens))
        _enforce_budget(
            conn,
            workstream,
            est_usd=est_usd,
            est_tokens=est_tokens,
            role=role,
            task_id=task_id,
            sink=sink,
        )

    # 3. select provider (dry-run if forced / no adapter / no key).
    provider = select_provider(spec, force_dry_run=force_dry_run)

    # 4. complete, timing the call for latency telemetry.
    start = time.monotonic()
    completion = provider.complete(spec.id, messages, **opts)
    latency_ms = int((time.monotonic() - start) * 1000)

    # 4. cost = tokens × registry price (single source of cost truth).
    cost = cost_usd(spec, completion.usage)

    # 5. emit the model.call event. Only numbers + ids — never prompt/secret text.
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

    # 6. account: add this call's tokens to the task's running spend (if wired).
    if conn is not None and task_id is not None:
        from ..tasks import add_spent_tokens

        add_spent_tokens(conn, task_id, completion.usage.total_tokens)

    return completion
