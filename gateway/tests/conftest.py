"""Shared fixtures — no live credentials; every token here is a throwaway fake."""

from __future__ import annotations

import pytest

from gateway.auth import (
    SCOPE_CLAIM,
    SCOPE_COMPLETE,
    SCOPE_ENQUEUE,
    SCOPE_READ,
    RateLimiter,
    Token,
    TokenRegistry,
    token_digest,
)
from gateway.config import Settings

#: Fake token secrets (never real credentials — this is a public repo).
FULL_SECRET = "test-full-token-secret"
READONLY_SECRET = "test-readonly-token-secret"
PINNED_SECRET = "test-pinned-token-secret"

FULL_IDENTITY = "offhost-full"
READONLY_IDENTITY = "offhost-readonly"
PINNED_IDENTITY = "offhost-pinned"
PINNED_WORKSTREAM = "pinned-vertical"

ALL_SCOPES_SET = frozenset({SCOPE_READ, SCOPE_ENQUEUE, SCOPE_CLAIM, SCOPE_COMPLETE})


def make_registry() -> TokenRegistry:
    """Three tokens: full authority, read-only, and one pinned to a workstream."""
    return TokenRegistry([
        Token(
            identity=FULL_IDENTITY,
            scopes=ALL_SCOPES_SET,
            digest=token_digest(FULL_SECRET),
        ),
        Token(
            identity=READONLY_IDENTITY,
            scopes=frozenset({SCOPE_READ}),
            digest=token_digest(READONLY_SECRET),
        ),
        Token(
            identity=PINNED_IDENTITY,
            scopes=ALL_SCOPES_SET,
            digest=token_digest(PINNED_SECRET),
            workstreams=frozenset({PINNED_WORKSTREAM}),
        ),
    ])


def make_settings(*, tokens: TokenRegistry = None, **overrides) -> Settings:
    kwargs = {"tokens": tokens if tokens is not None else make_registry()}
    kwargs.update(overrides)
    return Settings(**kwargs)


def bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def registry() -> TokenRegistry:
    return make_registry()


@pytest.fixture
def settings(registry: TokenRegistry) -> Settings:
    return make_settings(tokens=registry)


@pytest.fixture
def generous_limiter() -> RateLimiter:
    """A limiter that never trips, for tests that are not about rate limiting."""
    return RateLimiter(rate_per_min=100_000, burst=10_000)
