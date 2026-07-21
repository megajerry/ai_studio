# state/ — cross-machine substrate

This directory is the **shared state and async message bus** between the remote
development session and the execution host, until the host's local DB becomes the
source of truth. See [ADR-0007](../docs/decisions/0007-cross-machine-state.md).

## Layout

- `status.md` — current high-level status of the studio / workstreams (human-readable).
- `inbox/` — instructions **for the host** dropped by the remote session. The host
  polls, acts, and clears/acknowledges them.
- `outbox/` — results / questions **from the host** back to the remote session.
- `events/` — append-only event snapshots (JSONL) exported for auditing/replay.
- `lessons/` — the durable lessons corpus (Retro output), injected into future work.
- `offhost/` — async delegation queue to the **off-host agent** (an intermittent
  remote worker); see [`offhost/README.md`](offhost/README.md) and
  [ADR-0010](../docs/decisions/0010-offhost-remote-agent.md).

## Rules

- **Human-readable & merge-friendly.** Prefer append-only JSONL / markdown; avoid
  rewriting large files (reduces merge conflicts across machines).
- **No secrets, ever.** Nothing here goes in git that shouldn't be public within
  the repo. Secrets live in `.env` (git-ignored).
- **Once the host is live**, the local DB is source of truth and *exports*
  snapshots here; this tree stays the durable, auditable record.
