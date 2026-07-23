"""Run the AI Studio evaluation harness and print the metrics.

Usage::

    python -m evals                         # run all evals, print the metrics
    python -m evals --json  out.json        # also write the report as JSON
    python -m evals --markdown out.md       # also write a markdown summary
    python -m evals --workstream demo-xyz   # scope the telemetry rollup

Keyless by construction (forces ``MODELS_DRY_RUN``). The Verifier seeded-defect
eval + the metric arithmetic need no database; the PM structural eval and the
telemetry rollup need a reachable Postgres (``DATABASE_URL``/``POSTGRES_*``). With
no database those two are skipped cleanly (never hangs) and the harness still
reports the Verifier precision/recall so at least some numbers always exist.
Exit code is 0 when every eval that ran passed, else 1.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import report as report_mod
from .pm_eval import run_pm_structural_eval
from .verifier_eval import run_verifier_eval


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("MODELS_DRY_RUN", "1")
    parser = argparse.ArgumentParser(prog="evals", description="AI Studio eval harness v1")
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    parser.add_argument("--markdown", metavar="PATH", help="write a markdown summary")
    parser.add_argument("--workstream", metavar="WS", default=None,
                        help="scope the telemetry quality rollup to one workstream")
    args = parser.parse_args(argv)

    from runtime import db  # deferred: importing runtime shouldn't require a DB

    # --- Verifier seeded-defect eval (no DB required) -----------------------
    verifier = run_verifier_eval().to_dict()
    vcm = verifier["confusion"]
    print("=== Verifier seeded-defect eval (dry-run) ===")
    print(f"  cases={vcm['support']} defective={vcm['positives']} "
          f"precision={vcm['precision']} recall={vcm['recall']} "
          f"f1={vcm['f1']} accuracy={vcm['accuracy']}")
    print(f"  confusion tp={vcm['tp']} fp={vcm['fp']} fn={vcm['fn']} tn={vcm['tn']} "
          f"(positive=defective)  passed={verifier['passed']}")
    for c in verifier["cases"]:
        flag = "ok " if c["correct"] else "XX "
        print(f"    {flag}{c['name']:<44} expect_pass={c['expected_pass']!s:<5} "
              f"got={c['predicted_pass']!s:<5} :: {c['reason']}")

    pm = None
    quality = None
    all_passed = verifier["passed"]

    if db.can_connect(timeout=2.0):
        conn = db.connect()
        try:
            from runtime.migrate import migrate
            migrate(conn)

            # --- PM structural eval (needs DB) ------------------------------
            pm = run_pm_structural_eval(conn).to_dict()
            print("\n=== PM structural decomposition eval (dry-run) ===")
            print(f"  goals={pm['num_goals']} passed={pm['passed']}")
            for c in pm["cases"]:
                flag = "ok " if c["passed"] else "XX "
                print(f"    {flag}{c['goal']:<50} decision={c['decision']:<8} "
                      f"items={c['num_items']} criteria={c['all_items_have_criteria']} "
                      f"acyclic={c['dag_acyclic']} deps_sane={c['deps_sane']}")

            # --- Telemetry quality rollup (needs DB) ------------------------
            from runtime.quality import quality_report
            quality = quality_report(conn, workstream=args.workstream)
            t, r = quality["totals"], quality["rates"]
            print("\n=== Telemetry quality rollup ===")
            print(f"  workstream={quality['workstream'] or '(all)'}")
            print(f"  merged={t['tasks_merged']} abandoned={t['tasks_abandoned']} "
                  f"verify_passed={t['verify_passed']} verify_failed={t['verify_failed']} "
                  f"rekicks={t['rekicks']} model_calls={t['model_calls']}")
            print(f"  success_rate={r['task_success_rate']} "
                  f"verify_pass_rate={r['verify_pass_rate']} "
                  f"rekick_rate={r['rekick_rate']} error_rate={r['error_rate']}")
            print(f"  avg_cost/completed=${quality['cost']['avg_cost_per_completed_task_usd']} "
                  f"avg_tokens/completed={quality['cost']['avg_tokens_per_completed_task']} "
                  f"avg_latency/completed_ms={quality['latency']['avg_latency_per_completed_task_ms']}")

            all_passed = all_passed and pm["passed"]
        finally:
            conn.close()
    else:
        print("\n(no reachable DATABASE_URL — PM structural eval + telemetry rollup "
              "skipped; run against the live Postgres for those. Verifier eval above "
              "ran without a DB.)")

    full = report_mod.build_report(verifier, pm, quality)
    if args.json:
        Path(args.json).write_text(report_mod.to_json(full), encoding="utf-8")
        print(f"\nwrote JSON report -> {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(report_mod.render_markdown(full), encoding="utf-8")
        print(f"wrote markdown report -> {args.markdown}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
