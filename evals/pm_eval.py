"""Structural eval for the PM's goal decomposition (ADR-0003 / ADR-0015).

Real OUTCOME quality of a plan (are these the *right* work items?) needs a real
model and is deferred to go-live. What is measurable **now**, dry-run, is whether
the PM emits a *structurally well-formed* decomposition — which is exactly what the
downstream fleet relies on. We run the (dry-run) PM on labeled goals and score:

- **produces_items** — at least one work item (the PM actually decomposed);
- **all_items_have_criteria** — every item carries a concrete, checkable criterion
  (so the Verifier can judge a real artifact);
- **dag_acyclic** — the dependency graph the PM emitted is acyclic (no work item can
  wait on itself, directly or transitively);
- **deps_sane** — every dependency references a real sibling item (no dangling /
  self edges).

:func:`score_decomposition` is a pure scorer (no DB) so a deliberately BAD
decomposition can be unit-tested to prove the eval FLAGS it. :func:`run_pm_structural_eval`
runs the real :func:`runtime.roles.pm.run_pm_tick` with a capturing (no-DB)
enqueue, so the actual PM planning path is exercised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

from runtime.enforce import NullEventSink
from runtime.models import Task, TaskStatus
from runtime.roles.pm import run_pm_tick
from runtime.task_state import DependencyCycle, assert_acyclic

#: Labeled goals the PM should be able to decompose into a well-formed plan.
DEFAULT_GOALS: list[str] = [
    "Prove the studio operates end-to-end in dry-run.",
    "Launch a weekly short-form video channel with captioned clips.",
    "Stand up a lead-capture landing page and a follow-up email.",
]


def score_decomposition(items: list[dict]) -> dict:
    """Score the structural quality of a decomposition (pure; no I/O).

    ``items`` is the list of enqueued work items, each a dict with ``id`` (a UUID),
    ``payload`` (carrying ``criterion``), and ``depends_on`` (a list of sibling
    ids). Returns the per-property booleans plus an overall ``passed``. This is the
    single scorer both the live eval and its tests use, so a planted-bad plan is
    flagged by the same logic the harness runs.
    """
    n = len(items)
    ids = [it["id"] for it in items]
    id_set = set(ids)

    all_criteria = n > 0 and all(
        str((it.get("payload") or {}).get("criterion", "")).strip() for it in items
    )

    deps_sane = True
    edges: dict[Any, list] = {}
    for it in items:
        deps = list(it.get("depends_on") or [])
        edges[it["id"]] = deps
        for d in deps:
            if d == it["id"] or d not in id_set:
                deps_sane = False

    try:
        assert_acyclic(edges)
        acyclic = True
    except DependencyCycle:
        acyclic = False

    passed = bool(n >= 1 and all_criteria and acyclic and deps_sane)
    return {
        "num_items": n,
        "produces_items": n >= 1,
        "all_items_have_criteria": bool(all_criteria),
        "dag_acyclic": acyclic,
        "deps_sane": deps_sane,
        "passed": passed,
    }


def _capturing_enqueue(captured: list[dict]):
    """A fake ``enqueue`` that records each work item and returns a UUID-bearing
    stub — so :func:`run_pm_tick` exercises its full decomposition path (markers,
    edge mapping) without writing any task rows."""

    def enqueue(conn: Any, *, workstream: str, type: str, payload: Optional[dict] = None,
                priority: int = 0, assignee: Any = None, budget_tokens: Any = None,
                depends_on: Any = None, trajectory_id: Any = None) -> Any:
        tid = uuid4()
        captured.append({
            "id": tid,
            "type": type,
            "payload": payload or {},
            "depends_on": list(depends_on or []),
        })
        return SimpleNamespace(id=tid)

    return enqueue


def _fake_approval(conn: Any, **kwargs: Any) -> Any:
    """A stub approval (only reached if the PM pushes back) — never a real 🛑 write."""
    return SimpleNamespace(id=uuid4())


def _pm_task(ws: str, goal: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(), workstream=ws, type="pm.tick", status=TaskStatus.IN_PROGRESS,
        priority=0, payload={"goal": goal}, created_at=now, updated_at=now,
    )


@dataclass
class PMEvalResult:
    """Outcome of the PM structural eval across the labeled goals."""

    cases: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.cases) and all(c["passed"] for c in self.cases)

    def to_dict(self) -> dict:
        return {
            "name": "pm_structural_decomposition",
            "description": (
                "Structural quality of the (dry-run) PM's decomposition on labeled "
                "goals: >=1 item, per-item criteria, acyclic DAG, sane deps."
            ),
            "num_goals": len(self.cases),
            "cases": self.cases,
            "passed": self.passed,
        }


def run_pm_structural_eval(
    conn: Any, goals: Optional[list[str]] = None
) -> PMEvalResult:
    """Run the real dry-run PM on ``goals`` and score each decomposition.

    Needs a live ``conn`` (the PM recalls lessons + records its model call through
    it); keyless via ``MODELS_DRY_RUN``. Enqueue/approval are stubbed so no task
    rows are written — only the model.call telemetry for the throwaway eval
    workstream lands, which is expected.
    """
    os.environ.setdefault("MODELS_DRY_RUN", "1")
    goals = goals or DEFAULT_GOALS
    cases: list[dict] = []
    for goal in goals:
        ws = f"eval-pm-{uuid4().hex[:8]}"
        captured: list[dict] = []
        plan = run_pm_tick(
            conn, _pm_task(ws, goal), NullEventSink(),
            enqueue=_capturing_enqueue(captured),
            request_approval=_fake_approval,
        )
        score = score_decomposition(captured)
        cases.append({
            "goal": goal if len(goal) <= 80 else goal[:77] + "...",
            "decision": plan.decision,
            "confidence": plan.confidence,
            **score,
            # Structurally OK AND the PM actually chose to plan (not clarify/pushback).
            "passed": bool(score["passed"] and plan.decision == "planned"),
        })
    return PMEvalResult(cases=cases)
