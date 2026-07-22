"""End-to-end studio demo — the "studio operates" proof on the host (M3c).

Drives the full agent-driven loop against a **real** database, fully keyless
(dry-run): scheduler tick → PM plans + enqueues work → Executor does a tool +
model call → Verifier independently checks → commit. It then prints the event
trail so the whole run is visible as one replayable story.

Run it::

    python -m runtime.demo

Requires a reachable Postgres (``DATABASE_URL`` / ``POSTGRES_*``). With **no**
database it prints a clear notice and exits 0 (deferred to host verification) —
it never hangs. Forces ``MODELS_DRY_RUN`` so no API key is ever needed.
"""

from __future__ import annotations

import os
import tempfile
from uuid import uuid4

from . import db
from .enforce import DbEventSink
from .events import read_events
from .migrate import migrate
from .policy import load_policy
from .scheduler import tick_once
from .worker import build_registry, run_once


def _print_event_trail(conn, workstream: str) -> None:
    events = read_events(conn, workstream=workstream)
    print(f"\n=== event trail ({len(events)} events, workstream={workstream}) ===")
    for ev in events:
        payload = ev.payload or {}
        # A compact, PII-free summary of each event's salient fields.
        keys = ("effect", "tier", "tool", "role", "model", "provider",
                "status", "passed", "reason", "work_task_id", "outcome")
        bits = " ".join(f"{k}={payload[k]}" for k in keys if k in payload)
        print(f"  {ev.ts:%H:%M:%S} {ev.type:<20} {bits}")


def main() -> int:
    # Keyless by construction — dry-run every model call.
    os.environ.setdefault("MODELS_DRY_RUN", "1")

    if not db.can_connect(timeout=2.0):
        print(
            "runtime.demo: no reachable database (DATABASE_URL/POSTGRES_*).\n"
            "  This demo needs Postgres; deferring to host verification.\n"
            "  Start it with: docker compose up -d postgres && python -m runtime.migrate\n"
            "  (The full loop is covered keyless in runtime/tests/ — run: pytest runtime/tests/)"
        )
        return 0

    workstream = f"demo-{uuid4().hex[:8]}"
    scratch = tempfile.mkdtemp(prefix="ai_studio_demo_")
    registry = build_registry(scratch)
    config = load_policy()
    worker_id = "demo-worker"

    conn = db.connect()
    migrate(conn)
    try:
        sink = DbEventSink(conn)
        print(f"runtime.demo: workstream={workstream} scratch={scratch}")

        # 1. Scheduler pulse — enqueue a pm.tick (spawning the PM, ADR-0009).
        tick = tick_once(conn, workstream)
        print(f"  scheduler: enqueued {tick.type} {tick.id}" if tick else "  scheduler: (skipped)")

        # 2. Worker pass — PM plans + enqueues the work task.
        r1 = run_once(conn, worker_id, sink, registry=registry, config=config,
                      workstream=workstream)
        print(f"  worker#1: {r1.kind} {r1.outcome} — {r1.detail}" if r1 else "  worker#1: nothing claimed")

        # 3. Worker pass — Executor does the work, Verifier checks, commit.
        r2 = run_once(conn, worker_id, sink, registry=registry, config=config,
                      workstream=workstream)
        print(f"  worker#2: {r2.kind} {r2.outcome} — {r2.detail}" if r2 else "  worker#2: nothing claimed")

        _print_event_trail(conn, workstream)

        ok = bool(r2 and r2.kind == "work" and r2.outcome == "done")
        print(f"\nruntime.demo: {'OK — studio operated end-to-end' if ok else 'INCOMPLETE'}")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
