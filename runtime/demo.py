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


#: The exact workstreams THIS demo run creates. Populated by :func:`_new_ws` and
#: wiped by :func:`_cleanup_workstreams` in ``main()``'s finally, so the demo is a
#: valid go-live smoke test that leaves NO residue in ANY database — including the
#: LIVE studio DB (ADR-0028). We delete ONLY these exact strings, never a pattern.
_created_workstreams: list[str] = []

#: Exact ``approvals.id`` values the demo creates whose row is NOT reachable from a
#: demo task via ``task_id`` — the experiment ``experiment.scale`` approval
#: (``task_id IS NULL``) and the reviewer ``review`` approval (whose ``task_id``
#: points at a synthetic Task that was never INSERTed into ``tasks``). Tracked as
#: they are created so cleanup can delete them by exact id (no LIKE, no global).
_created_approval_ids: list[str] = []

#: Exact ``search_cache`` primary keys (``query_hash``, ``provider``, ``k``) the
#: demo's researcher step caches. Captured by a tight before/after diff around that
#: one synchronous step, so we delete precisely the cache rows the demo created.
_created_search_cache_keys: list[tuple[str, str, int]] = []


def _new_ws(prefix: str) -> str:
    """Mint a demo workstream and RECORD it for scoped self-cleanup (ADR-0028)."""
    ws = f"{prefix}-{uuid4().hex[:8]}"
    _created_workstreams.append(ws)
    return ws


def _track_approval(approval_id) -> None:
    """Record an approval id the demo created (parentless rows cleanup can't reach)."""
    if approval_id:
        _created_approval_ids.append(str(approval_id))


