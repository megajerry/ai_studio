"""Task gateway — the studio's least-authority remote surface (ADR-0028).

A remote session that is **not on the Mac LAN** (a cloud agent container, a
laptop elsewhere) cannot reach Postgres, and it must never be given a DB
credential: a credential grants full SQL authority, which would bypass the
canonical lifecycle guard (``runtime.tasks.transition``, invariant 4) and put a
host secret in a less-trusted environment (invariant 5).

So the host exposes the **verbs** instead of the database: list / enqueue / claim
/ heartbeat / complete, each routed through :mod:`runtime.tasks`, behind a scoped,
rate-limited, hash-stored bearer token on an authenticated tunnel.

- :mod:`gateway.auth` — the security gates (pure, framework-free, unit-tested).
- :mod:`gateway.config` — env-driven settings (ADR-0011: nothing baked in).
- :mod:`gateway.app` — the FastAPI surface.
- :mod:`gateway.client` — a **stdlib-only** remote client + CLI, so a fresh cloud
  container needs no dependency install to use the queue.

Runbook: ``docs/remote-task-access.md``.
"""

from __future__ import annotations

__all__ = ["auth", "config"]
