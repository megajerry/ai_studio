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
from .memory import recall_lessons
from .migrate import migrate
from .policy import load_policy
from .roles.lessons import inject_lessons
from .roles.verifier import VerifyResult
from .scheduler import tick_once
from .skills import SkillRegistry
from .tasks import enqueue_task
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


#: A representative PM base prompt (mirrors runtime.roles.pm) for showing injection.
_PM_BASE_PROMPT = (
    "You are the studio PM. Restate the goal in one sentence and define ONE "
    "concrete, independently checkable success criterion for it. Goal: {goal}"
)


def _demonstrate_learning(conn, registry, config, worker_id: str) -> bool:
    """Show the learning loop end-to-end: a work task fails → Retro distills a
    lesson into Knowledge memory → the NEXT PM prompt for that workstream is shown
    to INCLUDE the recalled lesson (deterministic apply-the-lesson step)."""
    ws = f"learn-{uuid4().hex[:8]}"
    sink = DbEventSink(conn)
    query = "work.demo verification success criterion marker"
    base = _PM_BASE_PROMPT.format(goal="prove the studio learns")
    print(f"\n=== learning loop demo (workstream={ws}) ===")

    # 0. Baseline — this workstream has none of its OWN lessons yet (the shared
    #    global corpus may contribute some by design; we track the delta below).
    own_before = recall_lessons(conn, ws, query, k=10, include_global=False)
    print(f"  before retro: this workstream has {len(own_before)} lesson(s) of its own")

    # 1. A work task that FAILS verification (forced) → terminal 'failed' with
    #    max_attempts=1 → the worker triggers a Retro (WORKER_RETRO=on_fail default).
    def always_fail(conn_, task, result, s, **kw):
        return VerifyResult(passed=False, reason="marker check failed (injected for demo)")

    enqueue_task(
        conn, workstream=ws, type="work.demo",
        payload={"goal": "prove the studio learns",
                 "criterion": "artifact contains the marker",
                 "marker": f"studio-ok:{uuid4().hex[:6]}", "attempt": 1},
    )
    r_fail = run_once(conn, worker_id, sink, registry=registry, config=config,
                      workstream=ws, run_verify=always_fail, max_attempts=1)
    print(f"  work#1: {r_fail.kind} {r_fail.outcome} — {r_fail.detail}")

    # 2. Next worker pass claims the enqueued retro → distills + stores lesson(s).
    r_retro = run_once(conn, worker_id, sink, registry=registry, config=config,
                       workstream=ws)
    print(f"  retro:  {r_retro.kind} {r_retro.outcome} — {r_retro.detail}")

    # 3. The lesson is now in this workstream's Knowledge corpus.
    own_after = recall_lessons(conn, ws, query, k=10, include_global=False)
    print(f"  lesson learned: {len(own_after)}")

    # 4. The SUBSEQUENT PM prompt for this workstream now INCLUDES the lesson —
    #    auto-injected at prompt assembly (deterministic, not model memory).
    after = inject_lessons(base, conn, ws, query)
    has_section = "### Lessons" in after
    learned_present = bool(own_after) and own_after[0].text in after
    print(f"  next PM prompt contains '### Lessons' section: {has_section}")
    print(f"  next PM prompt includes the newly-learned lesson: {learned_present}")
    if own_after:
        print(f"    learned lesson> {own_after[0].text[:120]}")

    retro_ok = bool(r_retro and r_retro.kind == "retro" and r_retro.outcome == "done")
    return retro_ok and len(own_after) > len(own_before) and has_section and learned_present


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
    skills = SkillRegistry.discover()  # on-demand skills for the PM (ADR-0008)
    worker_id = "demo-worker"

    conn = db.connect()
    migrate(conn)
    try:
        sink = DbEventSink(conn)
        print(f"runtime.demo: workstream={workstream} scratch={scratch} "
              f"skills={len(skills)}")

        # 1. Scheduler pulse — enqueue a pm.tick (spawning the PM, ADR-0009).
        tick = tick_once(conn, workstream)
        print(f"  scheduler: enqueued {tick.type} {tick.id}" if tick else "  scheduler: (skipped)")

        # 2. Worker pass — PM plans + enqueues the work task (skills injected).
        r1 = run_once(conn, worker_id, sink, registry=registry, config=config,
                      skills=skills, workstream=workstream)
        print(f"  worker#1: {r1.kind} {r1.outcome} — {r1.detail}" if r1 else "  worker#1: nothing claimed")

        # 3. Worker pass — Executor does the work, Verifier checks (with the
        #    `rigorous-review` doctrine injected), commit on evidence.
        r2 = run_once(conn, worker_id, sink, registry=registry, config=config,
                      skills=skills, workstream=workstream)
        print(f"  worker#2: {r2.kind} {r2.outcome} — {r2.detail}" if r2 else "  worker#2: nothing claimed")

        _print_event_trail(conn, workstream)

        ok = bool(r2 and r2.kind == "work" and r2.outcome == "done")
        print(f"\nruntime.demo: {'OK — studio operated end-to-end' if ok else 'INCOMPLETE'}")

        # Second act: demonstrate the learning loop (retro → lesson → injection).
        learned = _demonstrate_learning(conn, registry, config, worker_id)
        print(f"runtime.demo: {'OK — studio learned (lesson distilled + injected)' if learned else 'LEARNING INCOMPLETE'}")

        return 0 if (ok and learned) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
