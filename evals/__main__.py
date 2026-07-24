"""Run the AI Studio evaluation harness and print the metrics (v2).

Usage::

    python -m evals                         # run all evals, print the metrics
    python -m evals --json  out.json        # also write the report as JSON
    python -m evals --markdown out.md       # also write a markdown summary
    python -m evals --workstream demo-xyz   # scope the telemetry rollup

Keyless by construction (forces ``MODELS_DRY_RUN``). The Verifier seeded-defect
eval + the metric arithmetic need no database; the PM structural eval, the
trajectory decision-quality eval, and the telemetry rollup need a reachable Postgres
(``DATABASE_URL``/``POSTGRES_*``). With no database those are skipped cleanly (never
hangs) and the harness still reports the Verifier precision/recall (with n + Wilson
95% CI) so at least some honest numbers always exist. Exit code is 0 when every eval
that ran passed, else 1.

v2: EVERY rate is printed with its sample size ``n`` and a Wilson 95% CI, and small
samples (n<30) are flagged INSUFFICIENT — so the tiny-corpus weakness is visible.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import report as report_mod
from .grounding_eval import run_grounding_eval
from .pm_eval import run_pm_structural_eval
from .stats import Rate, rate
from .trajectory_eval import run_trajectory_eval
from .verifier_eval import VerifierEvalResult, run_verifier_eval


def _print_rates(rates: list[Rate]) -> None:
    for r in rates:
        print(f"    {r.render()}")


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("MODELS_DRY_RUN", "1")
    parser = argparse.ArgumentParser(prog="evals", description="AI Studio eval harness v2")
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    parser.add_argument("--markdown", metavar="PATH", help="write a markdown summary")
    parser.add_argument("--workstream", metavar="WS", default=None,
                        help="scope the telemetry quality rollup to one workstream")
    args = parser.parse_args(argv)

    from runtime import db  # deferred: importing runtime shouldn't require a DB

    # --- Verifier seeded-defect eval (no DB required) -----------------------
    verifier_result: VerifierEvalResult = run_verifier_eval()
    verifier = verifier_result.to_dict()
    vcm = verifier["confusion"]
    print("=== Verifier seeded-defect eval (dry-run) ===")
    print(f"  cases={vcm['support']} defective={vcm['positives']} "
          f"f1={vcm['f1']} accuracy={vcm['accuracy']}")
    print(f"  confusion tp={vcm['tp']} fp={vcm['fp']} fn={vcm['fn']} tn={vcm['tn']} "
          f"(positive=defective)  passed={verifier['passed']}")
    print("  rates (each with n + Wilson 95% CI):")
    _print_rates(verifier_result.rates())
    print("  NOTE: tiny hand-seeded corpus -> a LOGIC/ORACLE test with WIDE CIs "
          "(n<30), NOT a statistical quality estimate.")
    for c in verifier["cases"]:
        flag = "ok " if c["correct"] else "XX "
        print(f"    {flag}{c['name']:<44} expect_pass={c['expected_pass']!s:<5} "
              f"got={c['predicted_pass']!s:<5} :: {c['reason']}")

    pm = None
    trajectory = None
    grounding = None
    quality = None
    all_passed = verifier["passed"]

    if db.can_connect(timeout=2.0):
        conn = db.connect()
        try:
            from runtime.migrate import migrate
            migrate(conn)

            # --- PM structural eval (needs DB) ------------------------------
            pm_result = run_pm_structural_eval(conn)
            pm = pm_result.to_dict()
            print("\n=== PM structural decomposition eval (dry-run) ===")
            print(f"  goals={pm['num_goals']} passed={pm['passed']}")
            _print_rates([pm_result.pass_rate()])
            for c in pm["cases"]:
                flag = "ok " if c["passed"] else "XX "
                print(f"    {flag}{c['goal']:<50} decision={c['decision']:<8} "
                      f"items={c['num_items']} criteria={c['all_items_have_criteria']} "
                      f"acyclic={c['dag_acyclic']} deps_sane={c['deps_sane']}")

            # --- Trajectory decision-quality eval via swappable judge -------
            traj_result = run_trajectory_eval(conn)
            trajectory = traj_result.to_dict()
            vd = trajectory["verdict"]
            print("\n=== PM trajectory decision-quality eval (swappable judge, dry-run) ===")
            print(f"  trajectory={trajectory['trajectory_id']} rubric={trajectory['rubric_id']}")
            print(f"  verdict passed={vd['passed']} score={vd['score']} "
                  f"provider={vd['provider']} dry_run={vd['dry_run']} "
                  f"harness_passed={trajectory['passed']}")
            print("  NOTE: dryrun judge = MECHANISM signal only; a real model judges "
                  "the same trajectory at go-live with no code change.")

            # --- Grounding/fabrication telemetry eval (needs DB) ------------
            grounding_result = run_grounding_eval(conn)
            grounding = grounding_result.to_dict()
            gc = grounding["counts"]
            gv = grounding["verification"]
            print("\n=== Grounding/fabrication telemetry eval (S1 ledger, dry-run) ===")
            print(f"  seeded prefix={grounding['prefix']} (throwaway, cleaned up)")
            print(f"  claims: verified={gv['verified']} rejected={gv['rejected']} "
                  f"unverifiable={gv['unverifiable']} pending={gv['pending']} "
                  f"checked={gv['checked']}")
            print(f"  identities: revoked={gc['revoked_identities']} "
                  f"quarantined={gc['quarantined_identities']} "
                  f"total_strikes={gc['total_strikes']} "
                  f"top_offenders={grounding['top_offenders']}")
            print(f"  passed={grounding['passed']}")
            print("  rates (each with n + Wilson 95% CI):")
            _print_rates(grounding_result.rates())
            print("  NOTE: measures the LEDGER/TELEMETRY mechanism (recorded "
                  "fabrications are counted + rated correctly); end-to-end "
                  "fabrication-catch quality lands with the Spokesman gate + real models.")

            # --- Telemetry quality rollup (needs DB) ------------------------
            from runtime.quality import quality_report
            quality = quality_report(conn, workstream=args.workstream)
            t, r = quality["totals"], quality["rates"]
            print("\n=== Telemetry quality rollup ===")
            print(f"  workstream={quality['workstream'] or '(all)'}")
            print(f"  merged={t['tasks_merged']} abandoned={t['tasks_abandoned']} "
                  f"verify_passed={t['verify_passed']} verify_failed={t['verify_failed']} "
                  f"rekicks={t['rekicks']} model_calls={t['model_calls']}")
            print("  rates (each with n + Wilson 95% CI):")
            _print_rates(report_mod.telemetry_rates(quality))
            print(f"    rekick_rate={r['rekick_rate']} (per-task ratio, not a "
                  "proportion -> no CI)")
            print(f"  avg_cost/completed=${quality['cost']['avg_cost_per_completed_task_usd']} "
                  f"avg_tokens/completed={quality['cost']['avg_tokens_per_completed_task']} "
                  f"avg_latency/completed_ms={quality['latency']['avg_latency_per_completed_task_ms']}")

            all_passed = (all_passed and pm["passed"] and trajectory["passed"]
                          and grounding["passed"])
        finally:
            conn.close()
    else:
        print("\n(no reachable DATABASE_URL — PM structural, trajectory, and telemetry "
              "evals skipped; run against the live Postgres for those. Verifier eval "
              "above ran without a DB.)")

    full = report_mod.build_report(verifier, pm, quality, trajectory, grounding)
    if args.json:
        Path(args.json).write_text(report_mod.to_json(full), encoding="utf-8")
        print(f"\nwrote JSON report -> {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(report_mod.render_markdown(full), encoding="utf-8")
        print(f"wrote markdown report -> {args.markdown}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
