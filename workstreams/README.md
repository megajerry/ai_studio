# Workstreams — defining a vertical (config, not code)

A **vertical** workstream (a video channel, a game, a product) runs on top of the
one horizontal Productivity platform (this repo — ADR-0002). Standing one up is
**configuration, not code**: you write a `workstreams/<name>/config.yaml` that
supplies the vertical's charter, per-role overlays, budget, policy grants, skill
set, domain verify-checkers, and seed lessons to the SHARED platform roles via the
existing seams. **No PM/Executor/Verifier subclass is written.**

Where a vertical's four kinds of state live is ratified in
[ADR-0018](../docs/decisions/0018-vertical-isolation.md):

| State | Home | Isolation |
| --- | --- | --- |
| **state** (tasks/events/memory/budget/approvals) | the platform Postgres | scoped by the `workstream` column + memory scope rules |
| **artifacts** (renders/builds/exports) | an object store (MinIO) | one bucket per workstream (`object_store_bucket`) |
| **product code** (the game/site/pipeline) | its own git repo | built by the coding worker (opencode) in the sandbox |
| **definition** (this config) | this repo, `workstreams/<name>/` | the config file below |

The config is **rules-as-data and contains NO secrets** (ADR-0011): it only
*names* a bucket and references skills/checkers by name. Keys/credentials are
provisioned separately (see step 3).

## The config schema

Loaded + validated by [`runtime/workstream/config.py`](../runtime/workstream/config.py)
(`WorkstreamConfig`). Loading is **strict** — an unknown field, capability,
checker, period, or a name/directory mismatch raises a clear
`WorkstreamConfigError` naming the file. Override the workstreams root with
`AI_STUDIO_WORKSTREAMS_DIR`.

| Field | Drives | Via |
| --- | --- | --- |
| `name` | the workstream slug (must match the directory) | lookups by workstream |
| `charter` / `objective` | the `### Workstream charter` section of every role prompt | `compose_role_prompt(workstream_charter=…)` |
| `role_overlays` (per role) | that role's `### Role overlay` prompt section | `compose_role_prompt(role_overlay=…)` |
| `budget` (`cap_usd`/`cap_tokens`/`period`) | the workstream's spend ceiling | `runtime/budget.py` (seeded by `bootstrap_workstream`) |
| `policy_grants` (role→caps) | this workstream's capability grants, merged over base | `WorkstreamConfig.effective_policy()` → `invoke(config=…)` |
| `skills` (`names`/`dir`) | the roles' skill set (subset / local dir) | `WorkstreamConfig.effective_skills()` |
| `checkers` | domain verify-checkers registered on top of `marker` | `WorkstreamConfig.checker_registry()` → `verify(checkers=…)` |
| `memory_seed` | initial Knowledge lessons seeded into memory | `bootstrap_workstream` (idempotent) |
| `object_store_bucket` | the artifact bucket NAME (ADR-0018) | out-of-band provisioning |

A committed, runnable sample lives at
[`workstreams/example/config.yaml`](example/config.yaml).

## Worked example — a short-form VIDEO channel

### 1. Write the definition — `workstreams/video/config.yaml`

```yaml
name: video
charter: |
  You operate the studio's short-form video channel. Every published clip is
  30-180s, has burned-in captions and a thumbnail, and hooks in the first 3s.
role_overlays:
  pm: |
    Decompose an idea into script -> render -> caption -> thumbnail -> publish;
    each work item's success_criterion must be independently checkable.
  executor: |
    Record the clip's real facts in the artifact: `duration_seconds: <n>` and
    `captions: yes|no`, so the Verifier's video_audit judges on evidence.
  verifier: |
    Judge a clip on audited FACTS (duration + captions), never the "done" claim.
budget:
  cap_usd: 50.0
  period: monthly
policy_grants:
  # the channel's operator may spend money to PUBLISH (spend.money is 🔴 — a
  # publish still requires human approval; this only makes it reachable).
  operator: [fs.read, fs.write, net.fetch, secret.use, spend.money]
skills:
  names: [define-success-criteria, rigorous-review]
checkers: [video_audit]          # audits duration + captions from the artifact
memory_seed:
  - text: "Never publish a clip without burned-in captions and a thumbnail."
object_store_bucket: ws-video
```

### 2. The domain tools/skills/checker

- **`video_audit` checker** — a built-in domain check
  ([`runtime/workstream/checkers.py`](../runtime/workstream/checkers.py)) that
  re-reads the produced clip and judges on OBSERVED facts (duration + captions),
  never the author's claim (ADR-0014). A work item selects it with a structured
  criterion in its payload:
  `check: {check: video_audit, require: {min_seconds: 30, captions: true}}`.
  Adding a *new* domain check is a small, reviewed platform contribution
  (register it in `BUILTIN_CHECKERS`); any workstream then enables it by name.
- **A publish 🔴 tool** — publishing is a `spend.money` / `deploy`-class action,
  so it runs through the policy-gated `invoke` path at the 🔴 tier and requires a
  human approval every time (CLAUDE.md approval tiers). Grant the operator role
  the capability in `policy_grants` (as above); the tool reads its API token from
  the secret manager — the token is **never** in this config.
- **Skills** — reference existing reviewed skills by `names`, or point `skills.dir`
  at a workstream-local skills root for domain skills.

### 3. Provision keys + the bucket (out of band, never in the config)

Real credentials live in the git-ignored env (ADR-0011), collected by
`scripts/onboarding.sh`; the object-store bucket named in `object_store_bucket` is
created in MinIO. The config only *names* these — it holds no secret.

### 4. Seed the objective + bootstrap

```python
from runtime.db import connect
from runtime.workstream import load_workstream_config, bootstrap_workstream

with connect() as conn:
    cfg = load_workstream_config("video")
    bootstrap_workstream(conn, cfg)   # idempotent: seeds memory + sets the budget
```

`bootstrap_workstream` is safe to re-run — an already-seeded lesson is skipped and
the budget is an upsert.

### 5. Run it

Start the scheduler + worker as usual (`python -m runtime.worker`). When the
worker claims a task for workstream `video`, it resolves `workstreams/video/config.yaml`
automatically (`runtime.workstream.resolve_workstream_config`) and threads the
charter/overlays into the PM/Executor/Verifier prompts, the `video_audit` checker
into the Verifier, and the workstream's policy grants + skill set into every role.
Its budget gates model spend; its seeded lessons are recalled into future work.

A workstream with **no** config file falls back to the platform's inline base
behavior unchanged — configuring a vertical is purely additive.

The end-to-end path (charter/overlay drive the prompt; the domain checker,
budget, policy grants, and seeded+scope-isolated memory all apply) is exercised
live in `python -m runtime.demo` (the fifth act) and in
`runtime/tests/test_workstream_config.py`.
