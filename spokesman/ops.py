"""Human remote ops control-plane (ADR-0033) — TEMPORARY scaffolding.

Until the studio is self-sufficient, a remote stakeholder has no way to start the
runtime on the host. This module lets the **authenticated human** drive the host
Docker daemon (mounted into the Spokesman container) via a small, named allowlist
of ops plus a gated arbitrary ``docker`` escape hatch.

Invariant reconciliation (CLAUDE.md):

- **#2 (agents don't touch the host):** this is a capability-gated **tool**
  invoked by a HUMAN on a deterministic fast-path (``spokesman.app`` parses the
  leading ``ops`` verb BEFORE any model runs) or a token-gated HTTP endpoint —
  it is NEVER reachable from the conversational LLM (``spokesman.converse``). The
  LLM has no ops capability and cannot emit a message back into the fast-path.
- **#5 (secrets never reach an agent / no secrets in logs):** the audit event
  (``ops.invoked``) records only the REDACTED docker/compose argv (env values
  stripped), the exit code, and the human identity — never stdout/stderr, env, or
  any secret. Provider/DB creds live in the environment, not here.

Everything is:

- **token-gated** at the transport (``X-Spokesman-Token`` / dashboard token),
  fail-closed;
- **audited** — every attempt (even a blocked one) emits ``ops.invoked``;
- **bounded** — output is truncated and the subprocess has a hard timeout;
- **destructive-guarded** — volume-deleting / force-removing / pruning /
  postgres-stopping ops require an explicit ``confirm``, so a single message can
  never destroy data.

The daemon control this grants is powerful; it is safe ONLY because of the
token gate + human-fast-path + off-public-tunnel restriction documented in
``docs/spokesman-whatsapp.md``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess  # noqa: S404 - the whole point of this tool is to run docker
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from runtime.enforce import EventSink, NullEventSink
from runtime.event_types import EVENT_OPS_INVOKED
from runtime.models import make_event

logger = logging.getLogger("spokesman.ops")

WORKSTREAM = "productivity"

#: Compose profiles to pass so any profiled service (runtime/spokesman/gateway)
#: resolves for ps/logs/restart/up regardless of which profile it lives in.
COMPOSE_PROFILES = ("runtime", "spokesman", "gateway")

#: Services whose stop/removal is treated as destructive (data / whole-studio).
CRITICAL_SERVICES = ("postgres",)

#: Hard limits.
DEFAULT_TIMEOUT_S = 60.0
MAX_STREAM_CHARS = 4000
DEFAULT_LOG_TAIL = 200

#: A plausible compose service name (never starts with ``-`` so it can't be
#: mistaken for a flag; no shell metacharacters — argv is passed without a shell).
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: Secret-shaped env keys (UPPER_SNAKE, or anything mentioning key/token/secret/pwd).
_SECRET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_HINT_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PWD|CRED)", re.IGNORECASE)
_ENV_FLAGS = {"-e", "--env", "--env-file"}
_PASSWORD_FLAGS = {"--password", "--password-stdin", "-p"}


class OpsError(ValueError):
    """A malformed / unsupported ops command (never executed)."""

    def __init__(self, message: str, *, action: str = "unknown") -> None:
        super().__init__(message)
        self.action = action


@dataclass
class CompletedRun:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


#: A runner takes the docker/compose argv + a timeout and returns a CompletedRun.
#: Injectable so tests never touch a real daemon.
DockerRunner = Callable[[Sequence[str], float], CompletedRun]


@dataclass
class ParsedOp:
    """A recognized ops command resolved to a concrete docker/compose argv."""

    action: str  # audit label, e.g. "worker.start", "ps", "logs", "docker"
    argv: list[str]  # the exact docker/compose argv (no shell)


@dataclass
class OpsResult:
    ok: bool
    action: str
    argv: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    destructive: bool = False
    needs_confirm: bool = False
    timed_out: bool = False
    error: Optional[str] = None

    def render(self) -> str:
        """A bounded, human-readable reply. Never includes env/secret values
        (the argv shown is redacted; stdout/stderr are the daemon's own output,
        returned only to the authenticated human — never to the event log)."""
        if self.error and not self.argv:
            return f"ops error: {self.error}"
        if self.needs_confirm:
            return (
                f"⚠️ '{self.action}' is DESTRUCTIVE ({self.error}). "
                "Nothing was run. Re-send the same command with 'confirm' appended "
                "to proceed."
            )
        shown = " ".join(redact_argv(self.argv))
        head = f"$ {shown}\n(exit {self.exit_code}"
        if self.timed_out:
            head += ", TIMED OUT"
        head += ")"
        body = self.stdout or ""
        if self.stderr:
            body += ("\n" if body else "") + "[stderr]\n" + self.stderr
        body = body.strip()
        return f"{head}\n{body}".strip() if body else head


# --- Redaction (audit + reply) ----------------------------------------------


def _redact_token(tok: str, prev: str) -> str:
    """Redact a single argv token that could carry a secret.

    Covers ``KEY=VALUE`` env-injection (``-e FOO=bar`` / ``FOO=bar``) and the
    value following an env/password flag. Non-secret ``k=v`` (e.g. ``worker=2``
    from a scale) is left intact so the audit stays meaningful.
    """
    if prev in _ENV_FLAGS or prev in _PASSWORD_FLAGS:
        return "<redacted>"
    if "=" in tok:
        key, _, _val = tok.partition("=")
        if _SECRET_KEY_RE.match(key) or _SECRET_HINT_RE.search(key):
            return f"{key}=<redacted>"
    return tok


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Return the argv with any secret-shaped values redacted."""
    out: list[str] = []
    prev = ""
    for tok in argv:
        out.append(_redact_token(tok, prev))
        prev = tok
    return out


