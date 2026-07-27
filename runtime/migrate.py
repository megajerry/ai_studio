"""Minimal forward-only migration runner.

Applies ``runtime/migrations/*.sql`` in filename order against ``DATABASE_URL``,
recording each applied file in a ``schema_migrations`` table so re-runs are
idempotent. Each migration runs in its own transaction.

Usage:
    python -m runtime.migrate            # apply pending migrations
    python -m runtime.migrate --status   # list applied vs pending
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from .db import connect
from .models import build_database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def discover() -> list[Path]:
    """Return migration files sorted by filename (the NNNN_ prefix orders them)."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_MIGRATIONS_DDL)
        cur.execute("SELECT filename FROM schema_migrations")
        rows = {r["filename"] for r in cur.fetchall()}
    # Close our own transaction so we never hand a non-autocommit caller back a
    # connection left in an open read-tx. The trailing SELECT opens an implicit
    # transaction; if left dangling it snapshots the DB and keeps the caller's
    # OWN later writes from committing/being visible on fresh connections — a
    # footgun for any code that does `migrate(conn)` then writes on `conn`.
    # Committing here is safe (the DDL is idempotent) and a no-op under autocommit.
    if not conn.autocommit:
        conn.commit()
    return rows


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply all pending migrations; return the filenames applied this run."""
    done = _applied(conn)
    applied_now: list[str] = []
    for path in discover():
        if path.name in done:
            continue
        sql = path.read_text("utf-8")
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
        applied_now.append(path.name)
    return applied_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply runtime SQL migrations.")
    parser.add_argument(
        "--status", action="store_true", help="Show applied/pending, apply nothing."
    )
    args = parser.parse_args(argv)

    all_files = [p.name for p in discover()]
    if not all_files:
        print("No migrations found in", MIGRATIONS_DIR)
        return 0

    print(f"Connecting to {build_database_url()!r}")
    with connect() as conn:
        # Autocommit so each migration's own transaction commits independently:
        # a failure at file N must not roll back files already applied.
        conn.autocommit = True
        if args.status:
            done = _applied(conn)
            for name in all_files:
                print(("applied " if name in done else "pending ") + name)
            return 0

        applied_now = migrate(conn)
    if applied_now:
        print("Applied:", ", ".join(applied_now))
    else:
        print("Already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
