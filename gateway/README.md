# Remote task gateway — builder WIP (unreviewed)

**Status:** builder work on `builder/remote-task-gateway`, awaiting the normal
merge review round ([CONTRIBUTING](../CONTRIBUTING.md)). **Not** an approved
studio-PM plan.

Provenance, stated plainly: this tree was produced by an off-host Cursor session
that was (wrongly) prompted as "the PM". Studio roles run **only** as
queue-driven executors on the host worker — see
[`state/lessons/2026-07-27-cursor-task-is-not-studio-pm.md`](../state/lessons/2026-07-27-cursor-task-is-not-studio-pm.md)
and `.cursor/rules/studio-roles-are-queue-driven.mdc`. The code itself is
ordinary off-host builder work (ADR-0010) and is reviewable on its merits; the
*planning* it appeared to represent is not. The real intake for this requirement
is `pm.tick` `6135b920-0558-4912-9fab-a18aa0c9dd4f`, which the host worker must
claim so the studio PM decomposes the requirement itself.

So: review this branch as builder WIP, and let the PM's decomposition — not this
session — decide whether it is the shape the studio wants. (That `pm.tick` has
since run in **dry-run**, which merged three generic "produce the artifact for
part N of 3" placeholders rather than a real plan — so there is still no studio-PM
decomposition to compare this against.)

The code itself is verified end-to-end on the host as of 2026-07-27: the full
suite passes with a live Postgres, and the queue verbs round-trip from genuinely
off-LAN over a tunnel with every security gate observed refusing. Evidence is
recorded in [`docs/remote-task-access.md`](../docs/remote-task-access.md) §5.

## What it is

A least-authority HTTP surface that lets a **non-LAN** remote session work the
task queue without a database credential — five verbs, all routed through
`runtime.tasks`. Design + threat model:
[ADR-0027](../docs/decisions/0027-remote-task-access-gateway.md). Runbook:
[`docs/remote-task-access.md`](../docs/remote-task-access.md).

| File | Role |
| --- | --- |
| `auth.py` | the security gates (pure, framework-free, unit-tested) |
| `config.py` | env-driven settings (digest-only credentials) |
| `app.py` | the FastAPI verbs + audit events |
| `client.py` | stdlib-only remote client + CLI (incl. host-side `mint`) |
| `tests/` | gate tests + a live-Postgres acceptance path |
