"""The security gates themselves (ADR-0027) — pure, no server, no database.

One test per claim the ADR makes: fail-closed, digest-only storage, scopes,
workstream pinning, rate limiting, and the gate ORDER (rate limit before
authorization, so a scope-less token cannot hammer the surface for free).
"""

from __future__ import annotations

import pytest

from gateway.auth import (
    ALL_SCOPES,
    REASON_MISSING_SCOPE,
    REASON_NO_TOKEN,
    REASON_NO_TOKENS,
    REASON_RATE_LIMITED,
    REASON_UNKNOWN_TOKEN,
    REASON_WORKSTREAM_DENIED,
    SCOPE_ANY,
    SCOPE_CLAIM,
    SCOPE_ENQUEUE,
    SCOPE_READ,
    Allowed,
    Denied,
    RateLimiter,
    Token,
    TokenRegistry,
    TokenSpecError,
    authorize,
    parse_bearer,
    parse_token_spec,
    token_digest,
)

from .conftest import (
    FULL_IDENTITY,
    FULL_SECRET,
    PINNED_SECRET,
    PINNED_WORKSTREAM,
    READONLY_IDENTITY,
    READONLY_SECRET,
    make_registry,
)

DIGEST = token_digest("whatever")


# --- Token specs ------------------------------------------------------------


def test_parses_a_minimal_spec() -> None:
    tok = parse_token_spec(f"offhost-cursor:read|enqueue:{DIGEST}")
    assert tok.identity == "offhost-cursor"
    assert tok.scopes == {SCOPE_READ, SCOPE_ENQUEUE}
    assert tok.workstreams == frozenset()
    assert tok.digest == DIGEST


def test_parses_workstream_pinning() -> None:
    tok = parse_token_spec(f"pinned:read:{DIGEST}:video|productivity")
    assert tok.workstreams == {"video", "productivity"}


@pytest.mark.parametrize(
    "spec",
    [
        "identity-only",
        f"id:read:{DIGEST}:ws:extra",                 # too many fields
        f"id::{DIGEST}",                              # no scopes
        f"id:read:{'z' * 64}",                        # not hex
        "id:read:tooshort",                           # not a sha256 digest
        f"id:read:{DIGEST.upper()[:63]}",             # wrong length
        f"id:read|superuser:{DIGEST}",                # unknown scope
        f"Bad Identity:read:{DIGEST}",                # not identifier-shaped
        f"id:read:{DIGEST}:Bad WS",                   # bad workstream
    ],
)
def test_rejects_a_malformed_spec_loudly(spec: str) -> None:
    # A typo must be a STARTUP error, never a silently weaker gate.
    with pytest.raises(TokenSpecError):
        parse_token_spec(spec)


def test_the_plaintext_secret_is_never_a_valid_third_field() -> None:
    # Guards the "store the digest, not the token" contract: pasting the secret
    # itself into config fails instead of quietly working.
    with pytest.raises(TokenSpecError):
        parse_token_spec(f"id:read:{FULL_SECRET}")


def test_scope_any_is_not_grantable() -> None:
    with pytest.raises(TokenSpecError):
        parse_token_spec(f"id:{SCOPE_ANY}:{DIGEST}")
    assert SCOPE_ANY not in ALL_SCOPES


def test_registry_rejects_duplicate_digests() -> None:
    tok = Token(identity="a", scopes={SCOPE_READ}, digest=DIGEST)
    other = Token(identity="b", scopes={SCOPE_READ}, digest=DIGEST)
    with pytest.raises(TokenSpecError):
        TokenRegistry([tok, other])


def test_registry_from_spec_accepts_whitespace_and_commas() -> None:
    raw = f"a:read:{token_digest('a')}, b:claim:{token_digest('b')}\n c:enqueue:{token_digest('c')}"
    reg = TokenRegistry.from_spec(raw)
    assert len(reg) == 3
    assert reg.identities == ["a", "b", "c"]


# --- Authentication ---------------------------------------------------------


def test_authenticates_by_digest_not_plaintext() -> None:
    reg = make_registry()
    assert reg.authenticate(FULL_SECRET).identity == FULL_IDENTITY
    # The digest itself is not a usable credential either.
    assert reg.authenticate(token_digest(FULL_SECRET)) is None
    assert reg.authenticate("not-a-token") is None
    assert reg.authenticate("") is None
    assert reg.authenticate(None) is None


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("  Bearer   abc  ", "abc"),
        ("abc", "abc"),          # bare value (minimal clients)
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_parses_the_bearer_header(header, expected) -> None:
    assert parse_bearer(header) == expected


# --- Fail closed ------------------------------------------------------------


def test_no_tokens_configured_refuses_everything() -> None:
    decision = authorize(
        TokenRegistry(()), None, authorization="Bearer anything", scope=SCOPE_READ
    )
    assert isinstance(decision, Denied)
    assert (decision.status, decision.reason) == (503, REASON_NO_TOKENS)


def test_missing_and_unknown_tokens_are_401() -> None:
    reg = make_registry()
    assert authorize(reg, None, authorization=None, scope=SCOPE_READ).reason == (
        REASON_NO_TOKEN
    )
    assert authorize(
        reg, None, authorization="Bearer nope", scope=SCOPE_READ
    ).reason == REASON_UNKNOWN_TOKEN


