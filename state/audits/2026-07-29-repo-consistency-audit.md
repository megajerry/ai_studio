# Daily repo consistency/breakage audit — 2026-07-29

Delta since 2026-07-28 was tiny (host A2P commit `e47e5a0` touching spokesman
app/privacy/terms + my already-reviewed ops-usable change; `runtime/` byte-unchanged).
Scaled the sweep to the delta: full empirical ground-truth run + spokesman-delta audit +
cross-subsystem integration/drift check (rather than re-auditing unchanged subsystems).

## CRITICAL found + FIXED (merged 22afbfb, review-gated)
- **Spokesman container crash-loop / whole test suite broken.** Host commit `e47e5a0`
  ("A2P campaign") added `from .sms_optin import render_sms_opt_in` + a `/sms-opt-in` route
  to `spokesman/app.py` but never committed `spokesman/sms_optin.py`. `create_app()` runs at
  module top-level under `uvicorn spokesman.app:app`, so the container crash-looped and 11
  spokesman test modules ERRORed at collection. FIX: added `spokesman/sms_optin.py`
  (`render_sms_opt_in()` A2P consent page, mirroring terms/privacy) + test. `spokesman.app`
  imports again; suite collects clean. (My ops-usable branch was cut before the A2P commit, so
  its review passed in isolation; I've noted to run a post-merge import check when merging onto a
  moved main.)

## Clean
- `runtime/` (lifecycle/model/roles/gateway) byte-unchanged since yesterday → clean-by-unchanged.
- Migrations contiguous 0001..0019; config/compose coherent (`DOCKER_GID`, group_add well-formed);
  no event/contract drift from the delta; ops-usable change no regression.

## LOW flagged (not fixed)
- `terms.py` now promises in-chat `STOP`/`HELP` handling not implemented in the inbound path.
  Twilio's carrier-layer Advanced Opt-Out normally intercepts these pre-webhook, so it's a
  compliance-surface note, not a runtime break. Follow-up: add a minimal STOP/HELP fast-path.

Known-env only: `python-multipart` not installed off-host (readiness imports FAIL) — resolves on host.
