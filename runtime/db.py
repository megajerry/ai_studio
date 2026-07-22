"""Connection helpers for the runtime store (psycopg 3, sync).

Kept deliberately thin: the data-access functions in :mod:`runtime.events` and
:mod:`runtime.tasks` take an open ``connection`` so the caller controls the
transaction boundary. This module only knows how to open one from the resolved
``DATABASE_URL`` (see :func:`runtime.models.build_database_url`).

Degraded-mode contract (ADR-0017)
---------------------------------
The database can be remote (a LAN host) and therefore *unreachable* for stretches
at a time. Every always-on component (worker, scheduler, supervisor, spokesman
bridge) MUST survive that without crashing or hanging. The contract is:

    On DB-unreachable: **log degraded, retry with bounded backoff, DON'T crash.**

This module provides the two primitives that enforce it:

- :class:`DBUnavailable` — the single, explicit *degraded signal*. A caller that
  can tolerate an outage catches exactly this (instead of a grab-bag of raw
  ``psycopg`` / socket errors) and degrades cleanly.
- :func:`connect_with_retry` — a bounded retry/backoff connect helper that either
  returns an open connection or raises :class:`DBUnavailable` once its (bounded)
  attempts are exhausted. It never hangs (each attempt is time-bounded) and never
  leaks a raw driver error to a degrade-aware caller.

:func:`can_connect` (a never-raising boolean probe) is kept for tests that skip
cleanly when no Postgres is available.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import psycopg
from psycopg.rows import dict_row

from .models import build_database_url

log = logging.getLogger("runtime.db")

# --- Degraded-mode defaults (overridable per call) --------------------------
#: Times :func:`connect_with_retry` tries to open a connection before giving up
#: with :class:`DBUnavailable`. Bounded so a caller's loop never blocks forever.
DEFAULT_CONNECT_ATTEMPTS = 3
#: First backoff sleep (seconds); doubles each retry up to ``max_delay_s``.
DEFAULT_BACKOFF_BASE_S = 0.5
#: Cap on any single backoff sleep, so backoff stays bounded on long outages.
DEFAULT_BACKOFF_MAX_S = 30.0
#: Per-attempt TCP/handshake bound (seconds) so a black-holed host can't hang.
DEFAULT_CONNECT_TIMEOUT_S = 5.0


class DBUnavailable(RuntimeError):
    """The database was unreachable after bounded retries (the degraded signal).

    Raised by :func:`connect_with_retry` when every attempt failed. Degrade-aware
    callers catch **this** (not raw driver errors) to log-degrade and back off
    rather than crash. The underlying error is chained (``raise ... from`` /
    ``__cause__``) and also carried on :attr:`last_error` for logging.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        last_error: Optional[BaseException] = None,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


def connect(
    url: Optional[str] = None,
    *,
    connect_timeout: Optional[float] = None,
) -> psycopg.Connection:
    """Open a psycopg connection with dict rows.

    ``connect_timeout`` (seconds) bounds the TCP/handshake wait so probes never
    hang when no database is reachable.

    This is the *single* attempt. Callers that must survive a transient or
    prolonged outage should use :func:`connect_with_retry`, which wraps this with
    bounded backoff and the :class:`DBUnavailable` degraded signal.
    """
    dsn = build_database_url() if url is None else url
    kwargs: dict[str, object] = {"row_factory": dict_row}
    if connect_timeout is not None:
        # psycopg passes unknown kwargs through to libpq; connect_timeout is
        # a standard libpq parameter (whole seconds).
        kwargs["connect_timeout"] = int(max(1, connect_timeout))
    return psycopg.connect(dsn, **kwargs)


def connect_with_retry(
    url: Optional[str] = None,
    *,
    attempts: int = DEFAULT_CONNECT_ATTEMPTS,
    base_delay_s: float = DEFAULT_BACKOFF_BASE_S,
    max_delay_s: float = DEFAULT_BACKOFF_MAX_S,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
) -> psycopg.Connection:
    """Open a connection, retrying with **bounded exponential backoff**.

    Enforces the degraded-mode contract: try up to ``attempts`` times, sleeping
    ``base_delay_s * 2**(n-1)`` (capped at ``max_delay_s``) between failed
    attempts. Each attempt is bounded by ``connect_timeout`` so a dead/black-holed
    host can never hang the caller.

    - Success → returns an open :class:`psycopg.Connection`.
    - All attempts fail → raises :class:`DBUnavailable` (never a raw driver
      error), so a degrade-aware caller has exactly one signal to catch.

    ``sleep``/``on_retry`` are injectable so tests can run instantly and assert
    the backoff schedule without real waiting. This helper itself does not log the
    final failure — that is the caller's degrade decision — but it invokes
    ``on_retry(attempt, delay, error)`` before each backoff so a loop can surface
    "degraded, retrying" without duplicating the schedule.
    """
    attempts = max(1, int(attempts))
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return connect(url, connect_timeout=connect_timeout)
        except Exception as exc:  # connection-time failure (unreachable/refused/timeout)
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)
    raise DBUnavailable(
        f"database unreachable after {attempts} attempt(s): {last_error}",
        attempts=attempts,
        last_error=last_error,
    ) from last_error


def can_connect(url: Optional[str] = None, *, timeout: float = 2.0) -> bool:
    """Return True if a database is reachable, else False.

    Never raises and never hangs longer than ``timeout`` — used by integration
    tests to skip cleanly when no Postgres is available (e.g. the off-host
    sandbox).
    """
    try:
        with connect(url, connect_timeout=timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False
