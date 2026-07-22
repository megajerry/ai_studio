"""Roles — the studio's operating agents (M3c).

A role is, per architecture §3, ``prompt + skills + tools``. Today each role is a
small typed function plus an inline prompt template; it acts on the world ONLY
through the merged runtime seams:

- **tools** via the policy-gated :func:`runtime.enforce.invoke` (never a tool's
  ``execute`` directly);
- **models** via :func:`runtime.model.call.call_model` (the single instrumented
  call site);
- **coordination** via the task queue + event log (:mod:`runtime.tasks` /
  :mod:`runtime.events`) — roles never call each other.

The three roles form the agent-driven loop (architecture §4, ADR-0004):

    PM (plan + confidence gate) --enqueue--> work task
        --> Executor (do the work via a tool + a model)
        --> Verifier (independent verify→commit gate)

The **skills** layer (Agent Skills standard, ADR-0008) now exists in
:mod:`runtime.skills`: a role can pull relevant, reviewed skills from a
``SkillRegistry`` and compose them into its base prompt on demand (the PM's
``run_pm_tick`` takes an optional ``skills=`` registry). The base persona is
still an inline string template; skills are injected on top of it.
"""

from __future__ import annotations

from .executor import ExecutorResult, run_executor
from .pm import PlanResult, run_pm_tick
from .verifier import VerifyResult, verify

__all__ = [
    "PlanResult",
    "run_pm_tick",
    "ExecutorResult",
    "run_executor",
    "VerifyResult",
    "verify",
]
