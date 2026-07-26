"""Cursor provider adapter — AGENT-HARNESS inference via the `cursor-agent` CLI,
run INSIDE the Docker sandbox (never on the host).

**This is NOT a raw HTTP inference endpoint.** Cursor exposes no
``/chat/completions``-style API; its HTTP APIs are operational (admin / analytics
/ agents), not inference. The *only* programmatic way to get a model completion
out of Cursor is its **agent-harness CLI**::

    cursor-agent -p "<prompt>" --output-format json

which returns a JSON object whose ``result`` field is the complete assistant
response text (authenticated with ``CURSOR_API_KEY``; underlying model selectable
via ``--model``). So this adapter implements the same :class:`Provider` interface
as the other adapters, but the CLI *is* the transport. Because it drives a full
agentic harness (planning, tool loops), each call is **heavier and slower than a
plain completion**; treat it as an executor substrate, not a cheap turn.

**Confined execution (CLAUDE.md invariants 2 & 5).** The ``cursor-agent`` harness
is agent-authored code running a full tool loop — exactly the escape-the-sandbox
risk class as ``shell.exec`` / the coding worker. It is therefore run **inside the
Docker sandbox** via a :class:`~runtime.tools.shell.SandboxRunner`, NEVER as a raw
host ``subprocess``. The sandbox forwards **only** the env names in its allowlist
(:data:`_SANDBOX_ALLOWED_ENV` = ``CURSOR_API_KEY`` — handed to the ``docker``
client by name, never in the argv, so it can't be read off ``ps``); every other
host variable — every secret (``OPENAI_API_KEY``, ``DATABASE_URL``, Anthropic
keys, …) — is withheld from the child. The key never appears on the command line.

**Network.** ``cursor-agent`` must reach Cursor's servers, so the operator must
allow egress for the sandbox (``SANDBOX_NETWORK`` — the hardened default is
``none``). With no egress the CLI fails and the call falls back to a metered model
(fail-safe), never blocking the studio.

**Why a hard timeout + fallback is mandatory.** There is a known 2026 reliability
bug where ``cursor-agent -p`` can **hang with no output**. An agentic loop that
never returns would wedge the studio, so the sandbox enforces a hard wall-clock
timeout (:data:`_DEFAULT_TIMEOUT_S`, overridable via ``CURSOR_CLI_TIMEOUT_S``) and
kills the container; on timeout / non-zero exit / unparseable JSON this adapter
raises :class:`~runtime.model.providers.base.ProviderFallback` — the signal the
call wrapper uses to retry on the next (metered) model in the routed tier's chain.
The call is never blocked on Cursor.

**Fail-closed, keyless / dry-run safe.** With no ``CURSOR_API_KEY`` (adapter
unavailable) or in ``MODELS_DRY_RUN`` mode, the adapter does **NOT** shell out — it
returns a deterministic stub exactly like
:class:`~runtime.model.providers.dryrun.DryRunProvider`, so the whole suite stays
keyless-green. When a key IS present but **no sandbox is available** (none injected
and Docker is absent), the adapter refuses to run on the host and raises
:class:`ProviderFallback` instead — there is NO raw-host execution path.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from ..registry import Usage
from .base import Completion, Message, ProviderFallback
from .dryrun import DryRunProvider

#: The secret that activates the real (sandboxed) path. Absent -> dry-run stub.
_API_KEY_ENV = "CURSOR_API_KEY"
#: The CLI binary. Non-secret; overridable for a pinned path / wrapper.
_CLI_ENV = "CURSOR_CLI_CMD"
_DEFAULT_CLI = "cursor-agent"
#: Hard wall-clock timeout (seconds) — defends against the `-p` hang bug. Applied
#: by the sandbox (it kills the container), not a host-side subprocess timeout.
_TIMEOUT_ENV = "CURSOR_CLI_TIMEOUT_S"
_DEFAULT_TIMEOUT_S = 180.0
#: Optional underlying model to pass via `--model` (non-secret operator config).
#: Left unset -> Cursor's own default harness model. The registry `model_id`
#: (e.g. `cursor-composer`) is used for routing/cost/telemetry, not as `--model`.
_MODEL_ENV = "CURSOR_MODEL"

_DRY_RUN_ENV = "MODELS_DRY_RUN"

#: The ONLY host env names forwarded into the sandbox for ``cursor-agent`` — the
#: auth key, by name (the sandbox hands the value to the ``docker`` client, never
#: to the argv). Every other host variable — every secret — is withheld from the
#: child (CLAUDE.md invariant 5, ADR-0011).
_SANDBOX_ALLOWED_ENV = (_API_KEY_ENV,)


def _dry_run_forced() -> bool:
    return os.environ.get(_DRY_RUN_ENV, "").strip() in {"1", "true", "yes", "on"}


def _prompt_from_messages(messages: list[Message]) -> str:
    """Flatten the chat messages into a single prompt string for `-p`.

    ``cursor-agent -p`` takes one prompt argument; we join message contents in
    order (system + user turns) so the whole context reaches the harness.
    """
    parts: list[str] = []
    for m in messages or []:
        content = m.get("content", "")
        parts.append(content if isinstance(content, str) else str(content))
    return "\n\n".join(p for p in parts if p)


def _build_default_sandbox(timeout_s: float):
    """Build the Docker sandbox that confines ``cursor-agent``, or ``None``.

    Mirrors :meth:`runtime.tools.coding.CodingTool.with_docker_sandbox`: Docker is
    imported **lazily** (not required at module import) and no container is ever
    launched here. The runner forwards ONLY :data:`_SANDBOX_ALLOWED_ENV` into the
    container and applies ``timeout_s`` as its hard wall-clock kill. Returns
    ``None`` when the ``docker`` binary is absent, so the caller can fail-closed to
    a fallback rather than ever running on the host.
    """
    from ...sandbox import DockerSandboxRunner

    runner = DockerSandboxRunner(
        allowed_env=_SANDBOX_ALLOWED_ENV,
        timeout_s=timeout_s,
    )
    return runner if runner.docker_available() else None


class CursorCliProvider:
    """Cursor agent-harness inference over ``cursor-agent -p ... --output-format json``.

    Sandboxed path only when ``CURSOR_API_KEY`` is present, not in dry-run, AND a
    :class:`~runtime.tools.shell.SandboxRunner` is available (injected, or a
    Docker sandbox auto-built when the ``docker`` binary is present). Otherwise a
    deterministic keyless stub (keyless / dry-run) or a :class:`ProviderFallback`
    (key present but no sandbox) — never a raw host subprocess.
    """

    name = "cursor-cli"

    def __init__(
        self,
        *,
        sandbox: Any = None,
        cli: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        #: Injected :class:`SandboxRunner` (Docker/VM). ``None`` -> auto-build a
        #: Docker sandbox on demand, or fail-closed if Docker is absent. NEVER host.
        self._sandbox = sandbox
        self._cli = cli or os.environ.get(_CLI_ENV, _DEFAULT_CLI)
        if timeout_s is not None:
            self._timeout_s = timeout_s
        else:
            try:
                self._timeout_s = float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S))
            except (TypeError, ValueError):
                self._timeout_s = _DEFAULT_TIMEOUT_S

    def available(self) -> bool:
        return bool(os.environ.get(_API_KEY_ENV))

    def _resolve_sandbox(self):
        """The sandbox to run in: the injected one, else an auto-built Docker one.

        Returns ``None`` only when nothing is injected AND Docker is unavailable —
        the caller then fails closed (never runs on the host).
        """
        if self._sandbox is not None:
            return self._sandbox
        return _build_default_sandbox(self._timeout_s)

    def _build_command(self, prompt: str, model: str | None) -> str:
        """Shell command run inside the sandbox (``sh -c``). Every field is
        shell-quoted so the prompt is a single argument that cannot break out of
        the invocation; the API key is NEVER here (it rides the sandbox env
        allowlist, by name)."""
        command = (
            f"{shlex.quote(self._cli)} -p {shlex.quote(prompt)} "
            "--output-format json"
        )
        if model:
            command += f" --model {shlex.quote(str(model))}"
        return command

    def complete(
        self, model_id: str, messages: list[Message], **opts: Any
    ) -> Completion:
        # Keyless / dry-run: NEVER shell out — return a deterministic stub that is
        # byte-for-byte what the DryRunProvider would produce (keyless-green).
        if _dry_run_forced() or not self.available():
            stub = DryRunProvider().complete(model_id, messages, **opts)
            stub.provider = self.name  # attribute the routed provider honestly
            return stub

        # Fail-closed: with a key but no sandbox (none injected AND Docker absent)
        # we refuse to run the agent harness on the host — fall back to a metered
        # model. There is NO raw-host execution path (invariant 2).
        sandbox = self._resolve_sandbox()
        if sandbox is None:
            raise ProviderFallback(
                f"{self.name}: no Docker sandbox available — refusing to run "
                "cursor-agent on the host; falling back"
            )

        prompt = _prompt_from_messages(messages)
        model = opts.get("cursor_model") or os.environ.get(_MODEL_ENV)
        command = self._build_command(prompt, model)

        # Run confined. The sandbox forwards ONLY _SANDBOX_ALLOWED_ENV (the key, by
        # name) into the container and applies the hard timeout (killing the
        # container on the `-p` hang bug). Any launch/plumbing error is a
        # recoverable provider failure -> fall back rather than wedge the caller.
        try:
            exit_code, stdout, stderr = sandbox.run(command)
        except Exception as exc:  # sandbox plumbing (e.g. docker unavailable mid-run)
            raise ProviderFallback(
                f"{self.name}: sandbox execution failed ({type(exc).__name__}) "
                "— falling back"
            ) from exc

        # The sandbox returns TIMEOUT_EXIT_CODE (124) when it killed the container
        # for exceeding the timeout, and any non-zero code on a harness failure —
        # both are recoverable: fall back to the next (metered) model.
        from ...sandbox import TIMEOUT_EXIT_CODE

        if exit_code == TIMEOUT_EXIT_CODE:
            raise ProviderFallback(
                f"{self.name}: cursor-agent exceeded {self._timeout_s}s in the "
                "sandbox (cursor-agent -p hang bug) — falling back"
            )
        if exit_code != 0:
            raise ProviderFallback(
                f"{self.name}: cursor-agent exited {exit_code} in the sandbox "
                "— falling back"
            )

        try:
            data = json.loads(stdout)
            text = data["result"]
            if not isinstance(text, str):
                raise TypeError("'result' is not a string")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderFallback(
                f"{self.name}: could not parse cursor-agent JSON 'result' ({exc}) "
                "— falling back"
            ) from exc

        # Cursor bills at a flat monthly rate, not per token; the harness does not
        # return token accounting. Report a synthetic Usage (input from the prompt)
        # so telemetry has a token figure; marginal $ is 0 via the registry price.
        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(text) // 4)
        usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=0)
        return Completion(
            text=text, usage=usage, model_id=model_id, provider=self.name
        )
