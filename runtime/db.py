"""Connection helpers for the runtime store (psycopg 3, sync).

Kept deliberately thin: the data-access functions in :mod:`runtime.events` and
:mod:`runtime.tasks` take an open ``connection`` so the caller controls the
transaction boundary. This module only knows how to open one from the resolved
``DATABASE_URL`` (see :func:`runtime.models.build_database_url`).
"""

from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.rows import dict_row

from .models import build_database_url


def connect(
    url: Optional[str] = None,
    *,
    connect_timeout: Optional[float] = None,
) -> psycopg.Connection:
    """Open a psycopg connection with dict rows.

    ``connect_timeout`` (seconds) bounds the TCP/handshake wait so probes never
    hang when no database is reachable.
    """
    dsn = build_database_url() if url is None else url
    kwargs: dict[str, object] = {"row_factory": dict_row}
    if connect_timeout is not None:
        # psycopg passes unknown kwargs through to libpq; connect_timeout is
        # a standard libpq parameter (whole seconds).
        kwargs["connect_timeout"] = int(max(1, connect_timeout))
    return psycopg.connect(dsn, **kwargs)


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