# --- Parsing / argv construction --------------------------------------------


def _compose_base(with_profiles: bool = True) -> list[str]:
    base = ["docker", "compose"]
    if with_profiles:
        for p in COMPOSE_PROFILES:
            base += ["--profile", p]
    return base


def _require_service(name: str) -> str:
    if not _SERVICE_RE.match(name):
        raise OpsError(f"invalid service name {name!r}")
    return name


def build_op(tokens: Sequence[str]) -> ParsedOp:
    """Resolve ``ops`` command tokens (everything AFTER the ``ops`` verb) to a
    concrete docker/compose argv. Raises :class:`OpsError` for anything unknown.
    """
    toks = [t for t in tokens if t != ""]
    if not toks:
        raise OpsError(
            "usage: ops worker start|stop|status|scale N | ps | logs <svc> | "
            "restart <svc> | up <svc> | docker <args...>"
        )
    verb = toks[0].lower()
    rest = toks[1:]

    if verb == "worker":
        sub = rest[0].lower() if rest else ""
        if sub == "start":
            return ParsedOp("worker.start", _compose_base() + ["up", "-d", "worker"])
        if sub == "stop":
            return ParsedOp("worker.stop", _compose_base() + ["stop", "worker"])
        if sub == "status":
            return ParsedOp("worker.status", _compose_base() + ["ps", "worker"])
        if sub == "scale":
            if len(rest) < 2 or not re.fullmatch(r"\d+", rest[1]):
                raise OpsError("usage: ops worker scale <N>", action="worker.scale")
            n = rest[1]
            return ParsedOp(
                "worker.scale",
                _compose_base() + ["up", "-d", "--scale", f"worker={n}", "worker"],
            )
        raise OpsError("usage: ops worker start|stop|status|scale <N>", action="worker")

    if verb == "ps":
        return ParsedOp("ps", _compose_base() + ["ps"])

    if verb == "logs":
        if not rest:
            raise OpsError("usage: ops logs <svc>", action="logs")
        svc = _require_service(rest[0])
        return ParsedOp(
            "logs",
            _compose_base() + ["logs", "--no-color", "--tail", str(DEFAULT_LOG_TAIL), svc],
        )

    if verb == "restart":
        if not rest:
            raise OpsError("usage: ops restart <svc>", action="restart")
        svc = _require_service(rest[0])
        return ParsedOp("restart", _compose_base() + ["restart", svc])

    if verb == "up":
        if not rest:
            raise OpsError("usage: ops up <svc>", action="up")
        svc = _require_service(rest[0])
        return ParsedOp("up", _compose_base() + ["up", "-d", svc])

    if verb == "docker":
        if not rest:
            raise OpsError("usage: ops docker <args...>", action="docker")
        # Arbitrary escape hatch. Passed as argv (no shell) after `docker`.
        return ParsedOp("docker", ["docker", *rest])

    raise OpsError(f"unknown ops command {verb!r}", action=verb)


# --- Destructive classification ---------------------------------------------


def classify_destructive(argv: Sequence[str]) -> tuple[bool, str]:
    """Return ``(is_destructive, reason)`` for a resolved docker/compose argv.

    Destructive = can delete data or take down the whole studio. These require an
    explicit ``confirm`` so a single message can never destroy volumes.
    """
    lower = [t.lower() for t in argv]
    tokens = set(lower)

    if "down" in tokens:
        if tokens & {"-v", "--volumes"}:
            return True, "down --volumes deletes named volumes"
        return True, "down stops the whole project (including postgres)"
    if "prune" in tokens:
        return True, "prune bulk-deletes docker objects"
    if "rm" in tokens and tokens & {"-f", "--force"}:
        return True, "forced remove"
    if "volume" in tokens and tokens & {"rm", "prune"}:
        return True, "volume removal"
    stop_verbs = {"stop", "down", "kill", "rm"}
    if any(s in tokens for s in CRITICAL_SERVICES) and (stop_verbs & tokens):
        return True, "affects a critical service (postgres)"
    return False, ""


