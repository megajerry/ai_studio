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

from .checkers import (
    DEFAULT_REGISTRY,
    ArtifactRef,
    Checker,
    CheckerRegistry,
    CheckResult,
    UnknownChecker,
    default_registry,
    marker_check,
    resolve_criterion,
)
from .critic import (
    Concern,
    Critique,
    assess_concerns,
    decide as critic_decide,
    run_critic,
)
from .executor import ExecutorResult, run_executor
from .lessons import compose_lessons, inject_lessons, recall_lesson_texts
from .pm import PlanResult, run_pm_tick
from .prompt import compose_role_prompt
from .researcher import (
    RESEARCH_TASK_TYPE,
    ResearchResult,
    distill_findings,
    run_research,
)
from .retro import RetroResult, distill_lessons, run_retro
from .sourcing import (
    SOURCING_TASK_TYPES,
    CandidateDecision,
    SourcingResult,
    classify_candidate,
    run_sourcing,
    synthesize_candidate,
)
from .verifier import VerifyResult, verify

__all__ = [
    "PlanResult",
    "run_pm_tick",
    "Concern",
    "Critique",
    "run_critic",
    "assess_concerns",
    "critic_decide",
    "ExecutorResult",
    "run_executor",
    "VerifyResult",
    "verify",
    "RetroResult",
    "run_retro",
    "distill_lessons",
    "ResearchResult",
    "run_research",
    "distill_findings",
    "RESEARCH_TASK_TYPE",
    "SourcingResult",
    "CandidateDecision",
    "run_sourcing",
    "classify_candidate",
    "synthesize_candidate",
    "SOURCING_TASK_TYPES",
    "compose_lessons",
    "inject_lessons",
    "recall_lesson_texts",
    "compose_role_prompt",
    "CheckResult",
    "ArtifactRef",
    "Checker",
    "CheckerRegistry",
    "UnknownChecker",
    "marker_check",
    "resolve_criterion",
    "default_registry",
    "DEFAULT_REGISTRY",
]