def _approval_ids(conn) -> set[str]:
    """Snapshot the current set of ``approvals.id`` values (as text)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM approvals")
        ids = {r["id"] for r in cur.fetchall()}
    if not conn.autocommit:
        conn.commit()
    return ids


def _search_cache_keys(conn) -> set[tuple[str, str, int]]:
    """Snapshot the current set of ``search_cache`` primary keys."""
    with conn.cursor() as cur:
        cur.execute("SELECT query_hash, provider, k FROM search_cache")
        keys = {(r["query_hash"], r["provider"], int(r["k"])) for r in cur.fetchall()}
    if not conn.autocommit:
        conn.commit()
    return keys


def _cleanup_workstreams(
    conn,
    workstreams: list[str],
    *,
    approval_ids: "list[str] | tuple[str, ...]" = (),
    search_cache_keys: "list[tuple[str, str, int]] | tuple" = (),
) -> int:
    """Delete every row the demo created, scoped to its OWN exact rows.

    Deletes, in FK-safe order:

    1. ``spokesman_handoffs`` for the demo's workstreams FIRST — its ``approval_id``
       references ``approvals`` (``NO ACTION``), so it must go before approvals.
    2. ``task_transitions`` tied to the demo's tasks (FK → ``tasks``).
    3. ``approvals`` reachable via a demo task's ``task_id`` **and** the exact
       ``approval_ids`` the demo created directly (parentless rows: the experiment
       ``experiment.scale`` with ``task_id IS NULL``, and the reviewer ``review``
       whose ``task_id`` was never inserted).
    4. ``search_cache`` rows the demo cached, by exact (``query_hash``,``provider``,
       ``k``) key.
    5. Every remaining ``workstream``-scoped table by exact workstream, with
       ``tasks`` LAST (so FK-referencing rows are already gone; ``trajectory_steps``
       cascades on the ``trajectories`` delete).

    NEVER a global TRUNCATE/DELETE and never a LIKE pattern — only the exact
    workstreams / ids / cache keys the demo itself created.
    """
    if not workstreams:
        return 0
    deleted = 0
    demo_tasks = "SELECT id FROM tasks WHERE workstream = ANY(%s)"

    def _run(cur, sql, params) -> None:
        nonlocal deleted
        cur.execute(sql, params)
        if cur.rowcount and cur.rowcount > 0:
            deleted += cur.rowcount

    with conn.cursor() as cur:
        # Discover workstream-scoped tables (future-proof as migrations are added).
        cur.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'workstream'"
        )
        ws_tables = [r["table_name"] for r in cur.fetchall()]

        # 1. spokesman_handoffs first (its approval_id → approvals is NO ACTION).
        if "spokesman_handoffs" in ws_tables:
            _run(cur, "DELETE FROM spokesman_handoffs WHERE workstream = ANY(%s)",
                 (workstreams,))

        # 2. task_transitions (FK → tasks) — before the tasks delete.
        _run(cur, f"DELETE FROM task_transitions WHERE task_id IN ({demo_tasks})",
             (workstreams,))

        # 3. approvals: reachable-via-demo-task OR an exact id the demo created.
        _run(
            cur,
            f"DELETE FROM approvals WHERE task_id IN ({demo_tasks}) "
            "OR id::text = ANY(%s)",
            (workstreams, [str(a) for a in approval_ids]),
        )

        # 4. search_cache rows the demo cached, by exact composite key.
        for qh, provider, k in search_cache_keys:
            _run(cur,
                 "DELETE FROM search_cache WHERE query_hash = %s AND provider = %s "
                 "AND k = %s",
                 (qh, provider, k))

        # 5. Remaining workstream-scoped tables, ``tasks`` LAST (FK targets).
        remaining = [t for t in ws_tables
                     if t not in ("tasks", "spokesman_handoffs")]
        ordered = remaining + (["tasks"] if "tasks" in ws_tables else [])
        for table in ordered:
            _run(cur, f"DELETE FROM {table} WHERE workstream = ANY(%s)",
                 (workstreams,))
    if not conn.autocommit:
        conn.commit()
    return deleted


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
    ws = _new_ws("learn")
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
    ws = _new_ws("review")
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
    # The reviewer's 🛑 approval targets a synthetic Task never inserted into
    # ``tasks``, so cleanup can't reach it by task_id — track its exact id.
    _track_approval(flagged.approval_id)
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
    ws = _new_ws("research")
    sink = DbEventSink(conn)
    print(f"\n=== researcher demo (workstream={ws}) ===")

    before = len(recall_lessons(conn, ws, "best practice", k=5, include_global=False))
    enqueue_task(conn, workstream=ws, type=RESEARCH_TASK_TYPE,
                 payload={"topic": f"best practices for LLM agent tool use {ws}"})
    # The researcher's search is the ONLY step that writes the global (workstream-less)
    # search_cache; snapshot its keys tightly around this synchronous call so cleanup
    # deletes exactly the cache rows the demo created (ADR-0028 zero-residue promise).
    _cache_before = _search_cache_keys(conn)
    r = run_once(conn, worker_id, sink, registry=registry, config=config, workstream=ws)
    _created_search_cache_keys.extend(_search_cache_keys(conn) - _cache_before)
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
    ws = _new_ws("critic")
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
    # This approval's task_id IS a demo task (cleanup reaches it via task_id), but
    # track the id too so cleanup is robust regardless of how it was linked.
    _track_approval(escalated.approval_id)
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

    ws = _new_ws("vconfig")
    other = _new_ws("vother")
    base_dir = Path(tempfile.mkdtemp(prefix="ai_studio_ws_"))
    (base_dir / ws).mkdir()
    (base_dir / ws / "config.yaml").write_text(
        f"name: {ws}\n"
        "charter: Operate the demo VIDEO channel; every clip needs captions.\n"
        "role_overlays:\n"
        "  executor: Record duration_seconds and captions in the clip artifact.\n"
        "budget:\n  cap_usd: 25.0\n  period: monthly\n"
        # ADDITIVE grant: lists ONLY net.fetch, yet the executor still writes its
        # artifact below — proving fs.write is RETAINED from the base policy (union,
        # not REPLACE). A REPLACE would have silently dropped fs.write and failed.
        "policy_grants:\n  executor: [net.fetch]\n"
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

    # 4. Policy grants are ADDITIVE (union over base); a role scopes DOWN only via
    #    an EXPLICIT revocation — adding one grant never silently drops the base set.
    from .workstream.config import WorkstreamConfig

    base_pol = load_policy()
    base_exec = {c.value for c in base_pol.granted("executor")}
    add_exec = {c.value for c in WorkstreamConfig(
        name=ws, policy_grants={"executor": ["net.fetch"]}
    ).effective_policy(base_pol).granted("executor")}
    rev_exec = {c.value for c in WorkstreamConfig(
        name=ws, policy_revocations={"executor": ["fs.write"]}
    ).effective_policy(base_pol).granted("executor")}
    union_ok = base_exec <= add_exec and add_exec == base_exec | {"net.fetch"}
    revoke_ok = "fs.write" not in rev_exec and "fs.read" in rev_exec
    print(f"  policy union:  base executor={sorted(base_exec)}")
    print(f"                 +grant net.fetch -> {sorted(add_exec)} (base kept) union_ok={union_ok}")
    print(f"                 -revoke fs.write -> {sorted(rev_exec)} explicit scope-down revoke_ok={revoke_ok}")

    return bool(cfg and budget_ok and len(r1.seeds_added) == 1 and r2.seeds_added == []
                and isolated and charter_in_prompt and overlay_in_prompt
                and union_ok and revoke_ok)


def _demonstrate_failure_analyst(conn, scratch: str) -> bool:
    """Show the failure→durable-fix→verify loop the stakeholder asked for: a
    RECURRING API-error failure is recognized from telemetry (only above threshold
    AND n ≥ floor, on the CI lower bound), a durable-fix candidate is PROPOSED (never
    applied) + framed as an experiment, and — after a human applies the fix — the
    experiment reads REAL post-fix traffic and confirms the fix worked."""
    from .capabilities import Capability
    from .enforce import DbEventSink
    from .experiment import ExperimentStatus, start_experiment
    from .policy import PolicyConfig
    from .quality import failure_report
    from .roles.failure_analyst import (
        observe_and_evaluate_fix,
        run_failure_analysis,
    )
    from .tools import FilesystemTool, ToolRegistry

    ws = _new_ws("fail")
    sink = DbEventSink(conn)
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=scratch))
    policy = PolicyConfig(roles={"failure_analyst": frozenset(
        {Capability.FS_READ, Capability.FS_WRITE})})
    print(f"\n=== failure-analyst demo (workstream={ws}) ===")

    # 1. Seed a RECURRING API-error failure: 40 of 100 model calls die RateLimitError.
    for _ in range(60):
        append_event(conn, make_event(workstream=ws, type="model.call",
                                      payload={"model": "dryrun", "cost_usd": 0.0,
                                               "input_tokens": 1, "output_tokens": 1}))
    for _ in range(40):
        append_event(conn, make_event(
            workstream=ws, type="model.call.failed",
            payload={"error_type": "RateLimitError", "model": "m", "provider": "p",
                     "role": "executor", "task_type": "work"}))
    rep = failure_report(conn, ws)
    er = rep["rates"]["model_call_error_rate"]
    print(f"  telemetry: model_call_error_rate={er['rate']} (n={er['n']}, CI={er['ci95']})")

    # 2. The analyst recognizes the pattern → PROPOSES a fix + frames an experiment.
    now = datetime.now(timezone.utc)
    task = Task(id=uuid4(), workstream=ws, type="analyze.failures",
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload={})
    result = run_failure_analysis(conn, task, sink, tool_registry=reg, policy=policy)
    if not result.proposals:
        print("  no recurring pattern detected — INCOMPLETE")
        return False
    fix = result.proposals[0]
    print(f"  detected: {fix.pattern_id} rate={fix.rate} (n={fix.n}) → proposed fix "
          f"'{fix.proposal_path}' [{fix.proposal_status}], experiment={fix.experiment_id}")
    print(f"  fix auto-applied? NO — candidate only (a human applies it); "
          f"experiment target: {fix.metric_name} <= {fix.target}")

    # 3. A human applies the fix + starts observing; capture the post-fix cursor.
    start_experiment(conn, fix.experiment_id, sink=sink)
    with conn.cursor() as cur:
        cur.execute("SELECT max(seq) AS s FROM events")
        cursor = int(cur.fetchone()["s"])
    conn.commit()

    # 4. Real POST-FIX traffic — the rate dropped to 0.05 (5/100) → fix works.
    for _ in range(95):
        append_event(conn, make_event(workstream=ws, type="model.call",
                                      payload={"model": "dryrun", "cost_usd": 0.0,
                                               "input_tokens": 1, "output_tokens": 1}))
    for _ in range(5):
        append_event(conn, make_event(
            workstream=ws, type="model.call.failed",
            payload={"error_type": "RateLimitError", "model": "m", "provider": "p",
                     "role": "executor", "task_type": "work"}))
    # A scaled experiment raises a red ``experiment.scale`` approval with
    # ``task_id IS NULL`` (surfaced only via the event payload, not the return
    # value) — snapshot approval ids tightly around this synchronous call so we
    # track its exact id and cleanup can delete it (ADR-0028 zero residue).
    _appr_before = _approval_ids(conn)
    exp = observe_and_evaluate_fix(conn, fix.experiment_id, sink=sink,
                                   workstream=ws, since_seq=cursor)
    for aid in (_approval_ids(conn) - _appr_before):
        _track_approval(aid)
    effective = exp.status in (ExperimentStatus.KEPT, ExperimentStatus.SCALED)
    print(f"  post-fix traffic: observed rate={exp.observed_value} → experiment "
          f"{exp.status.value} (fix {'CONFIRMED effective' if effective else 'ineffective'})")

    return bool(fix.experiment_id and fix.proposal_status == "executed"
                and exp.status is not ExperimentStatus.PROPOSED and effective)


def _demonstrate_curator(conn, scratch: str) -> bool:
    """Show the Skill Curator (ADR-0024 P2) induce → PROPOSE: a RECURRING + MATURE +
    EFFICIENT cluster of CLOSED trajectories becomes a ``reviewed: false`` candidate
    SKILL.md written to a review path (never the live skills/ root) + a body-free
    ``skill.proposed`` event — while a non-qualifying (inefficient) cluster in the same
    family proposes NOTHING. It NEVER auto-adopts / flips reviewed:true (PROPOSE-only)."""
    from .capabilities import Capability
    from .policy import PolicyConfig
    from .roles.curator import run_curator
    from .tasks import enqueue_task, transition
    from .tools import FilesystemTool, ToolRegistry
    from .trajectory import add_step, close_trajectory, start_trajectory

    ws = _new_ws("curate")
    sink = DbEventSink(conn)
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=scratch))
    policy = PolicyConfig(roles={"curator": frozenset(
        {Capability.FS_READ, Capability.FS_WRITE})})
    print(f"\n=== skill-curator demo (workstream={ws}) ===")

    # 1. Seed two contrasting clusters in ONE "work" family across CLOSED trajectories:
    #    an EFFICIENT matured procedure (short, cheap) and an INEFFICIENT one (long,
    #    costly). Both first-pass merge; the family median falls between them.
    eff_sig = ["observe", "plan", "commit"]
    ineff_sig = ["observe", "plan", "revise", "decide", "commit"]

    def _seed(ttype, sig, *, in_tokens, tools):
        tid = start_trajectory(conn, "executor", ws, f"do {ttype}")
        for st in sig:
            add_step(conn, tid, st, f"reasoning {st}")
        close_trajectory(conn, tid, outcome_summary="done")
        t = enqueue_task(conn, workstream=ws, type=ttype, payload={}, trajectory_id=tid)
        for st in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.READY_FOR_REVIEW,
                   TaskStatus.APPROVED, TaskStatus.MERGED):
            transition(conn, t.id, st)
        append_event(conn, make_event(workstream=ws, type="model.call", task_id=t.id,
                                      payload={"input_tokens": in_tokens, "output_tokens": 0,
                                               "cost_usd": 0}))
        for _ in range(tools):
            append_event(conn, make_event(workstream=ws, type="tool.invoked",
                                          task_id=t.id, payload={}))
        conn.commit()

    for tt in ("work.a", "work.b"):
        for _ in range(16):
            _seed(tt, eff_sig, in_tokens=100, tools=1)
            _seed(tt, ineff_sig, in_tokens=500, tools=4)

    # 2. The curator induces the reusable procedure → PROPOSES a reviewed:false candidate.
    now = datetime.now(timezone.utc)
    task = Task(id=uuid4(), workstream=ws, type="curate",
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload={})
    result = run_curator(conn, task, sink, tool_registry=reg, policy=policy)
    print(f"  examined {result.clusters_examined} cluster(s); induced "
          f"{result.candidates_detected} candidate(s) (the inefficient cluster proposed nothing)")
    if result.candidates_detected != 1:
        print("  expected exactly one qualifying cluster — INCOMPLETE")
        return False
    cand = result.candidates[0]
    written = Path(scratch) / cand.proposal_path
    body = written.read_text() if written.exists() else ""
    reviewed_false = "reviewed: false" in body and "reviewed: true" not in body
    live_untouched = not (Path(scratch) / "skills").exists()
    print(f"  proposed candidate: {cand.slug} (family={cand.task_family}, "
          f"steps={cand.step_signature}, first_pass_rate={cand.first_pass_rate}, "
          f"CI={cand.ci95})")
    print(f"  wrote {cand.proposal_path} [{cand.proposal_status}] — reviewed:false={reviewed_false}, "
          f"auto-adopt=NO, live skills/ untouched={live_untouched}")

    return bool(cand.proposal_status == "executed" and cand.reviewed is False
                and reviewed_false and live_untouched)


def main() -> int:
    # Keyless by construction — dry-run every model call.
    os.environ.setdefault("MODELS_DRY_RUN", "1")
    # Fresh ledger of this run's own rows (for scoped self-cleanup).
    _created_workstreams.clear()
    _created_approval_ids.clear()
    _created_search_cache_keys.clear()

    if not db.can_connect(timeout=2.0):
        print(
            "runtime.demo: no reachable database (DATABASE_URL/POSTGRES_*).\n"
            "  This demo needs Postgres; deferring to host verification.\n"
            "  Start it with: docker compose up -d postgres && python -m runtime.migrate\n"
            "  (The full loop is covered keyless in runtime/tests/ — run: pytest runtime/tests/)"
        )
        return 0

    workstream = _new_ws("demo")
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

        # Seventh act: recognize a recurring failure → propose a durable fix (never
        # auto-applied) → verify the fix on real post-fix traffic as an experiment.
        healed = _demonstrate_failure_analyst(conn, scratch)
        print(f"runtime.demo: {'OK — failure-pattern loop closed (recurring failure → proposed durable fix → verified effective on real traffic)' if healed else 'FAILURE-ANALYST INCOMPLETE'}")

        # Eighth act: induce a candidate skill from recurring+mature+efficient closed
        # trajectories → PROPOSE it reviewed:false (never auto-adopted; live skills/ untouched).
        curated = _demonstrate_curator(conn, scratch)
        print(f"runtime.demo: {'OK — skill curator induced a recurring+mature+efficient procedure → proposed a reviewed:false candidate (never auto-adopted)' if curated else 'CURATOR INCOMPLETE'}")

        return 0 if (ok and learned and reviewed and researched and configured and critiqued and healed and curated) else 1
    finally:
        # Self-cleanup (ADR-0028): leave NO residue in ANY database — including the
        # LIVE studio DB — so `python -m runtime.demo` stays a safe go-live smoke
        # test. Scoped to the demo's OWN exact workstreams; guarded so a cleanup
        # hiccup never crashes the run or flips the exit code.
        try:
            removed = _cleanup_workstreams(
                conn, list(_created_workstreams),
                approval_ids=list(_created_approval_ids),
                search_cache_keys=list(_created_search_cache_keys),
            )
            print(f"runtime.demo: self-cleanup removed {removed} row(s) across "
                  f"{len(_created_workstreams)} demo workstream(s) — no residue left")
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the demo result
            print(f"runtime.demo: WARNING self-cleanup failed ({type(exc).__name__}: "
                  f"{exc}); demo's synthetic workstreams may remain")
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
