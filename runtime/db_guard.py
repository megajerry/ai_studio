"""Disposable-database guard — keep the LIVE studio DB sacred (ADR-0028).

The DB-backed test suite (and ``runtime.demo``) seed synthetic ``work.*`` /
``pollute-*`` tasks and workstreams. Historically those suites skipped only when
the database was *unreachable* (:func:`runtime.db.can_connect`), never when it was
a *production* database — so running ``make test`` against the live studio DB
permanently polluted the task queue (measured: tens of thousands of synthetic
tasks on one box).

This module answers a single, purely-local question: *is the target database
disposable (safe to seed and wipe), or is it a production DB we must never touch?*

A database is **disposable** iff:

1. The operator explicitly opts in via a truthy ``AI_STUDIO_TEST_DB`` env var
   (the sanctioned "yes, this box's DB is a throwaway" declaration), **or**
2. the target database *name* looks like a test database (case-insensitive: ends
   with ``_test`` or contains ``test``).

Otherwise it is treated as production and DB-backed tests must skip loudly (see
the repo-root ``conftest.py``). No secrets ever appear in the returned reason —
only the database NAME, which is not a credential (invariant 5).
"""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .models import build_database_url

#: Env var an operator sets (truthy) to declare "this box's DB is disposable".
OPT_IN_ENV = "AI_STUDIO_TEST_DB"

#: Values that count as *false* for a boolean-ish env var (everything else truthy).
_FALSEY = {"", "0", "false", "no", "off", "n"}

#: libpq keyword/value DSN form: ``... dbname=aistudio ...``.
_KV_DBNAME_RE = re.compile(r"(?:^|\s)dbname=([^\s]+)")


def _is_truthy(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() not in _FALSEY


def extract_dbname(dsn: str) -> Optional[str]:
    """Best-effort extract the database NAME from a resolved dsn/URL.

    Handles both the URL form (``postgresql://user@host:5432/aistudio?sslmode=..``)
    and the libpq keyword/value form (``host=.. dbname=aistudio ..``). Returns
    ``None`` when no name can be determined. Never raises.
    """
    if not dsn:
        return None
    try:
        if "://" in dsn:
            parts = urlsplit(dsn)
            # urlsplit already strips ``?query`` and ``#fragment`` off the path.
            name = parts.path.lstrip("/")
            return name or None
        m = _KV_DBNAME_RE.search(dsn)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001 - a malformed dsn is simply "unknown name"
        return None
    return None


def require_disposable_db(
    url: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[bool, str]:
    """Return ``(is_disposable, reason)`` for the resolved target database.

    ``url`` defaults to the resolved :func:`runtime.models.build_database_url`
    (``DATABASE_URL`` verbatim, else assembled from ``POSTGRES_*``). ``env``
    overrides ``os.environ`` (for testing). The reason string is safe to log /
    surface: it names only the database, never a credential.
    """
    env = os.environ if env is None else env

    if _is_truthy(env.get(OPT_IN_ENV)):
        return True, f"{OPT_IN_ENV} opt-in set (operator declared DB disposable)"

    dsn = build_database_url(env) if url is None else url
    dbname = extract_dbname(dsn)
    if dbname is None:
        return False, (
            "could not determine the target database name; refusing to treat it "
            f"as disposable — set {OPT_IN_ENV}=1 or use a *_test database"
        )

    low = dbname.lower()
    if low.endswith("_test") or "test" in low:
        return True, f"database name {dbname!r} matches the test pattern"

    return False, (
        f"refusing to run DB-backed tests against non-disposable DB {dbname!r}; "
        f"set {OPT_IN_ENV}=1 or use a *_test database"
    )
