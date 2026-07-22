# `runtime/sandbox/` — the Docker sandbox runner

Architecture §8 (zero trust) + CLAUDE.md invariant 2: **no agent shells directly;
everything runs in a Docker sandbox.** This package is the concrete runner behind
the `ShellTool` seam. The 🔴 `shell` tool declares the extension point (the
`SandboxRunner` protocol in `runtime/tools/shell.py`); `DockerSandboxRunner` runs
a command inside a locked-down, throwaway container with no host access.

> **Scope.** This milestone delivers the **runner** only. Wiring an
> opencode / coding-worker to *dispatch* work through it is a **separate later
> milestone** (backlog "opencode / Docker sandbox worker" → coding-worker
> dispatch), which will construct a `ShellTool.with_docker_sandbox(...)` (or a
> `DockerSandboxRunner`) and route a coding task's commands through the normal
> policy-gated `invoke` path. Nothing dispatches through the sandbox yet.

## Threat model

The command a container runs is **untrusted** (agent- or model-authored). The
sandbox assumes it may try to: reach the network, read host files, exfiltrate
env/secrets, consume unbounded CPU/RAM/PIDs, escalate privileges, or persist
state. Defaults are chosen so that each of those fails closed.

| Threat | Default control |
| --- | --- |
| Network egress / exfiltration | `--network none` (`SANDBOX_NETWORK`) |
| Reading/clobbering host files | no bind mount unless `SANDBOX_WORKDIR` is set; then **only** that dir, at `/workspace`; host root `/` refused |
| Stealing host env / secrets | container gets **only** the `allowed_env` allowlist (default: none); values passed to the docker client by name, never in the argv/`ps` (CLAUDE.md invariant 5, ADR-0011) |
| Privilege escalation | `--user 65534:65534` (non-root), `--cap-drop ALL`, `--security-opt no-new-privileges` |
| Immutable-image tamper | `--read-only` rootfs + `--tmpfs /tmp` (only the mounted workdir + /tmp are writable) |
| Resource exhaustion | `--memory 256m`, `--cpus 1.0`, `--pids-limit 256` |
| Runaway / hung command | `SANDBOX_TIMEOUT_S` (default 30s); on exceed the container is `docker rm -f`'d and exit code `124` is returned |
| State accretion | `--rm` (container deleted on exit) |

The docker **client** subprocess itself receives only an operational env
passthrough (`PATH`, `HOME`, `DOCKER_HOST`, `DOCKER_CONFIG`, …) plus the values
of allowlisted vars — never the full host environment.

## Config (env → constructor → default)

| env | arg | default |
| --- | --- | --- |
| `SANDBOX_IMAGE` | `image` | `ai-studio-sandbox:latest` |
| `SANDBOX_NETWORK` | `network` | `none` |
| `SANDBOX_TIMEOUT_S` | `timeout_s` | `30` |
| `SANDBOX_MEMORY` | `memory` | `256m` |
| `SANDBOX_CPUS` | `cpus` | `1.0` |
| `SANDBOX_WORKDIR` | `workdir` | *(none — no bind mount)* |
| `SANDBOX_USER` | `user` | `65534:65534` |
| — | `allowed_env` | `()` (no env forwarded) |
| — | `pids_limit` | `256` |
| — | `read_only_rootfs` | `True` |

## How `ShellTool` uses it

`shell.exec` is 🔴, so `runtime.enforce.invoke` returns `NEEDS_APPROVAL` and never
reaches `ShellTool.execute` without a resolved human approval — **that gate is
unchanged.** Two independent guards remain (see `runtime/policy-tools.md`):

1. **Tier gate** — 🔴 in the policy engine.
2. **Refuse-without-sandbox** — with no runner injected, `execute` returns
   `ok=False` "sandbox not configured" and touches nothing on the host.

When a runner *is* configured, `execute` calls `sandbox.run(command)` and maps the
`(exit_code, stdout, stderr)` into a `ToolResult`. Wire it with:

```python
from runtime.tools.shell import ShellTool

# Lazy: Docker is imported only here, and a container launches only when
# execute() runs — i.e. only after a human approves the 🔴 call.
tool = ShellTool.with_docker_sandbox(workdir="/work/scoped", allowed_env=["BUILD_ID"])
registry.register(tool)   # then run only via invoke(role, "shell", command=...)
```

## Host setup (Docker required — NOT available in the remote session)

The runner and its tests need **no Docker** (the CLI is invoked lazily and mocked
in tests). Real container execution is **host-verified**:

```bash
# 1. Build the minimal non-root sandbox image (tag = default SANDBOX_IMAGE).
docker build -t ai-studio-sandbox:latest infra/sandbox

# 2. Smoke-test the runner against real Docker.
python3 - <<'PY'
from runtime.sandbox import DockerSandboxRunner
code, out, err = DockerSandboxRunner().run("echo hello from the sandbox")
print(code, repr(out), repr(err))          # -> 0 'hello from the sandbox\n' ''
# network is off by default:
print(DockerSandboxRunner().run("getent hosts example.com || echo NO-NET"))
PY
```

See `infra/sandbox/Dockerfile` for the image (non-root user, no secrets baked in).

## Verify (Docker mocked)

```bash
python3 -m pytest runtime/tests/test_sandbox.py -q
```

The tests patch `subprocess.run`, so no real container ever launches. They assert
every safety flag above, that a timeout force-removes the container, that only
allowlisted env is forwarded (and no secret leaks into argv or the client env),
and that `ShellTool` still refuses without a runner and stays 🔴 with one.
