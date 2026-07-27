"""Security gates for the remote task gateway (ADR-0027) — pure, framework-free.

Everything that decides *whether a remote may act* lives here, with no FastAPI /
psycopg import, so a reviewer can read the whole trust boundary in one file and
every gate is unit-testable without a server or a database.

The gates, in the order a request meets them:

1. **A bearer token is required and the surface fails CLOSED.** No tokens
   configured ⇒ :class:`TokenRegistry` is empty and *every* authenticated verb is
   refused (``no_tokens_configured``) — a half-configured deploy is unusable,
   never open.
2. **Tokens are matched by SHA-256 digest**, never by plaintext. Config carries
   only the digest, so the host env (and a leaked ``.env``) holds no usable
   credential; the comparison is :func:`hmac.compare_digest` over the hex digest
   and scans the whole registry (no early exit) so it is constant-time in both
   the secret and which token matched.
3. **Scopes** (``read`` / ``enqueue`` / ``claim`` / ``complete``) — least
   authority per token; a read-only remote cannot mutate the queue.
4. **Workstream pinning** — a token may be restricted to named workstreams, which
   keeps vertical isolation (ADR-0018) intact across the remote boundary.
5. **Per-identity rate limit** (token bucket) so a leaked token cannot flood the
   queue or brute-force task ids.

A token's ``identity`` is also the queue identity (``claimed_by`` / ``agent_id``),
so every remote action is attributable in ``task_transitions`` + the event log,
and the ADR-0021 trust ledger's ``revoked``/``quarantined`` fence applies to
remotes for free.

Nothing here ever logs or returns a token (only the identity + a reason CODE).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Union

# --- Scopes -----------------------------------------------------------------

#: Read the queue (list ready/waiting/for-review, read one task).
SCOPE_READ = "read"
#: Create new tasks.
SCOPE_ENQUEUE = "enqueue"
#: Grab + start a task, and heartbeat one this identity holds.
SCOPE_CLAIM = "claim"
#: Finalize a task this identity holds (merged / abandoned).
SCOPE_COMPLETE = "complete"

ALL_SCOPES: frozenset[str] = frozenset(
    {SCOPE_READ, SCOPE_ENQUEUE, SCOPE_CLAIM, SCOPE_COMPLETE}
)

#: Requires a VALID token but no particular scope — used only by ``/v1/whoami``,
#: which reveals nothing a caller doesn't already hold. It is NOT a grantable
#: scope: :func:`parse_token_spec` rejects it (it is not in :data:`ALL_SCOPES`),
#: so it can never appear in configuration.
SCOPE_ANY = "*"

#: Denial reason CODES (never free text): what the audit event records.
REASON_NO_TOKENS = "no_tokens_configured"
REASON_NO_TOKEN = "no_token"
REASON_UNKNOWN_TOKEN = "unknown_token"
REASON_MISSING_SCOPE = "missing_scope"
REASON_WORKSTREAM_DENIED = "workstream_denied"
REASON_RATE_LIMITED = "rate_limited"
REASON_NOT_OWNER = "not_owner"

#: Identities/workstreams/task types are identifier-shaped, never free text: a
#: gateway-supplied value ends up in ``claimed_by`` / ``workstream`` / ``type``
#: and in event payloads, so keep it to a boring, loggable alphabet.
_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,63}$")
#: A 64-char lowercase hex SHA-256 digest.
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def is_identifier(value: object) -> bool:
    """True iff ``value`` is a safe, identifier-shaped string (see :data:`_IDENT_RE`)."""
    return isinstance(value, str) and bool(_IDENT_RE.match(value))


def token_digest(secret: str) -> str:
    """SHA-256 hex digest of a token secret — the only form ever stored/compared."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class TokenSpecError(ValueError):
    """A ``TASK_GATEWAY_TOKENS`` entry is malformed (raised at startup, not per request)."""


