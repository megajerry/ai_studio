"""Cursor provider adapter — AGENT-HARNESS inference via the `cursor-agent` CLI.

**This is NOT a raw HTTP inference endpoint.** Cursor exposes no
``/chat/completions``-style API; its HTTP APIs are operational (admin / analytics
/ agents), not inference. The *only* programmatic way to get a model completion
out of Cursor is its **agent-harness CLI**::

    cursor-agent -p "<prompt>" --output-format json

which returns a JSON object whose ``result`` field is the complete assistant
response text (authenticated with ``CURSOR_API_KEY``; underlying model selectable
via ``--model``). So this adapter implements the same :class:`Provider` interface
as the other adapters, but by **subprocess** rather than ``httpx`` — the CLI *is*
the transport. Because it drives a full agentic harness (planning, tool loops),
each call is **heavier and slower than a plain completion**; treat it as an
executor substrate, not a cheap turn.

**Why a hard timeout + fallback is mandatory.** There is a known 2026 reliability
bug where ``cursor-agent -p`` can **hang with no output**. An agentic loop that
never returns would wedge the studio, so this adapter enforces a hard
:data:`_DEFAULT_TIMEOUT_S` (overridable via ``CURSOR_CLI_TIMEOUT_S``) and, on
timeout / non-zero exit / unparseable JSON, raises
:class:`~runtime.model.providers.base.ProviderFallback` — the signal the call
wrapper uses to retry on the next (metered) model in the routed tier's chain.
The call is never blocked on Cursor.

**Keyless / dry-run safe.** With no ``CURSOR_API_KEY`` (adapter unavailable) or
in ``MODELS_DRY_RUN`` mode, the adapter does **NOT** shell out — it returns a
deterministic stub exactly like :class:`~runtime.model.providers.dryrun.DryRunProvider`,
so the whole suite stays keyless-green and nothing ever launches ``cursor-agent``
in tests. Secrets are read from the environment INSIDE this module and are never
logged, returned, or placed in the argv (ADR-0011, invariant 5): ``CURSOR_API_KEY``
is forwarded to the child process via its environment, by name, never on the
command line.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

from ..registry import Usage
from .base import Completion, Message, ProviderFallback
from .dryrun import DryRunProvider

#: The secret that activates the real (subprocess) path. Absent -> dry-run stub.
_API_KEY_ENV = "CURSOR_API_KEY"
#: The CLI binary. Non-secret; overridable for a pinned path / wrapper.
_CLI_ENV = "CURSOR_CLI_CMD"
_DEFAULT_CLI = "cursor-agent"
#: Hard wall-clock timeout (seconds) — defends against the `-p` hang bug.
_TIMEOUT_ENV = "CURSOR_CLI_TIMEOUT_S"
_DEFAULT_TIMEOUT_S = 180.0
#: Optional underlying model to pass via `--model` (non-secret operator config).
#: Left unset -> Cursor's own default harness model. The registry `model_id`
#: (e.g. `cursor-composer`) is used for routing/cost/telemetry, not as `--model`.
_MODEL_ENV = "CURSOR_MODEL"

_DRY_RUN_ENV = "MODELS_DRY_RUN"


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


class CursorCliProvider:
    """Cursor agent-harness inference over ``cursor-agent -p ... --output-format json``.

    Real subprocess path only when ``CURSOR_API_KEY`` is present and not in
    dry-run; otherwise a deterministic keyless stub (see the module docstring).
    """

    name = "cursor-cli"

    def __init__(self, *, cli: str | None = None, timeout_s: float | None = None) -> None:
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

    def complete(
        self, model_id: str, messages: list[Message], **opts: Any
    ) -> Completion:
        # Keyless / dry-run: NEVER shell out — return a deterministic stub that is
        # byte-for-byte what the DryRunProvider would produce (keyless-green).
        if _dry_run_forced() or not self.available():
            stub = DryRunProvider().complete(model_id, messages, **opts)
            stub.provider = self.name  # attribute the routed provider honestly
            return stub

        prompt = _prompt_from_messages(messages)
        argv = [self._cli, "-p", prompt, "--output-format", "json"]
        model = opts.get("cursor_model") or os.environ.get(_MODEL_ENV)
        if model:
            argv += ["--model", str(model)]

        # The child inherits the environment (so CURSOR_API_KEY reaches the CLI by
        # name, never on the argv). We shell out with a HARD timeout so the `-p`
        # hang bug can never wedge the caller.
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderFallback(
                f"{self.name}: '{shlex.join(argv[:1])} -p ...' timed out after "
                f"{self._timeout_s}s (cursor-agent -p hang bug) — falling back"
            ) from exc
        except (OSError, ValueError) as exc:  # binary missing / bad args
            raise ProviderFallback(
                f"{self.name}: could not launch {self._cli!r} ({exc}) — falling back"
            ) from exc

        if proc.returncode != 0:
            raise ProviderFallback(
                f"{self.name}: cursor-agent exited {proc.returncode} — falling back"
            )

        try:
            data = json.loads(proc.stdout)
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
