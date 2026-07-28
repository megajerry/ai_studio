# Daily repo consistency/breakage audit — 2026-07-28

7-auditor fan-out + ground-truth. **Suite: 1286 passed, 1 failed (known `python-multipart` env only),
0 regressions; all 75 modules import; demo green + self-cleans.** Findings cluster in yesterday's new
compose/ops/dispatcher work (ADR-0031/0033). Core subsystems (lifecycle/dispatcher, model, free-form,
events, migrations 0001..0019, gateway security) verified clean.

## Confirmed bugs — fixes spawned (branch → independent review → merge)
| Sev | Finding | Fix branch |
|-----|---------|-----------|
| HIGH | Containerized worker can't handle `spokesman.prep`: `runtime/Dockerfile` copies only `runtime/`, but the worker imports `spokesman.converse` for that task type → ModuleNotFoundError → task churns. Also `workstreams/` absent (vertical config won't apply, latent). | `builder/runtime-image-completeness` |
| MED | First-run `ops worker start` times out (60s) because the runtime compose services build on first `up` (>60s). | `builder/ops-controlplane-fixes` |
| MED | Gateway: a multi-pinned token gets 403 on the workstream-less endpoints (`whoami`, `GET /tasks/{id}`, `/studio/status`, `/agents/env`) — `default_workstream()` returns None for multi-pin. Fail-closed, not a leak. | `builder/gateway-multipin-mint` |
| LOW | `CURSOR_API_KEY` missing from compose `x-runtime-env`; `MODELS_DRY_RUN` undocumented in `.env.example`. | `builder/runtime-image-completeness` |
| LOW | `ops` verb hijacks free text on public channels (canned refusal instead of reaching converse). | `builder/ops-controlplane-fixes` |
| LOW | `/ops` HTTP endpoint doesn't honor a trailing `confirm` token like the chat path. | `builder/ops-controlplane-fixes` |
| LOW | Gateway `mint` writes an unvalidated spec → a typo bricks gateway startup for all tokens. | `builder/gateway-multipin-mint` |

## Flagged for a decision — NOT auto-fixed (autonomy)
- **ADR-0031 "PM commissions all roles" is only half-wired (MED).** The dispatcher + `enqueue_role_task` + prompt
  catalog exist, but the PM's planning loop never calls `enqueue_role_task`, the Plan/WorkItem schema has no
  "commission role" field, and non-`work.*` item types are coerced to `work.task`. So Sourcing/Failure-analyst/
  Skill-lifecycle/Capacity-Steward handlers are registered but never produced at runtime. Completing it = the PM
  autonomously commissions specialist roles (bumps autonomy). Given "not self-sufficient / human-in-loop / not
  crons," this is the stakeholder's call: (a) wire PM autonomous commissioning, or (b) keep `enqueue_role_task`
  as a manual/human hook and fix the ADR-0031 §3 doc overclaim. Recommend (b) for now.

## Noted resilience follow-up (not fixed this cycle)
- `runtime/worker.py::run_once` doesn't wrap `handler(ctx)` in try/except, so a handler exception churns the task
  via the outer loop instead of failing it cleanly onto the recovery ladder. Worth hardening.

All clean-bug fixes are review-gated; the ADR-0031 item awaits a stakeholder decision.

## Outcome (end of cycle)
All 3 fix branches independently reviewed and MERGED to main:
- `29e6b9e` ops-controlplane-fixes (first-run timeout + verb fall-through + /ops confirm) — review APPROVE.
- `0f1b007` runtime-image-completeness (HIGH: ship spokesman/ + workstreams/; CURSOR key; MODELS_DRY_RUN doc) — review APPROVE (transitive-dep sufficiency proven).
- `ae8e8a6` gateway-multipin-mint (multi-pin 403 + scoped studio_status + mint validation) — TWO review rounds: first caught a `studio_status` multi-pin cross-workstream aggregate leak; fixed by scoping the aggregate to the token's pin-set; second APPROVE.
Merged main: key modules import; non-DB suite 284 passed / 83 skipped (DB-down skips only); no regressions.

STILL OPEN (stakeholder decision): ADR-0031 PM-autonomous-commissioning (wire it vs keep manual hook + fix doc) — recommend the latter.
NOTED follow-up: `run_once` doesn't wrap `handler(ctx)` (a handler crash churns a task instead of failing it cleanly).