# --- Execution ---------------------------------------------------------------


def _compose_dir() -> str:
    """Directory `docker compose` runs in (so it finds the project + .env)."""
    env = os.environ.get("AI_STUDIO_COMPOSE_DIR")
    if env:
        return env
    # Fallback: repo root relative to this file (host / dev runs).
    return str(Path(__file__).resolve().parent.parent)


def _subprocess_runner(argv: Sequence[str], timeout: float) -> CompletedRun:
    """Default runner — invokes docker/compose against the mounted socket.

    Uses argv (NOT a shell), a hard timeout, and captured output. Never sets
    ``shell=True`` and never interpolates untrusted text into a shell string.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv),
            cwd=_compose_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return CompletedRun(exit_code=124, stdout=out, stderr=err, timed_out=True)
    except FileNotFoundError:
        return CompletedRun(
            exit_code=127, stdout="", stderr="docker CLI not found in container"
        )
    return CompletedRun(
        exit_code=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
    )


def _truncate(s: str, limit: int = MAX_STREAM_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"


def _audit(
    sink: EventSink,
    *,
    action: str,
    argv: Sequence[str],
    identity: str,
    destructive: bool,
    blocked: bool,
    ok: bool,
    exit_code: Optional[int],
    timed_out: bool = False,
    error: Optional[str] = None,
) -> None:
    """Emit the leak-free ``ops.invoked`` audit event.

    Payload carries ONLY operational metadata + the REDACTED argv — never
    stdout/stderr, env, or any secret value (CLAUDE.md invariants 5 & 6).
    """
    payload = {
        "action": action,
        "argv": redact_argv(argv),
        "identity": identity,
        "destructive": destructive,
        "blocked": blocked,
        "ok": ok,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }
    if error:
        payload["error"] = error[:200]
    try:
        sink.emit(
            make_event(type=EVENT_OPS_INVOKED, workstream=WORKSTREAM, payload=payload)
        )
    except Exception:  # noqa: BLE001 - audit must never break the op response
        logger.warning("ops.invoked audit emit failed for action=%s", action)


def run_ops(
    tokens: Sequence[str],
    *,
    identity: str,
    confirm: bool = False,
    runner: Optional[DockerRunner] = None,
    sink: Optional[EventSink] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> OpsResult:
    """Parse, guard, execute (or block) one ops command, and audit it.

    ``tokens`` are the words AFTER the ``ops`` verb (e.g. ``["worker","start"]``).
    ``identity`` is the accountable human (masked channel id / control-plane).
    Destructive ops are blocked unless ``confirm`` is true. Every path emits
    exactly one ``ops.invoked`` audit event.
    """
    runner = runner or _subprocess_runner
    sink = sink or NullEventSink()

    try:
        op = build_op(tokens)
    except OpsError as exc:
        _audit(
            sink, action=getattr(exc, "action", "unknown"), argv=[], identity=identity,
            destructive=False, blocked=True, ok=False, exit_code=None, error=str(exc),
        )
        return OpsResult(
            ok=False, action=getattr(exc, "action", "unknown"), error=str(exc)
        )

    destructive, reason = classify_destructive(op.argv)
    if destructive and not confirm:
        _audit(
            sink, action=op.action, argv=op.argv, identity=identity,
            destructive=True, blocked=True, ok=False, exit_code=None, error=reason,
        )
        return OpsResult(
            ok=False, action=op.action, argv=op.argv, destructive=True,
            needs_confirm=True, error=reason,
        )

    run = runner(op.argv, timeout)
    ok = run.exit_code == 0 and not run.timed_out
    _audit(
        sink, action=op.action, argv=op.argv, identity=identity,
        destructive=destructive, blocked=False, ok=ok, exit_code=run.exit_code,
        timed_out=run.timed_out,
    )
    return OpsResult(
        ok=ok, action=op.action, argv=op.argv, exit_code=run.exit_code,
        stdout=_truncate(run.stdout), stderr=_truncate(run.stderr),
        destructive=destructive, timed_out=run.timed_out,
    )


def parse_confirm(tokens: Sequence[str]) -> tuple[list[str], bool]:
    """Split a trailing ``confirm`` token out of a message-form ops command.

    ``ops docker system prune -f confirm`` → (``[docker, system, prune, -f]``, True).
    Only a trailing ``confirm`` counts, so a real docker arg is never eaten.
    """
    toks = list(tokens)
    if toks and toks[-1].lower() == "confirm":
        return toks[:-1], True
    return toks, False