@dataclass(frozen=True)
class Token:
    """One configured remote credential — *the digest*, never the secret.

    ``workstreams`` empty means "not pinned" (any workstream the verb allows).
    """

    identity: str
    scopes: frozenset[str]
    digest: str
    workstreams: frozenset[str] = field(default_factory=frozenset)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def allows_workstream(self, workstream: Optional[str]) -> bool:
        """True iff this token may act on ``workstream`` (``None`` = unscoped read).

        An unpinned token allows anything; a pinned token allows only its named
        workstreams — and refuses an unscoped (``None``) request, because "all
        workstreams" is exactly what pinning takes away.
        """
        if not self.workstreams:
            return True
        return workstream is not None and workstream in self.workstreams

    def default_workstream(self) -> Optional[str]:
        """The implied workstream when a pinned token names exactly one, else ``None``."""
        if len(self.workstreams) == 1:
            return next(iter(self.workstreams))
        return None


def parse_token_spec(spec: str) -> Token:
    """Parse one ``identity:scopes:digest[:workstreams]`` entry.

    ``scopes`` and ``workstreams`` are ``|``-separated (so the entry separator and
    the field separator can never be confused), e.g.::

        offhost-cursor:read|enqueue|claim:3f8c…64hex:video|productivity

    Rejects an unknown scope, a non-identifier identity/workstream and anything
    that is not a 64-char hex digest — a typo is a startup error (``TokenSpecError``),
    never a silently weaker gate.
    """
    parts = [p.strip() for p in spec.strip().split(":")]
    if len(parts) not in (3, 4):
        raise TokenSpecError(
            "token spec must be identity:scopes:sha256hex[:workstreams] "
            f"(got {len(parts)} field(s))"
        )
    identity, raw_scopes, digest = parts[0], parts[1], parts[2].lower()
    raw_workstreams = parts[3] if len(parts) == 4 else ""

    if not is_identifier(identity):
        raise TokenSpecError(f"invalid token identity {identity!r}")
    if not _DIGEST_RE.match(digest):
        raise TokenSpecError(
            f"token {identity!r}: third field must be a 64-char sha256 hex digest "
            "(store the DIGEST, never the token itself)"
        )
    scopes = frozenset(s for s in (x.strip() for x in raw_scopes.split("|")) if s)
    if not scopes:
        raise TokenSpecError(f"token {identity!r}: no scopes")
    unknown = sorted(scopes - ALL_SCOPES)
    if unknown:
        raise TokenSpecError(
            f"token {identity!r}: unknown scope(s) {unknown} "
            f"(allowed: {sorted(ALL_SCOPES)})"
        )
    workstreams = frozenset(
        w for w in (x.strip() for x in raw_workstreams.split("|")) if w
    )
    bad = sorted(w for w in workstreams if not is_identifier(w))
    if bad:
        raise TokenSpecError(f"token {identity!r}: invalid workstream(s) {bad}")
    return Token(
        identity=identity, scopes=scopes, digest=digest, workstreams=workstreams
    )


class TokenRegistry:
    """The configured remote credentials, looked up in constant time by digest."""

    def __init__(self, tokens: Iterable[Token] = ()):
        self._tokens: list[Token] = list(tokens)
        seen: set[str] = set()
        for tok in self._tokens:
            if tok.digest in seen:
                raise TokenSpecError(
                    f"duplicate token digest for identity {tok.identity!r}"
                )
            seen.add(tok.digest)

    @classmethod
    def from_spec(cls, raw: Optional[str]) -> "TokenRegistry":
        """Parse the whitespace/comma-separated ``TASK_GATEWAY_TOKENS`` value.

        An empty/unset value yields an EMPTY registry — the fail-closed state in
        which every authenticated verb is refused.
        """
        if not raw or not raw.strip():
            return cls(())
        entries = [e for e in re.split(r"[\s,]+", raw.strip()) if e]
        return cls(parse_token_spec(e) for e in entries)

    def __len__(self) -> int:
        return len(self._tokens)

    @property
    def identities(self) -> list[str]:
        """Configured identities (safe to log — never the digests)."""
        return [t.identity for t in self._tokens]

    def authenticate(self, presented: Optional[str]) -> Optional[Token]:
        """Return the :class:`Token` a presented secret belongs to, else ``None``.

        Hashes the presented secret once, then compares against **every**
        configured digest with :func:`hmac.compare_digest` and no early exit, so
        neither the digest bytes nor *which* token matched is observable by
        timing.
        """
        if not presented or not self._tokens:
            return None
        presented_digest = token_digest(presented)
        matched: Optional[Token] = None
        for tok in self._tokens:
            if hmac.compare_digest(presented_digest, tok.digest):
                matched = tok
        return matched


