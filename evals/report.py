"""Assemble the eval results into one report (dict / JSON / markdown).

Kept rendering-only so the individual evals stay independently runnable and
testable; this module just collects their ``to_dict()`` outputs and the telemetry
:func:`runtime.quality.quality_report` into a single, printable artifact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional


def build_report(
    verifier: dict, pm: Optional[dict], quality: Optional[dict]
) -> dict:
    """Combine the eval outputs into one report dict (+ generated-at stamp)."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "harness": "evaluation-harness-v1",
        "mode": "dry-run/keyless (mechanism + structural quality; outcome quality deferred to go-live)",
        "verifier_seeded_defect": verifier,
        "pm_structural_decomposition": pm,
        "telemetry_quality_report": quality,
    }


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def render_markdown(report: dict) -> str:
    """Render a compact human-readable markdown summary of a report dict."""
    lines: list[str] = []
    lines.append("# AI Studio — Evaluation Harness v1 report")
    lines.append("")
    lines.append(f"_Generated: {report.get('generated_at')}_  ")
    lines.append(f"_Mode: {report.get('mode')}_")
    lines.append("")

    v = report.get("verifier_seeded_defect")
    if v:
        cm = v["confusion"]
        lines.append("## Verifier seeded-defect precision/recall")
        lines.append("")
        lines.append(f"- cases: {cm['support']} (defective/positive: {cm['positives']})")
        lines.append(
            f"- **precision={cm['precision']}  recall={cm['recall']}  "
            f"f1={cm['f1']}  accuracy={cm['accuracy']}**"
        )
        lines.append(
            f"- confusion: tp={cm['tp']} fp={cm['fp']} fn={cm['fn']} tn={cm['tn']} "
            "(positive class = defective)"
        )
        lines.append(f"- overall passed: {v.get('passed')}")
        lines.append("")
        lines.append("| case | check | expected_pass | predicted_pass | correct |")
        lines.append("| --- | --- | --- | --- | --- |")
        for c in v["cases"]:
            lines.append(
                f"| {c['name']} | {c['check']} | {c['expected_pass']} | "
                f"{c['predicted_pass']} | {c['correct']} |"
            )
        lines.append("")

    p = report.get("pm_structural_decomposition")
    if p:
        lines.append("## PM structural decomposition")
        lines.append("")
        lines.append(f"- goals evaluated: {p.get('num_goals')}; overall passed: {p.get('passed')}")
        lines.append("")
        lines.append("| goal | decision | items | criteria | acyclic | deps_sane | passed |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for c in p["cases"]:
            lines.append(
                f"| {c['goal']} | {c['decision']} | {c['num_items']} | "
                f"{c['all_items_have_criteria']} | {c['dag_acyclic']} | "
                f"{c['deps_sane']} | {c['passed']} |"
            )
        lines.append("")

    q = report.get("telemetry_quality_report")
    if q:
        lines.append("## Telemetry quality rollup")
        ws = q.get("workstream") or "(all workstreams)"
        lines.append("")
        lines.append(f"- workstream: {ws}")
        t, r = q["totals"], q["rates"]
        lines.append(
            f"- tasks: merged={t['tasks_merged']} abandoned={t['tasks_abandoned']} "
            f"| verify passed={t['verify_passed']} failed={t['verify_failed']} "
            f"| rekicks={t['rekicks']} | model_calls={t['model_calls']}"
        )
        lines.append(
            f"- rates: success={_fmt(r['task_success_rate'])} "
            f"verify_pass={_fmt(r['verify_pass_rate'])} "
            f"rekick={_fmt(r['rekick_rate'])} error={_fmt(r['error_rate'])}"
        )
        lines.append(
            f"- per completed task: cost_usd="
            f"{_fmt(q['cost']['avg_cost_per_completed_task_usd'])} "
            f"tokens={_fmt(q['cost']['avg_tokens_per_completed_task'])} "
            f"latency_ms={_fmt(q['latency']['avg_latency_per_completed_task_ms'])}"
        )
        lines.append("")

    return "\n".join(lines)


def to_json(report: dict) -> str:
    """Serialize a report dict to indented JSON (default=str for UUIDs etc.)."""
    return json.dumps(report, indent=2, default=str, sort_keys=False)
