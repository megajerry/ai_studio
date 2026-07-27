"""Env-driven settings for the task gateway (ADR-0011: nothing baked in).

Every knob has a conservative default, and the one credential-bearing value
(``TASK_GATEWAY_TOKENS``) carries only SHA-256 **digests** — see
:mod:`gateway.auth`. All names are documented in ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from .auth import RateLimiter, TokenRegistry

#: Configured remote credentials: whitespace/comma-separated
#: ``identity:scopes:sha256hex[:workstreams]`` entries. Unset ⇒ fail closed.
ENV_TOKENS = "TASK_GATEWAY_TOKENS"
ENV_RATE_PER_MIN = "TASK_GATEWAY_RATE_PER_MIN"
ENV_BURST = "TASK_GATEWAY_BURST"
ENV_MAX_BODY_BYTES = "TASK_GATEWAY_MAX_BODY_BYTES"
ENV_MAX_PAYLOAD_BYTES = "TASK_GATEWAY_MAX_PAYLOAD_BYTES"
ENV_MAX_PRIORITY = "TASK_GATEWAY_MAX_PRIORITY"
ENV_MAX_BUDGET_TOKENS = "TASK_GATEWAY_MAX_BUDGET_TOKENS"
ENV_MAX_LIMIT = "TASK_GATEWAY_MAX_LIMIT"
ENV_PORT = "TASK_GATEWAY_PORT"

#: Sustained requests/minute allowed per identity, and the instantaneous burst.
DEFAULT_RATE_PER_MIN = 120
DEFAULT_BURST = 20
#: Hard cap on a request body (bytes) — a remote cannot make the host buffer more.
DEFAULT_MAX_BODY_BYTES = 64 * 1024
#: Hard cap on a task ``payload`` (bytes of JSON) written into the queue row.
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024
#: A remote may not outrank host work: enqueue priority is clamped to ±this.
DEFAULT_MAX_PRIORITY = 10
#: Ceiling a remote may set on a task's token budget (ADR-0022 still enforces spend).
DEFAULT_MAX_BUDGET_TOKENS = 200_000
#: Ceiling on any list endpoint's ``limit``.
DEFAULT_MAX_LIMIT = 100


def _int_env(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved gateway configuration (immutable for the process' lifetime)."""

    tokens: TokenRegistry
    rate_per_min: int = DEFAULT_RATE_PER_MIN
    burst: int = DEFAULT_BURST
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    max_priority: int = DEFAULT_MAX_PRIORITY
    max_budget_tokens: int = DEFAULT_MAX_BUDGET_TOKENS
    max_limit: int = DEFAULT_MAX_LIMIT

    def new_limiter(self) -> RateLimiter:
        return RateLimiter(self.rate_per_min, self.burst)


def get_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build :class:`Settings` from the environment.

    A malformed ``TASK_GATEWAY_TOKENS`` raises
    :class:`~gateway.auth.TokenSpecError` here — at startup, loudly — rather than
    degrading to a weaker gate at request time.
    """
    env = os.environ if env is None else env
    return Settings(
        tokens=TokenRegistry.from_spec(env.get(ENV_TOKENS)),
        rate_per_min=_int_env(env, ENV_RATE_PER_MIN, DEFAULT_RATE_PER_MIN),
        burst=_int_env(env, ENV_BURST, DEFAULT_BURST),
        max_body_bytes=_int_env(env, ENV_MAX_BODY_BYTES, DEFAULT_MAX_BODY_BYTES),
        max_payload_bytes=_int_env(
            env, ENV_MAX_PAYLOAD_BYTES, DEFAULT_MAX_PAYLOAD_BYTES
        ),
        max_priority=_int_env(env, ENV_MAX_PRIORITY, DEFAULT_MAX_PRIORITY, minimum=0),
        max_budget_tokens=_int_env(
            env, ENV_MAX_BUDGET_TOKENS, DEFAULT_MAX_BUDGET_TOKENS
        ),
        max_limit=_int_env(env, ENV_MAX_LIMIT, DEFAULT_MAX_LIMIT),
    )
