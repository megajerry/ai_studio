# 0030 — Prod vs test traffic is an explicit payload tag (never goal text)

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

Ops cleanup and stakeholder views previously tried to distinguish "real" queue
rows from fixture/demo debris by **matching goal phrases** and disposable
workstream name heuristics. That failed in production: a genuine Spokesman /
PM goal that happened to share wording with a fixture was abandoned as
`cleanup:test-debris`, and a remote session counting "waiting" work could not
tell prod from test by looking at the goal string either.

ADR-0029 already keeps DB-backed tests off the live DB when possible. It does
not mark rows that *do* land on a shared database (or that survive a mis-aimed
run) as test traffic in a way that later cleanup and dashboards can trust.

## Decision

**Every task payload carries an explicit `traffic` field: `prod` or `test`.**

1. **Write path.** `runtime.tasks.enqueue_task` auto-tags via
   `runtime.traffic.tag_payload` when the key is missing. Default is `prod`.
   Processes that intentionally enqueue synthetic work set
   `AI_STUDIO_TRAFFIC=test` (also implied by `AI_STUDIO_TEST_DB=1`). Callers may
   still set `payload.traffic` explicitly; a well-formed value is preserved.
2. **Read path.** Stakeholder views (`spokesman.noise`, `studio_status`,
   dashboard) treat `payload.traffic = 'test'` as noise first. Legacy rows
   without the tag still fall back to workstream/type heuristics — **never**
   goal-string matching.
3. **Ops.** Cleanups and abandonment of "test debris" must key off
   `traffic=test` (and/or disposable-DB workstream conventions). Matching on
   goal text is forbidden.

## Consequences

- Tests / evals / coverage (`make test|coverage|evals`) set `AI_STUDIO_TRAFFIC=test`
  so their enqueues are marked even if someone points them at a non-`*_test`
  database by mistake.
- Production host processes leave the env unset → `prod`.
- Remotes enqueueing through the gateway inherit host default unless the
  payload sets `traffic` explicitly; gateway smoke/fixtures should set `test`.
- ADR-0029 remains the primary guard against polluting the live DB; this ADR is
  the systematic *label* when rows exist anyway.
