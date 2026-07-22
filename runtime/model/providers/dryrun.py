"""Dry-run provider — runs the whole model path with NO network and NO key.

This is the default whenever ``MODELS_DRY_RUN=1`` or the selected model's real
provider key is absent, so the studio boots and every code path (route → call →
cost → events → spent_tokens) exercises end-to-end keyless. It returns a
deterministic stub text and SYNTHETIC token counts derived purely from the input
length, so tests and cost math are reproducible.

**Planning calls** (the PM's confidence gate, ADR-0003): when a call carries a
``plan_goal`` option (the PM sets ``call_model(..., task_type="plan",
plan_goal=goal)``), the completion text is a DETERMINISTIC, PARSEABLE structured
plan (JSON matching :class:`runtime.roles.pm.Plan`) — the goal split into 2–3
work items, each with its own marker-based, independently checkable success
criterion. This lets the whole PM decomposition path (parse → confidence gate →
decompose → enqueue N work items) run keyless; a real model wired later returns
the same schema from the natural-language prompt, so no PM code changes. All
other dry-run behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..registry import Usage
from .base import Completion, Message, messages_char_len

#: Rough chars-per-token used to synthesize input token counts (~4 is typical
#: for English). Only needs to be a stable, plausible constant.
_CHARS_PER_TOKEN = 4
#: Synthetic output tokens as a fraction of synthetic input tokens.
_OUTPUT_RATIO = 4  # output ~= input // 4

#: Option key the PM sets on a planning call to request a structured plan (above).
PLAN_GOAL_OPT = "plan_goal"


def _goal_digest(goal: str) -> str:
    """A short, stable fingerprint of the goal — the marker namespace for a plan."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:8]


def build_dry_run_plan(goal: str) -> dict[str, Any]:
    """Deterministically decompose ``goal`` into a 2–3 item plan (dict form).

    Pure + reproducible: the same goal always yields the same plan. Each work item
    carries a unique, marker-based, independently checkable success criterion
    (``studio-ok:<goal-digest>:<i>``) so the Executor/Verifier contract flows
    through unchanged — the Verifier still checks a real artifact against a real
    marker. The shape matches :class:`runtime.roles.pm.Plan`.
    """
    goal = (goal or "").strip() or "unspecified goal"
    digest = _goal_digest(goal)
    # 2 or 3 items, chosen deterministically from the goal (>1 so decomposition is
    # always visible/observable in the demo + tests).
    n_items = 2 + (int(digest, 16) % 2)
    short = goal if len(goal) <= 80 else goal[:77] + "..."

    work_items = []
    for i in range(1, n_items + 1):
        marker = f"studio-ok:{digest}:{i}"
        work_items.append(
            {
                "title": f"Part {i}/{n_items}: {short}",
                "type": "work.task",
                "instructions": (
                    f"Produce the artifact for part {i} of {n_items} of the goal: {goal}"
                ),
                "success_criterion": (
                    f"The part {i} artifact exists and contains the marker {marker!r}."
                ),
                "marker": marker,
            }
        )

    return {
        "restated_goal": goal,
        "success_criteria": [
            f"All {n_items} work items complete and each artifact contains its marker.",
        ],
        "confidence": 0.9,
        "feasible": True,
        "reason": "dry-run deterministic decomposition",
        "work_items": work_items,
    }


class DryRunProvider:
    """A keyless, networkless provider producing deterministic stubs."""

    name = "dryrun"

    def available(self) -> bool:
        # Always available — that is the whole point.
        return True

    def complete(
        self, model_id: str, messages: list[Message], **opts: Any
    ) -> Completion:
        # Planning call → return a deterministic, PARSEABLE structured plan derived
        # from the goal (the PM decomposition path, ADR-0003). Keyed on the presence
        # of the plan_goal option so all other dry-run calls are unchanged.
        plan_goal = opts.get(PLAN_GOAL_OPT)
        if isinstance(plan_goal, str) and plan_goal.strip():
            text = json.dumps(build_dry_run_plan(plan_goal))
            input_tokens = max(1, messages_char_len(messages) // _CHARS_PER_TOKEN)
            output_tokens = max(1, len(text) // _CHARS_PER_TOKEN)
            return Completion(
                text=text,
                usage=Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=0,
                ),
                model_id=model_id,
                provider=self.name,
                raw={"dry_run": True, "plan": True},
            )

        chars = messages_char_len(messages)
        input_tokens = max(1, chars // _CHARS_PER_TOKEN)
        output_tokens = max(1, input_tokens // _OUTPUT_RATIO)
        # A short, deterministic fingerprint so identical inputs yield identical
        # stub text (useful for snapshotting) without echoing the prompt.
        digest = hashlib.sha256(
            f"{model_id}:{chars}".encode("utf-8")
        ).hexdigest()[:12]
        text = f"[dry-run:{model_id}] stub completion ({digest})"
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=0,
        )
        return Completion(
            text=text,
            usage=usage,
            model_id=model_id,
            provider=self.name,
            raw={"dry_run": True},
        )
