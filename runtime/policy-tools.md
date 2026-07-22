# `runtime/` M2 — policy engine + tool layer

The capability-gated tool layer (architecture §5, CLAUDE.md invariants 2/3/5/6).
Agents are *powerful but unreliable CPUs*: they never touch the host. Every side
effect goes through a **tool**, every tool call goes through the **policy
engine**, and every decision + invocation **emits an event**.

> **The one rule for callers: agents only ever call `runtime.enforce.invoke`.**
> They never import or call a tool's `execute` directly. `invoke` is the choke
> point that enforces least privilege, the 🔴 approval gate, and the event log.
> A tool handle obtained any other way bypasses all three.

## Layout

| File | Purpose |
| --- | --- |
| `capabilities.py` | `Capability` enum, `ActionTier`, default capability→tier map, `effective_tier` (pure) |
| `policy.py` | `PolicyConfig` (rules-as-data), `PolicyRequest`, `Decision`, `decide()` (pure) |
| `tools/base.py` | `Tool` abstraction + `ToolResult` |
| `tools/__init__.py` | `ToolRegistry` (look up tools by name) |
| `tools/filesystem.py` | `FilesystemTool` — read/write/delete confined to a root |
| `tools/shell.py` | `ShellTool` — 🔴; refuses to run unsandboxed; `SandboxRunner` extension point |
| `enforce.py` | `invoke()` — the enforced path; `EventSink` (`Db`/`Memory`/`Null`) |
| `policy.example.yaml` | Documented default policy (rules are data); real policy → git-ignored `policy.yaml` |
| `tests/test_*.py` | pure policy/capability tests + tool confinement + enforce path (no DB) |

## Capability → tier mapping

Tiers follow CLAUDE.md / architecture §5. A call that needs several capabilities
is gated at the **most restrictive** tier among them.

| Capability | Tier | Behavior |
| --- | --- | --- |
| `fs.read` | 🟢 Green | auto-allow |
| `net.fetch` | 🟢 Green | auto-allow |
| `fs.write` | 🟡 Yellow | auto-allow, **logged** |
| `git.write` | 🟡 Yellow | auto-allow, logged |
| `secret.use` | 🟡 Yellow | auto-allow, logged (tool reads the secret from env, ADR-0011) |
| `fs.delete` | 🔴 Red | **human approval** |
| `shell.exec` | 🔴 Red | human approval |
| `spend.money` | 🔴 Red | human approval |
| `deploy` | 🔴 Red | human approval |

The map is the built-in baseline (`capabilities.DEFAULT_CAPABILITY_TIER`); a
policy config can override any entry as data (`tier_overrides`).

## Policy rules are DATA

`policy.example.yaml` (committed, documented) is the default. A real deployment
copies it to `runtime/policy.yaml` (git-ignored) or points `$AI_STUDIO_POLICY_FILE`
at an external path. Resolution order: `$AI_STUDIO_POLICY_FILE` → `runtime/policy.yaml`
→ `runtime/policy.example.yaml`.

```yaml
roles:
  researcher: [fs.read, net.fetch]          # reads/searches only — no write, no delete
  builder:    [fs.read, fs.write, git.write] # writes code + commits
  deployer:   [fs.read, fs.write, git.write, deploy, shell.exec]
tier_overrides: {}   # e.g. net.fetch: yellow
```

- **Least privilege** — a role may only use capabilities it is granted; anything
  else is `DENY`ed before tier/budget are even considered.
- Re-tiering a capability or adding a role is a **config edit, not a code change**.

## The invoke / decision flow

```
agent → invoke(role, tool_name, **kwargs)
          │  resolve tool from registry; required = tool.capabilities_for(**kwargs)
          ▼
        policy.decide(request, config)                 ── emits policy.decision (always)
          │
          ├─ DENY            role lacks a required capability  → DENIED, nothing runs
          ├─ NEEDS_APPROVAL  🔴 tier OR budget would exceed    → emits approval.requested,
          │                                                       returns PENDING, nothing runs
          └─ ALLOW           🟢 auto / 🟡 auto+logged           → emits tool.invoked, runs tool
```

`decide` order: **(1) least-privilege deny → (2) budget → (3) tier.** Budget: if a
`BudgetContext` is supplied and `spent + estimated > cap`, the call escalates to
`NEEDS_APPROVAL` (raising budget is a 🛑 stakeholder-approval item, ADR-0006).

**NEEDS_APPROVAL never auto-executes.** `invoke` returns a `PENDING` result with an
`approval_id` and emits `approval.requested`; the event log *is* the pending
record. The Spokesman/stakeholder loop (ADR-0006, 🔴 tier) resolves it later — this
layer does not auto-approve.

### Events (ADR-0012)

`invoke` emits via an injected `EventSink`, so the logic runs with no database:

- **production** → `DbEventSink(conn)` appends to the M1 append-only log.
- **tests / dry-run** → `MemoryEventSink` / `NullEventSink`.

Emitted types: `policy.decision` (every call), `tool.invoked` (on ALLOW+run),
`approval.requested` (on NEEDS_APPROVAL). Payloads carry only argument **keys**
(`arg_keys`), never values — file contents and any secret stay out of the log.

```python
from runtime import invoke, DbEventSink, FilesystemTool, ToolRegistry, load_policy

reg = ToolRegistry(); reg.register(FilesystemTool(root="/work/sandbox"))
res = invoke("builder", "filesystem",
             registry=reg, config=load_policy(), events=DbEventSink(conn),
             op="write", path="notes.md", content="…", task_id=task.id)
res.status  # EXECUTED | DENIED | PENDING
```

## Adding a tool

1. Subclass `Tool` in `runtime/tools/`. Set `name` and `required_capabilities`
   (the superset). Override `capabilities_for(**kwargs)` if a call's needed
   capabilities depend on its arguments (least privilege per operation).
2. Implement `execute(**kwargs) -> ToolResult`. It is the **only** place a side
   effect happens. Read any secret from `os.environ` / the secret store inside the
   tool — never accept secrets as kwargs (ADR-0011).
3. Return handled failures as `ToolResult(ok=False, error=…)`; raise only on
   programmer error.
4. Register it (`registry.register(MyTool(...))`) and grant the relevant roles the
   new capability in the policy config. Add tests (confinement / refusal / caps).

## ShellTool / sandbox caveat

`ShellTool` declares `shell.exec` (🔴). The Docker sandbox (architecture §8) is not
built yet, so it **must not shell out on the host**. Two independent guards:

1. **Tier gate** — `shell.exec` is 🔴, so `invoke` returns `PENDING` and never
   reaches `execute`.
2. **Defense in depth** — even if `execute` is called directly, it refuses unless a
   `SandboxRunner` is injected, returning `ok=False` "sandbox not configured".

**Extension point:** implement the `SandboxRunner` protocol
(`run(command, **kwargs) -> (exit_code, stdout, stderr)`) to run inside a
container/VM with no host access, and pass `ShellTool(sandbox=…)`. Nothing else on
the call path changes.

## Verify

```bash
pip install -r runtime/requirements.txt pytest   # adds PyYAML
python -m py_compile runtime/*.py runtime/tools/*.py
pytest runtime/tests/                             # policy/tools/enforce pass; DB tests skip
```

No Docker or Postgres required: policy/tool/enforce tests are pure or use a temp
dir + `MemoryEventSink`; the M1 DB integration tests skip cleanly off-host.
