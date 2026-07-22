# Coding-worker dispatch — opencode as a replaceable Worker

Architecture §14 + CLAUDE.md invariants 2 & 3: **opencode is one replaceable
Worker, not the runtime.** The Builder never knows which coding agent runs; it
only knows "Need Prototype," and the runtime dispatches a Worker. This is the
dispatch seam — it routes a coding task through the (already-built) Docker sandbox
runner via the policy-gated `invoke` path, with opencode as the swappable worker.

Nothing about the studio's brain (orchestration, memory, policy, evaluation,
business logic) depends on opencode; swapping it for Claude/Gemini/any CLI coding
agent is a config change (`CODING_WORKER_CMD`), not a code change.

## The flow

```
Builder "Need Prototype"                         (a work.code / prototype task)
        │
        ▼
runtime.worker.run_once  ──▶ _handle_code         (loop-free coding path, §14)
        │
        ▼
runtime.enforce.invoke(role="builder", tool_name="coding", …)   ← policy gate
        │  code.run is 🔴  →  NEEDS_APPROVAL
        ▼
   ┌──────────────── no grant ────────────────┐   ┌──────── human grant ────────┐
   │ task parked `blocked` on the approval;    │   │ invoke consumes the grant    │
   │ resume_approved re-queues on grant/deny   │   │ and runs the tool once        │
   └───────────────────────────────────────────┘   └───────────────┬──────────────┘
                                                                     ▼
                                                   CodingTool.execute  (refuses on host)
                                                                     │
                                                                     ▼
                                        SandboxRunner.run(  "opencode run <goal>"  )
                                                                     │  inside Docker
                                                                     ▼
                                        DockerSandboxRunner → hardened container
                                        (network-off, non-root, read-only, cap-drop,
                                         scoped /workspace mount, allowlisted env only)
```

Result: a `ToolResult` with the worker's `exit_code`, `stdout`, and the
`produced_files` ref (the scratch workspace). On success the task merges; on a
non-zero worker exit it is abandoned. There is **no retry loop** in the coding
path — the worker's own exit status is the pass/fail signal.

## Two independent guards (same as `ShellTool`)

1. **Tier gate.** `code.run` is 🔴 (`runtime/capabilities.py`), so
   `runtime.enforce.invoke` returns `NEEDS_APPROVAL` and never reaches
   `CodingTool.execute` without a resolved human approval (ADR-0006). Running
   agent-authored code is the same escape-the-sandbox risk class as `shell.exec`.
2. **Refuse-without-sandbox.** Even if `execute` is reached, it refuses unless a
   `SandboxRunner` was injected — it **never** runs opencode on the host. With no
   runner it returns `ok=False` "sandbox not configured" and touches nothing.

## Secrets (CLAUDE.md invariant 5, ADR-0011)

`CodingTool` reads **no** secrets itself. Any credential the worker needs is
forwarded **only** via the sandbox's explicit `allowed_env` allowlist (reusing
`DockerSandboxRunner`'s env handling): the values are handed to the `docker`
client by name, never embedded in the argv/`ps`, and every other host variable —
every secret — stays out of the container.

## Config

| env | default | meaning |
| --- | --- | --- |
| `CODING_WORKER_CMD` | `opencode` | the coding worker CLI (swap to any agent) |
| `SANDBOX_*` | see `runtime/sandbox.md` | the Docker sandbox hardening/config |

The task payload carries the prototype spec: `goal` (the "Need Prototype" ask)
and `workspace` (the scratch workspace ref, echoed back as `produced_files`).
The role is the **Builder**, granted `code.run` in `runtime/policy.example.yaml`.

## Host setup (Docker + opencode required — NOT available in the remote session)

The tool and its tests need **no Docker and no opencode** (the sandbox is mocked,
opencode is never launched in tests). Real dispatch is **host-verified**:

```bash
# 1. Build the sandbox image (see runtime/sandbox.md) and install opencode in it,
#    OR install opencode into the sandbox image so `opencode` is on PATH there.
docker build -t ai-studio-sandbox:latest infra/sandbox

# 2. Wire the CodingTool with a Docker sandbox scoped to a scratch workspace.
python3 - <<'PY'
from runtime.tools import CodingTool
tool = CodingTool.with_docker_sandbox(
    workdir="/work/scratch-proto",     # bind-mounted to /workspace, rw
    allowed_env=["OPENCODE_MODEL"],    # forward ONLY what opencode needs
)
# code.run is 🔴 → only run via invoke(role="builder", "coding", goal=..., workspace=...)
# after a human approves; opencode then runs inside the container.
PY
```

To swap opencode for another worker, set `CODING_WORKER_CMD` (e.g.
`export CODING_WORKER_CMD=claude-code`) and ensure that CLI is on PATH inside the
sandbox image. Nothing else changes.

## Verify (Docker + opencode mocked)

```bash
python3 -m pytest runtime/tests/test_tools_coding.py runtime/tests/test_worker_code.py -q
```

The tests use a fake sandbox and a stubbed `invoke`, so no real container or
opencode ever launches. They assert: `code.run` is 🔴; the tool refuses without a
sandbox (never runs on the host); it builds the correct `opencode run <goal>`
invocation inside the sandbox; opencode is swappable via `CODING_WORKER_CMD`; the
env allowlist forwards only allowlisted names and leaks no host secret; the
enforced `invoke` PENDs without a grant and EXECUTES with one; and the worker
routes `work.code` / `prototype` to the loop-free coding path (block on 🔴,
merge on success, abandon on failure/deny).
