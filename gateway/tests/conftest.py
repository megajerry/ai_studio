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
MULTI_SECRET = "test-multi-pinned-token-secret"

FULL_IDENTITY = "offhost-full"
READONLY_IDENTITY = "offhost-readonly"
PINNED_IDENTITY = "offhost-pinned"
MULTI_IDENTITY = "offhost-multi"
PINNED_WORKSTREAM = "pinned-vertical"
#: A token legitimately pinned to 2+ workstreams (ADR-0028) — has no single
#: default_workstream(), the case that used to fail-closed on the workstream-less
#: endpoints (whoami / read-one-task / studio-status / agents-env).
MULTI_WORKSTREAMS = frozenset({"multi-alpha", "multi-beta"})

ALL_SCOPES_SET = frozenset({SCOPE_READ, SCOPE_ENQUEUE, SCOPE_CLAIM, SCOPE_COMPLETE})


def make_registry() -> TokenRegistry:
    """Four tokens: full authority, read-only, singly-pinned, and multi-pinned."""
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
        Token(
            identity=MULTI_IDENTITY,
            scopes=ALL_SCOPES_SET,
            digest=token_digest(MULTI_SECRET),
            workstreams=MULTI_WORKSTREAMS,
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