# --- Scopes + workstream pinning -------------------------------------------


def test_scope_is_enforced_per_token() -> None:
    reg = make_registry()
    ok = authorize(reg, None, authorization=f"Bearer {READONLY_SECRET}", scope=SCOPE_READ)
    assert isinstance(ok, Allowed) and ok.identity == READONLY_IDENTITY

    denied = authorize(
        reg, None, authorization=f"Bearer {READONLY_SECRET}", scope=SCOPE_ENQUEUE
    )
    assert isinstance(denied, Denied)
    assert (denied.status, denied.reason) == (403, REASON_MISSING_SCOPE)
    assert denied.identity == READONLY_IDENTITY  # attributable


def test_whoami_scope_needs_only_a_valid_token() -> None:
    reg = make_registry()
    decision = authorize(
        reg, None, authorization=f"Bearer {READONLY_SECRET}", scope=SCOPE_ANY
    )
    assert isinstance(decision, Allowed)


def test_pinned_token_is_confined_to_its_workstream() -> None:
    reg = make_registry()
    allowed = authorize(
        reg, None, authorization=f"Bearer {PINNED_SECRET}", scope=SCOPE_CLAIM,
        workstream=PINNED_WORKSTREAM,
    )
    assert isinstance(allowed, Allowed) and allowed.workstream == PINNED_WORKSTREAM

    denied = authorize(
        reg, None, authorization=f"Bearer {PINNED_SECRET}", scope=SCOPE_CLAIM,
        workstream="someone-elses-vertical",
    )
    assert isinstance(denied, Denied)
    assert (denied.status, denied.reason) == (403, REASON_WORKSTREAM_DENIED)


def test_pinned_token_defaults_to_its_single_workstream() -> None:
    # An unscoped request must NOT widen to "all workstreams": it resolves to the
    # single pinned one instead.
    allowed = authorize(
        make_registry(), None, authorization=f"Bearer {PINNED_SECRET}",
        scope=SCOPE_READ, workstream=None,
    )
    assert isinstance(allowed, Allowed) and allowed.workstream == PINNED_WORKSTREAM


def test_multi_pinned_token_must_be_explicit() -> None:
    reg = TokenRegistry([
        Token(
            identity="multi", scopes={SCOPE_READ}, digest=token_digest("multi"),
            workstreams=frozenset({"a", "b"}),
        )
    ])
    denied = authorize(reg, None, authorization="Bearer multi", scope=SCOPE_READ)
    assert isinstance(denied, Denied) and denied.reason == REASON_WORKSTREAM_DENIED
    ok = authorize(reg, None, authorization="Bearer multi", scope=SCOPE_READ, workstream="b")
    assert isinstance(ok, Allowed) and ok.workstream == "b"


def test_unpinned_token_may_stay_unscoped() -> None:
    allowed = authorize(
        make_registry(), None, authorization=f"Bearer {FULL_SECRET}", scope=SCOPE_READ
    )
    assert isinstance(allowed, Allowed) and allowed.workstream is None


# --- Rate limiting ----------------------------------------------------------


def test_bucket_allows_burst_then_denies() -> None:
    clock = [0.0]
    limiter = RateLimiter(rate_per_min=60, burst=3, clock=lambda: clock[0])
    assert [limiter.check("who") for _ in range(3)] == [None, None, None]
    retry_after = limiter.check("who")
    assert retry_after is not None and retry_after > 0


def test_bucket_refills_over_time() -> None:
    clock = [0.0]
    limiter = RateLimiter(rate_per_min=60, burst=1, clock=lambda: clock[0])
    assert limiter.check("who") is None
    assert limiter.check("who") is not None
    clock[0] += 1.0  # 60/min = one unit per second
    assert limiter.check("who") is None


def test_limits_are_per_identity() -> None:
    clock = [0.0]
    limiter = RateLimiter(rate_per_min=60, burst=1, clock=lambda: clock[0])
    assert limiter.check("a") is None
    assert limiter.check("a") is not None
    assert limiter.check("b") is None  # b is unaffected by a's flood


def test_rate_limit_precedes_authorization_checks() -> None:
    # A token spamming an endpoint it has NO scope for must still be throttled,
    # otherwise 403s are a free DoS channel.
    clock = [0.0]
    limiter = RateLimiter(rate_per_min=60, burst=1, clock=lambda: clock[0])
    reg = make_registry()
    first = authorize(
        reg, limiter, authorization=f"Bearer {READONLY_SECRET}", scope=SCOPE_ENQUEUE
    )
    second = authorize(
        reg, limiter, authorization=f"Bearer {READONLY_SECRET}", scope=SCOPE_ENQUEUE
    )
    assert first.reason == REASON_MISSING_SCOPE
    assert second.reason == REASON_RATE_LIMITED and second.status == 429


def test_unauthenticated_requests_never_consume_a_bucket() -> None:
    # Otherwise an anonymous flood could exhaust a legitimate identity's budget.
    clock = [0.0]
    limiter = RateLimiter(rate_per_min=60, burst=1, clock=lambda: clock[0])
    for _ in range(10):
        assert authorize(
            make_registry(), limiter, authorization="Bearer nope", scope=SCOPE_READ
        ).reason == REASON_UNKNOWN_TOKEN
    assert limiter.check(FULL_IDENTITY) is None
