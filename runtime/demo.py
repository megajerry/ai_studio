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
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import db
from .enforce import DbEventSink
from .events import append_event, read_events
from .memory import recall_lessons
from .migrate import migrate
from .models import Task, TaskStatus, make_event
from .policy import load_policy
from .roles.critic import (
    KIND_RISK,
    SEVERITY_HIGH,
    Concern,
    Critique,
    run_critic,
)
from .roles.lessons import inject_lessons
from .roles.pm import run_pm_tick
from .roles.researcher import RESEARCH_TASK_TYPE
from .roles.reviewer import REVIEW_TASK_TYPE, run_review
from .roles.verifier import VerifyResult
from .scheduler import tick_once
from .skills import SkillRegistry
from .tasks import enqueue_task
from .worker import build_registry, run_once


def _count_work_tasks(conn, workstream: str) -> int:
    """How many ``work.*`` tasks the PM enqueued for this workstream (decomposition)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type LIKE 'work.%%'",
            (workstream,),
        )
        n = cur.fetchone()["n"]
    if not conn.autocommit:
        conn.commit()
    return int(n)


def _print_event_trail(conn, workstream: str) -> None:
    events = read_events(conn, workstream=workstream)
    print(f"\n=== event trail ({len(events)} events, workstream={workstream}) ===")
    for ev in events:
        payload = ev.payload or {}
        # A compact, PII-free summary of each event's salient fields.
        keys = ("effect", "tier", "tool", "role", "model", "provider",
                "status", "passed", "reason", "work_item_count", "confidence",
                "work_task_id", "outcome")
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


def _review_task(ws: str, target_id, *, artifact_path, marker, scratch) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(), workstream=ws, type=REVIEW_TASK_TYPE, status=TaskStatus.IN_PROGRESS,
        priority=0, created_at=now, updated_at=now,
        payload={"target_task_id": str(target_id), "target_task_type": "work.demo",
                 "outcome": "done", "artifact_path": artifact_path, "marker": marker,
                 "spent_tokens": 0, "budget_tokens": None, "retries": 0},
    )


def _demonstrate_review(conn, registry, config, scratch: str) -> bool:
    """Show the independent Reviewer/Whistle-blower guard end-to-end: it PASSES a
    clean episode and FLAGS a hallucinated-success one (a done/verified CLAIM whose
    real artifact lacks the marker) — escalating with 🚨 review.alarm + a 🛑 approval.
    The verdict rests on the ACTUAL artifact evidence, never a claim (ADR-0014)."""
    ws = f"review-{uuid4().hex[:8]}"
    sink = DbEventSink(conn)
    print(f"\n=== reviewer / whistle-blower demo (workstream={ws}) ===")

    def _claim_trail(target_id):
        for typ in ("executor.acted", "verify.passed", "task.finished"):
            append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id,
                                          payload={"status": "done"}))

    # 1. Clean episode — the trail claims done AND the real artifact backs it.
    good_id = uuid4()
    good_marker = f"studio-ok:{uuid4().hex[:6]}"
    good_path = f"review-good-{good_id}.txt"
    _claim_trail(good_id)
    (Path(scratch) / good_path).write_text(f"{good_marker}\nall done\n")
    clean = run_review(conn, _review_task(ws, good_id, artifact_path=good_path,
                                          marker=good_marker, scratch=scratch),
                       sink, registry=registry, config=config)
    print(f"  clean episode:  review {'PASSED' if clean.ok else 'FLAGGED'} "
          f"(severity={clean.severity})")

    # 2. Hallucinated success — the trail CLAIMS done+verified, but the real
    #    artifact does NOT contain the success marker. Evidence beats the claim.
    bad_id = uuid4()
    bad_marker = f"studio-ok:{uuid4().hex[:6]}"
    bad_path = f"review-bad-{bad_id}.txt"
    _claim_trail(bad_id)
    (Path(scratch) / bad_path).write_text("looks fine, trust me — done!\n")  # no marker
    flagged = run_review(conn, _review_task(ws, bad_id, artifact_path=bad_path,
                                            marker=bad_marker, scratch=scratch),
                         sink, registry=registry, config=config)
    esc = " 🚨 alarm + 🛑 approval raised" if flagged.severity == "high" else ""
    print(f"  hallucinated:   review {'FLAGGED' if not flagged.ok else 'PASSED'} "
          f"(severity={flagged.severity}, {len(flagged.reasons)} signal(s)){esc}")
    if flagged.reasons:
        print(f"    reason> {flagged.reasons[0]}")

    return bool(clean.ok and (not flagged.ok) and flagged.severity == "high"
                and flagged.approval_id)


def _demonstrate_research(conn, registry, config, worker_id: str) -> bool:
    """Show the Researcher end-to-end: a `research` task → the worker dispatches it
    → search runs through the policy-gated cached gateway (net.fetch, keyless
    dry-run) → findings are distilled into recallable Knowledge lessons. The
    research task enqueues NOTHING (no research-loop)."""
    ws = f"research-{uuid4().hex[:8]}"
    sink = DbEventSink(conn)
    print(f"\n=== researcher demo (workstream={ws}) ===")

    before = len(recall_lessons(conn, ws, "best practice", k=5, include_global=False))
    enqueue_task(conn, workstream=ws, type=RESEARCH_TASK_TYPE,
                 payload={"topic": f"best practices for LLM agent tool use {ws}"})
    r = run_once(conn, worker_id, sink, registry=registry, config=config, workstream=ws)
    print(f"  worker (research): {r.kind} {r.outcome} — {r.detail}" if r else "  worker: nothing claimed")

    after = recall_lessons(conn, ws, "best practices LLM agent tool use", k=5,
                           include_global=False)
    print(f"  lessons distilled into Knowledge memory: {len(after)} (was {before})")
    if after:
        print(f"    distilled lesson> {after[0].text[:110]}")
    # No follow-on task was spawned by the research task (no loop).
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream = %s", (ws,))
        n_tasks = int(cur.fetchone()["n"])
    if not conn.autocommit:
        conn.commit()
    print(f"  tasks in this workstream: {n_tasks} (the research task spawned none)")
    return bool(r and r.kind == "research" and r.outcome == "done"
                and len(after) > before and n_tasks == 1)


def _demonstrate_critic(conn, skills) -> bool:
    """Show the Critic as a FORWARD adversarial partner + the PM↔Critic consensus
    loop: (1) on a healthy plan the Critic finds nothing blocking → the PM drives to
    consensus and decomposes; (2) on an unresolved genuine disagreement the loop is
    BOUNDED and escalates a 🛑 pushback to the stakeholder (never an infinite loop).
    Distinct from the after-the-fact Reviewer: it critiques BEFORE commitment."""
    ws = f"critic-{uuid4().hex[:8]}"
    sink = DbEventSink(conn)
    print(f"\n=== critic / PM↔Critic consensus demo (workstream={ws}) ===")

    # 1. Consensus — the real Critic critiques the PM's (dry-run) plan; nothing
    #    blocking → the PM proceeds and decomposes into work items.
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "prove the critic partners on decisions"})
    consented = run_pm_tick(conn, pm_task, sink, skills=skills,
                            critic=run_critic, critic_rounds=2)
    n_work = _count_work_tasks(conn, ws)
    print(f"  consensus: PM decision={consented.decision!r} → decomposed into {n_work} work item(s)")

    # 2. Escalation — a Critic that keeps a fundamental objection through the bound.
    #    The PM cannot drive to consensus → 🛑 pushback (no work enqueued).
    def stubborn(subject, context=None, **kw):
        run_critic(subject, context, sink=sink, conn=conn,
                   workstream=ws, task_id=kw.get("task_id"))  # still emits critic.reviewed
        return Critique(subject_kind="plan", blocking=True, recommendation="revise",
                        concerns=[Concern(kind=KIND_RISK, severity=SEVERITY_HIGH,
                                          statement="fundamental objection")])

    esc_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                            payload={"goal": "ship despite a fundamental objection"})
    escalated = run_pm_tick(conn, esc_task, sink, skills=skills,
                            critic=stubborn, critic_rounds=2)
    print(f"  escalation: PM decision={escalated.decision!r} "
          f"(🛑 approval raised={bool(escalated.approval_id)})")

    return bool(consented.decision == "planned" and n_work > 1
                and escalated.decision == "pushback" and escalated.approval_id)


def _demonstrate_workstream_config(conn, scratch: str) -> bool:
    """Show a vertical is CONFIG, not code: a workstreams/<name>/config.yaml drives
    the runtime. Bootstrap seeds its memory (idempotently) + budget; the config's
    charter/overlay flows into the role prompt; its domain checker + policy grants
    apply; and its seeded memory is scope-isolated from another workstream."""
    from .roles.executor import run_executor
    from .workstream import bootstrap_workstream, resolve_workstream_config
    from .worker import run_once

    ws = f"vconfig-{uuid4().hex[:8]}"
    other = f"vother-{uuid4().hex[:8]}"
    base_dir = Path(tempfile.mkdtemp(prefix="ai_studio_ws_"))
    (base_dir / ws).mkdir()
    (base_dir / ws / "config.yaml").write_text(
        f"name: {ws}\n"
        "charter: Operate the demo VIDEO channel; every clip needs captions.\n"
        "role_overlays:\n"
        "  executor: Record duration_seconds and captions in the clip artifact.\n"
        "budget:\n  cap_usd: 25.0\n  period: monthly\n"
        "policy_grants:\n  executor: [fs.read, fs.write]\n"
        "checkers: [video_audit]\n"
        "memory_seed:\n"
        "  - text: Never publish a clip without captions and a thumbnail.\n"
        "object_store_bucket: ws-demo-video\n"
    )
    sink = DbEventSink(conn)
    resolve = lambda name: resolve_workstream_config(name, base_dir=base_dir)  # noqa: E731
    print(f"\n=== workstream-config demo (workstream={ws}) ===")

    cfg = resolve(ws)
    print(f"  config loaded: charter+overlay set, checkers={cfg.checker_registry().names()}, "
          f"bucket={cfg.object_store_bucket!r}")

    # 1. Bootstrap seeds memory + budget; a re-run is idempotent (no duplicates).
    r1 = bootstrap_workstream(conn, cfg)
    r2 = bootstrap_workstream(conn, cfg)
    from .budget import get_budget

    budget_ok = get_budget(conn, ws) is not None
    print(f"  bootstrap: seeded {len(r1.seeds_added)} lesson(s) + budget={budget_ok}; "
          f"re-run added {len(r2.seeds_added)} (skipped {r2.seeds_skipped}) — idempotent")

    # 2. Scope isolation — the seed is recallable by THIS workstream, not another.
    mine = recall_lessons(conn, ws, "captions thumbnail publish", k=5, include_global=False)
    theirs = recall_lessons(conn, other, "captions thumbnail publish", k=5, include_global=False)
    isolated = bool(mine) and not theirs
    print(f"  scope isolation: this ws recalls {len(mine)} seed(s), other ws recalls "
          f"{len(theirs)} — isolated={isolated}")

    # 3. The config's charter+overlay flow into the ACTUAL role prompt.
    import runtime.roles.executor as exec_mod

    captured: dict = {}
    real_call = exec_mod.call_model
    def _capture(**kw):
        captured["prompt"] = kw["messages"][0]["content"]
        return real_call(**kw)
    exec_mod.call_model = _capture
    try:
        task = enqueue_task(conn, workstream=ws, type="work.task",
                            payload={"goal": "render clip", "criterion": "captioned",
                                     "marker": f"studio-ok:{uuid4().hex[:6]}"})
        # Claim + run the one work task through the worker with the config resolved.
        run_once(conn, "demo-worker", sink, registry=build_registry(scratch),
                 config=load_policy(), resolve_config=resolve, workstream=ws, max_attempts=1)
    finally:
        exec_mod.call_model = real_call
    charter_in_prompt = "Operate the demo VIDEO channel" in captured.get("prompt", "")
    overlay_in_prompt = "Record duration_seconds" in captured.get("prompt", "")
    print(f"  role prompt driven by config: charter={charter_in_prompt} overlay={overlay_in_prompt}")

    return bool(cfg and budget_ok and len(r1.seeds_added) == 1 and r2.seeds_added == []
                and isolated and charter_in_prompt and overlay_in_prompt)


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

        # 2. Worker pass — PM understands the goal, self-scores confidence, and
        #    DECOMPOSES it into N work items (skills injected).
        r1 = run_once(conn, worker_id, sink, registry=registry, config=config,
                      skills=skills, workstream=workstream)
        print(f"  worker#1 (PM): {r1.kind} {r1.outcome} — {r1.detail}" if r1 else "  worker#1: nothing claimed")
        n_work = _count_work_tasks(conn, workstream)
        print(f"  PM decomposed into {n_work} work item(s)")

        # 3. Worker passes — Executor does each work item, Verifier checks it (with
        #    the `rigorous-review` doctrine injected), commit on evidence. Drain ALL
        #    decomposed work items so the whole plan completes.
        done = 0
        while True:
            r = run_once(conn, worker_id, sink, registry=registry, config=config,
                         skills=skills, workstream=workstream)
            if r is None:
                break
            print(f"  worker (work): {r.kind} {r.outcome} — {r.detail}")
            if r.kind == "work" and r.outcome == "done":
                done += 1

        _print_event_trail(conn, workstream)

        ok = bool(n_work > 1 and done == n_work)
        print(f"\nruntime.demo: {'OK — studio operated end-to-end (PM decomposed into %d work items)' % n_work if ok else 'INCOMPLETE'}")

        # Second act: demonstrate the learning loop (retro → lesson → injection).
        learned = _demonstrate_learning(conn, registry, config, worker_id)
        print(f"runtime.demo: {'OK — studio learned (lesson distilled + injected)' if learned else 'LEARNING INCOMPLETE'}")

        # Third act: demonstrate the independent Reviewer/Whistle-blower risk guard.
        reviewed = _demonstrate_review(conn, registry, config, scratch)
        print(f"runtime.demo: {'OK — reviewer guarded (clean passed, hallucination flagged + escalated)' if reviewed else 'REVIEW INCOMPLETE'}")

        # Fourth act: demonstrate the Researcher (search gateway → distilled lessons).
        researched = _demonstrate_research(conn, registry, config, worker_id)
        print(f"runtime.demo: {'OK — researcher mined external best-practice into recallable lessons' if researched else 'RESEARCH INCOMPLETE'}")

        # Fifth act: demonstrate a vertical is CONFIG, not code (workstream config).
        configured = _demonstrate_workstream_config(conn, scratch)
        print(f"runtime.demo: {'OK — vertical defined by config drove the runtime (charter/checker/budget/policy/seed, scope-isolated)' if configured else 'WORKSTREAM-CONFIG INCOMPLETE'}")

        # Sixth act: demonstrate the Critic (forward partner) + PM↔Critic consensus.
        critiqued = _demonstrate_critic(conn, skills)
        print(f"runtime.demo: {'OK — critic partnered forward (consensus decomposed, unresolved disagreement escalated 🛑)' if critiqued else 'CRITIC INCOMPLETE'}")

        return 0 if (ok and learned and reviewed and researched and configured and critiqued) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
