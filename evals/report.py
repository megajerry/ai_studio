"""Assemble the eval results into one report (dict / JSON / markdown) — harness v2.

Kept rendering-only so the individual evals stay independently runnable and
testable; this module collects their ``to_dict()`` outputs and the telemetry
:func:`runtime.quality.quality_report` into a single, printable artifact.

v2 change: **every rate is rendered with its sample size ``n`` and a Wilson 95%
confidence interval**, and small samples (``n < 30``) are flagged INSUFFICIENT — so
a bare ``precision=1.0`` can never again read as a trustworthy quality estimate. The
telemetry rollup's proportions are given CIs here (computed from the counts the
rollup already returns; :mod:`runtime.quality` is not touched, keeping the tracks
independent).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from .stats import Rate, rate

HARNESS_VERSION = "evaluation-harness-v2"


def telemetry_rates(quality: dict) -> list[Rate]:
    """Wilson-CI rates for the telemetry rollup's binomial proportions.

    Built from the counts ``quality_report`` already returns (no re-query, no edit to
    :mod:`runtime.quality`). ``rekick_rate`` is deliberately excluded: re-kicks per
    terminal task is a per-task RATIO (can exceed 1), not a proportion, so a Wilson
    interval would misrepresent it — it stays a bare count in ``totals``."""
    t = quality.get("totals", {})
    merged = int(t.get("tasks_merged", 0))
    abandoned = int(t.get("tasks_abandoned", 0))
    terminal = int(t.get("tasks_terminal", merged + abandoned))
    vp = int(t.get("verify_passed", 0))
    vf = int(t.get("verify_failed", 0))
    return [
        rate("task_success_rate", merged, terminal),
        rate("verify_pass_rate", vp, vp + vf),
        rate("error_rate", abandoned + vf, terminal + vp + vf),
    ]


def build_report(
    verifier: dict,
    pm: Optional[dict],
    quality: Optional[dict],
    trajectory: Optional[dict] = None,
    grounding: Optional[dict] = None,
) -> dict:
    """Combine the eval outputs into one report dict (+ generated-at stamp).

    Telemetry proportions are augmented with Wilson CIs under
    ``telemetry_quality_report['rates_ci']`` so every reported rate carries n+CI."""
    if quality is not None:
        quality = {**quality, "rates_ci": [r.to_dict() for r in telemetry_rates(quality)]}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "harness": HARNESS_VERSION,
        "mode": (
            "dry-run/keyless — mechanism + structural quality measured now; "
            "real OUTCOME quality (swappable judge on a real model) slots in at "
            "go-live with no code change. Every rate carries n + Wilson 95% CI."
        ),
        "verifier_seeded_defect": verifier,
        "pm_structural_decomposition": pm,
        "pm_trajectory_decision_quality": trajectory,
        "grounding_fabrication_telemetry": grounding,
        "telemetry_quality_report": quality,
    }


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def _rate_line(r: dict) -> str:
    """Render a rate dict (from :meth:`evals.stats.Rate.to_dict`) as one bullet."""
    lo, hi = r["ci95"]
    flag = "  **INSUFFICIENT (n<30)**" if r.get("insufficient_sample") else ""
    val = _fmt(r.get("value"))
    return f"- **{r['label']}={val}**  n={r['n']}  95%CI=[{lo}, {hi}]{flag}"


def render_markdown(report: dict) -> str:
    """Render a compact human-readable markdown summary of a report dict."""
    lines: list[str] = []
    lines.append("# AI Studio — Evaluation Harness v2 report")
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
        for r in v.get("rates", []):
            lines.append(_rate_line(r))
        lines.append(
            f"- confusion: tp={cm['tp']} fp={cm['fp']} fn={cm['fn']} tn={cm['tn']} "
            "(positive class = defective)"
        )
        lines.append(
            "- NOTE: these rates are a LOGIC/ORACLE test on a tiny hand-seeded "
            "corpus — the wide CIs above (n<30) are the honest signal, NOT a "
            "statistical quality estimate. See docs/evaluation.md."
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
        for r in p.get("rates", []):
            lines.append(_rate_line(r))
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

    tr = report.get("pm_trajectory_decision_quality")
    if tr:
        vd = tr["verdict"]
        lines.append("## PM trajectory decision-quality (swappable judge)")
        lines.append("")
        lines.append(f"- trajectory: {tr['trajectory_id']}  rubric: {tr['rubric_id']}")
        lines.append(
            f"- verdict: passed={vd['passed']} score={vd['score']} "
            f"provider={vd['provider']} dry_run={vd['dry_run']}"
        )
        lines.append(f"- rationale: {vd['rationale']}")
        lines.append(
            "- NOTE: on the dryrun judge this is a MECHANISM signal only "
            "(deterministic stub); a real model judges the same trajectory at "
            "go-live with no code change."
        )
        lines.append(f"- passed: {tr.get('passed')}")
        lines.append("")

    g = report.get("grounding_fabrication_telemetry")
    if g:
        gv = g.get("verification", {})
        gc = g.get("counts", {})
        lines.append("## Grounding/fabrication telemetry (S1 ledger)")
        lines.append("")
        lines.append(
            f"- claims: verified={gv.get('verified')} rejected={gv.get('rejected')} "
            f"unverifiable={gv.get('unverifiable')} pending={gv.get('pending')} "
            f"| checked={gv.get('checked')}"
        )
        lines.append(
            f"- identities: revoked={gc.get('revoked_identities')} "
            f"quarantined={gc.get('quarantined_identities')} "
            f"total_strikes={gc.get('total_strikes')}"
        )
        for r in g.get("rates", []):
            lines.append(_rate_line(r))
        lines.append(
            "- NOTE: measures the LEDGER/TELEMETRY mechanism (recorded fabrications "
            "counted + rated correctly); end-to-end fabrication-catch quality lands "
            "with the Spokesman gate + real models."
        )
        lines.append(f"- passed: {g.get('passed')}")
        lines.append("")

    q = report.get("telemetry_quality_report")
    if q:
        lines.append("## Telemetry quality rollup")
        ws = q.get("workstream") or "(all workstreams)"
        lines.append("")
        lines.append(f"- workstream: {ws}")
        t = q["totals"]
        lines.append(
            f"- tasks: merged={t['tasks_merged']} abandoned={t['tasks_abandoned']} "
            f"| verify passed={t['verify_passed']} failed={t['verify_failed']} "
            f"| rekicks={t['rekicks']} | model_calls={t['model_calls']}"
        )
        for r in q.get("rates_ci", []):
            lines.append(_rate_line(r))
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
