# 0007 — Cross-machine state via a git substrate

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

This repo is developed from a **remote session** that has no access to the
**execution host** (a separate Mac that will run the real system with a local
DB). This split may persist for a while. We still need to track state/progress
and pass instructions between the two.

## Decision

Until the host is live, **git is the shared substrate and a low-cost async
message bus.** A tracked [`state/`](../../state/) tree holds human-readable
status, an append-only event snapshot, the lessons corpus, and an inbox/outbox:

- the remote session drops instructions into `state/inbox/`;
- the host polls, acts, and writes results/status back to `state/outbox/` and
  `state/status.md`;
- both sides sync by commit + pull.

Once the host runs, the **local DB is the source of truth**, and it *exports
snapshots* into `state/` for the remote session to read. Git remains the
durable, auditable record.

## Consequences

- No new infrastructure needed for cross-machine coordination during dev.
- State files must be human-readable and merge-friendly (prefer append-only
  JSONL / markdown over rewriting large files).
- Secrets never go in `state/` (or anywhere in git) — see `.env` convention.
- When the DB becomes source of truth, define the export cadence and format.