def parse_bearer(header: Optional[str]) -> Optional[str]:
    """Extract the secret from an ``Authorization: Bearer <token>`` header.

    Also accepts a bare value (some minimal clients send only the token) so the
    remote CLI stays dependency-free, but never accepts an empty credential.
    """
    if not header:
        return None
    value = header.strip()
    lowered = value.lower()
    if lowered == "bearer":  # the scheme with no credential is not a credential
        return None
    if lowered.startswith("bearer "):
        value = value[7:].strip()
    return value or None


# --- Rate limiting ----------------------------------------------------------


class RateLimiter:
    """Per-identity token bucket: ``rate_per_min`` sustained, ``burst`` at once.

    Bounds what a *valid but compromised* token can do (queue flooding, id
    brute-forcing). In-process and monotonic-clock based — deliberately simple:
    this is one small single-replica service, and a shared-state limiter would
    add a Redis dependency to the security path for no gain at this scale.
    ``clock`` is injectable so tests assert refill without sleeping.
    """

    def __init__(
        self,
        rate_per_min: int,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.rate_per_s = max(0.0, float(rate_per_min) / 60.0)
        self.burst = max(1, int(burst))
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # identity -> (tokens, at)

    def check(self, identity: str) -> Optional[float]:
        """Consume one unit for ``identity``.

        Returns ``None`` when allowed, else the ``Retry-After`` seconds to report.
        """
        now = self._clock()
        tokens, at = self._buckets.get(identity, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - at) * self.rate_per_s)
        if tokens < 1.0:
            self._buckets[identity] = (tokens, now)
            if self.rate_per_s <= 0:
                return 60.0
            return max(1.0, round((1.0 - tokens) / self.rate_per_s, 3))
        self._buckets[identity] = (tokens - 1.0, now)
        return None


# --- The composed decision --------------------------------------------------


@dataclass(frozen=True)
class Denied:
    """A refused request: an HTTP status + a reason CODE (never free-form text)."""

    status: int
    reason: str
    identity: Optional[str] = None
    retry_after: Optional[float] = None


@dataclass(frozen=True)
class Allowed:
    """An authorized request, carrying the acting identity + resolved workstream."""

    token: Token
    workstream: Optional[str]

    @property
    def identity(self) -> str:
        return self.token.identity


def authorize(
    registry: TokenRegistry,
    limiter: Optional[RateLimiter],
    *,
    authorization: Optional[str],
    scope: str,
    workstream: Optional[str] = None,
) -> Union[Allowed, Denied]:
    """Run gates 1–5 for one request. THE single entry point used by the app.

    ``workstream`` is the caller-requested workstream (``None`` = unspecified). For
    a pinned token that names exactly one workstream, an unspecified request
    resolves to it; a pinned token that names several must be explicit (the
    request is refused rather than silently widened).
    """
    if len(registry) == 0:
        return Denied(503, REASON_NO_TOKENS)

    secret = parse_bearer(authorization)
    if secret is None:
        return Denied(401, REASON_NO_TOKEN)
    token = registry.authenticate(secret)
    if token is None:
        return Denied(401, REASON_UNKNOWN_TOKEN)

    # Rate limit BEFORE the authorization checks so *all* authenticated traffic is
    # bounded — including a token hammering endpoints it has no scope for.
    if limiter is not None:
        retry_after = limiter.check(token.identity)
        if retry_after is not None:
            return Denied(
                429, REASON_RATE_LIMITED,
                identity=token.identity, retry_after=retry_after,
            )

    if scope != SCOPE_ANY and not token.has_scope(scope):
        return Denied(403, REASON_MISSING_SCOPE, identity=token.identity)

    resolved = workstream if workstream is not None else token.default_workstream()
    if not token.allows_workstream(resolved):
        return Denied(403, REASON_WORKSTREAM_DENIED, identity=token.identity)

    return Allowed(token=token, workstream=resolved)
