# Daily repo consistency/breakage audit — 2026-07-30

**Zero code changes since yesterday's audit** (only the 2026-07-29 audit-report doc landed;
`git diff --stat 22afbfb..HEAD` = one `state/audits/*.md`). So instead of re-auditing a
byte-identical tree, I ran the empirical heartbeat (imports + full suite + demo). Product
healthy: all modules import; `runtime.demo` exit 0.

## Finding — test-isolation flake (test-only, FIXED, merged c1a2fd3)
The FULL suite showed 8 `spokesman/tests/test_ops.py` failures that PASS in isolation.
Root cause: `runtime/tests/test_runtime_image_completeness.py::test_spokesman_prep_import_chain_resolves`
(added 2026-07-28) evicts `spokesman.*` from `sys.modules` to simulate the runtime image and
never restored them → a later `from . import ops` in `app.py::handle_inbound_command` bound a
FRESH `spokesman.ops` whose `_subprocess_runner` wasn't the test's mock → real docker call →
exit 127 → failures. NOT a product bug (nothing deletes `sys.modules` at runtime; confirmed).
Fix: snapshot + restore `sys.modules` in the test's `finally` (test-only, +19/-3). Full suite
now stably green across 2 runs (910 passed, 418 skipped, 1 known `python-multipart` env FAIL).

## Everything else
No other findings — the tree is otherwise identical to yesterday's clean sweep.

## Process note
Two days running I fumbled merging a subagent's worktree branch (used the agent-id / a
non-resolving name as the merge ref). Correct procedure recorded in memory
[[merging-subagent-worktree-branches]]: resolve the REAL ref (`git branch -a | grep`, or
`git log --all --oneline | grep <msg>`) and merge by the verified commit HASH before removing
the worktree.
