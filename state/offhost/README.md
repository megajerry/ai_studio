# state/offhost/ — delegation to the off-host agent

Async work queue between the execution **host** and the **off-host agent** (an
intermittent remote session that shares state only via git). See
[ADR-0010](../../docs/decisions/0010-offhost-remote-agent.md).

## Layout

- `requests/` — tasks the host/PM delegates **to** the off-host agent. One file
  per request. Must be **self-contained and idempotent** (goal, inputs, acceptance
  criteria, priority, `stale_after` hint) — the off-host agent may act much later
  with only git as context.
- `results/` — results **from** the off-host agent, usually a reference to a
  pushed branch/PR plus a summary.

## Protocol

1. Host/PM writes `requests/<id>.md` and pushes.
2. Off-host agent (when active) pulls, picks a request, and appends a **claim**
   (agent id + timestamp) to that file so work isn't duplicated.
3. It does the work on a branch under [`CONTRIBUTING.md`](../../CONTRIBUTING.md),
   pushes, and writes `results/<id>.md`.
4. Merge review still applies — off-host work lands via a reviewed PR.

## Rules

- **The host never blocks on this.** Every delegated item has a host-side fallback
  and a timeout; the off-host agent may never respond.
- **Delegate only host-resource-free work** (research, design, docs, code drafting,
  review). Never delegate anything needing running services, secrets, or deploy.
- The supervisor may **expire stale claims** so abandoned requests can be retried.
