"""The worker — the on-demand driver that runs one task to completion (M3c).

"Spawning an agent" = enqueuing a task (ADR-0009); the worker is what
materializes to service it. It is a **non-LLM driver** (the reasoning lives in the
roles): it claims a task, dispatches by type, heartbeats while the role works, and
turns the Verifier's verdict into the terminal state — enforcing verify→commit
(architecture §4, CLAUDE.md invariant 4): a work task is never ``done`` until the
Verifier passes.

Dispatch:

- ``pm.tick`` → :func:`runtime.roles.pm.run_pm_tick` (plan + enqueue work), commit.
- ``work.*``  → :func:`runtime.roles.executor.run_executor` then
  :func:`runtime.roles.verifier.verify`; on pass → ``complete_task(done)``; on
  fail → a **bounded** re-enqueue (nudge) or ``complete_task(failed)``.

All coordination is through the merged M1 queue + event log; all tools go through
the M2 policy gate (inside the roles); all model calls go through M3b
``call_model`` (inside the roles). The worker itself calls no model and touches no
host state except through those seams.

Run it as an on-demand driver::

    python -m runtime.worker

Config (env): ``WORKER_ID``, ``WORKER_SCRATCH_DIR`` (tool root),
``WORKER_IDLE_SLEEP_S`` (poll gap when the queue is empty),
``WORKER_MAX_WORK_ATTEMPTS`` (verify-fail re-enqueues before failing),
``WORKER_RETRO`` (``on_fail`` (default) | ``always`` | ``off`` — when a terminal
work task triggers a learning Retro).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any, Callable, Optional
from uuid import uuid4

import psycopg
from pydantic import BaseModel

from .approvals import (
    STATUS_APPROVED,
    STATUS_DENIED,
    get_approval,
)
from .db import connect
from .enforce import DbEventSink, EventSink, InvokeStatus, NullEventSink
from .models import Assignee, Task, TaskStatus, make_event
from .policy import PolicyConfig, load_policy
from .roles.executor import run_executor
from .roles.pm import run_pm_tick
from .roles.retro import RETRO_TASK_TYPE, run_retro as run_retro_default
from .roles.verifier import verify as run_verify_default
from .scheduler import PM_TICK_TYPE
from .skills import SkillRegistry
from .tasks import (
    block_task,
    claim_task,
    complete_task,
    enqueue_task,
    find_blocked_tasks,
    heartbeat,
    requeue_blocked_task,
)
from .tools import FilesystemTool, ShellTool, ToolRegistry

log = logging.getLogger("runtime.worker")

#: Event emitted when the worker re-enqueues a work task after a verify fail.
EVENT_WORK_RETRY = "work.retry"

#: Event emitted when the worker enqueues a retro after a terminal work task.
EVENT_RETRO_TRIGGERED = "retro.triggered"

#: Event emitted when a task blocked on a 🔴 approval is re-queued after a grant.
EVENT_APPROVAL_RESUMED = "approval.resumed"

DEFAULT_IDLE_SLEEP_S = 5.0
DEFAULT_MAX_WORK_ATTEMPTS = 2

#: When the worker fires a Retro after a work task reaches a terminal state.
#: ``on_fail`` (default) keeps cost down + matches ADR-0003 "more retro when errors";
#: ``always`` retros every episode; ``off`` disables the learning loop's trigger.
RETRO_ON_FAIL = "on_fail"
RETRO_ALWAYS = "always"
RETRO_OFF = "off"
DEFAULT_RETRO_MODE = RETRO_ON_FAIL
_RETRO_MODES = {RETRO_ON_FAIL, RETRO_ALWAYS, RETRO_OFF}

# Injectable seams — defaults are the real M1/M3b/role functions; tests pass fakes
# so `run_once` drives the full loop with no database (same idiom as supervisor).
Claimer = Callable[..., Optional[Task]]
Heartbeater = Callable[..., Optional[Task]]
Completer = Callable[..., Optional[Task]]
Enqueuer = Callable[..., Task]
Blocker = Callable[..., Optional[Task]]


class RunResult(BaseModel):
    """What one :func:`run_once` pass did — for logging/telemetry and tests."""

    task_id: str
    task_type: str
    #: "pm" | "work" | "retro" | "unknown"
    kind: str
    #: "done" | "retry" | "failed" | "blocked"
    outcome: str
    detail: str = ""


def build_registry(scratch_dir: str) -> ToolRegistry:
    """Build the worker's tool registry: a filesystem tool confined to
    ``scratch_dir`` plus the (host-refusing) shell tool. The scratch root need not
    pre-exist — the filesystem tool creates parents on write."""
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=scratch_dir))
    reg.register(ShellTool())
    return reg


def _handle_pm_tick(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    model_registry,
    enqueue: Enqueuer,
    heartbeat: Heartbeater,
    complete: Completer,
    worker_id: str,
    run_pm: Callable[..., Any],
    skills: Optional[SkillRegistry] = None,
) -> RunResult:
    plan = run_pm(conn, task, sink, registry=model_registry, skills=skills, enqueue=enqueue)
    heartbeat(conn, task.id, worker_id)
    complete(conn, task.id, result=plan.model_dump(), status=TaskStatus.DONE)
    return RunResult(
        task_id=str(task.id),
        task_type=task.type,
        kind="pm",
        outcome="done",
        detail=f"enqueued work task {plan.work_task_id}",
    )


def _resolve_retro_mode(retro_mode: Optional[str]) -> str:
    """Resolve the retro trigger policy (arg > ``WORKER_RETRO`` env > default)."""
    mode = (retro_mode if retro_mode is not None else os.environ.get("WORKER_RETRO", "")).strip().lower()
    if mode in _RETRO_MODES:
        return mode
    if mode:
        log.warning("invalid WORKER_RETRO=%r; using default %s", mode, DEFAULT_RETRO_MODE)
    return DEFAULT_RETRO_MODE


def _maybe_enqueue_retro(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    enqueue: Enqueuer,
    outcome: str,
    detail: str,
    retro_mode: str,
) -> None:
    """Enqueue ONE retro task for a terminal work task, per the retro policy.

    Bounded by construction: only ``work.*`` terminal states reach here, the retro
    task is a distinct type (never ``work.*``/``pm.tick``), and a retro NEVER
    enqueues another task — so there is no retro-of-a-retro loop. ``on_fail`` fires
    only on a failed episode; ``always`` on every terminal episode; ``off`` never.
    """
    if retro_mode == RETRO_OFF:
        return
    if retro_mode == RETRO_ON_FAIL and outcome != "failed":
        return
    retro = enqueue(
        conn,
        workstream=task.workstream,
        type=RETRO_TASK_TYPE,
        payload={
            "target_task_id": str(task.id),
            "target_task_type": task.type,
            "outcome": outcome,
            "reason": detail,
        },
        priority=task.priority,
    )
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_RETRO_TRIGGERED,
            task_id=task.id,
            payload={"retro_task_id": str(retro.id), "outcome": outcome, "mode": retro_mode},
        )
    )


def _handle_work(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
    model_registry,
    heartbeat: Heartbeater,
    complete: Completer,
    enqueue: Enqueuer,
    block: Blocker,
    worker_id: str,
    max_attempts: int,
    run_exec: Callable[..., Any],
    run_verify: Callable[..., Any],
    retro_mode: str = DEFAULT_RETRO_MODE,
) -> RunResult:
    # Heartbeat around each phase — liveness while the role does the work.
    heartbeat(conn, task.id, worker_id)
    exec_result = run_exec(
        conn, task, sink, registry=registry, config=config, model_registry=model_registry
    )

    # 🔴 human-in-the-loop: the Executor's tool call PENDed on an approval. Park the
    # task `blocked` on that approval and STOP — do NOT verify or complete. invoke
    # already emitted approval.requested; resume_approved re-queues it once granted.
    if exec_result.invoke_status == InvokeStatus.PENDING.value:
        block(conn, task.id, approval_id=exec_result.approval_id,
              reason="awaiting 🔴 approval")
        return RunResult(
            task_id=str(task.id), task_type=task.type, kind="work",
            outcome="blocked",
            detail=f"blocked on approval {exec_result.approval_id}",
        )

    heartbeat(conn, task.id, worker_id)
    verdict = run_verify(
        conn, task, exec_result, sink,
        registry=registry, config=config, model_registry=model_registry,
    )
    heartbeat(conn, task.id, worker_id)

    if verdict.passed:
        # verify → commit: only now is the task done.
        complete(
            conn, task.id,
            result={"verified": True, "reason": verdict.reason,
                    "artifact_path": exec_result.artifact_path},
            status=TaskStatus.DONE,
        )
        # Terminal (done): fire a retro per policy (default on_fail → skip on done).
        _maybe_enqueue_retro(
            conn, task, sink, enqueue=enqueue,
            outcome="done", detail=verdict.reason, retro_mode=retro_mode,
        )
        return RunResult(
            task_id=str(task.id), task_type=task.type, kind="work",
            outcome="done", detail=verdict.reason,
        )

    # Verify failed — bounded re-enqueue (nudge) or fail.
    attempt = int((task.payload or {}).get("attempt", 1))
    complete(
        conn, task.id,
        result={"verified": False, "reason": verdict.reason, "attempt": attempt},
        status=TaskStatus.FAILED,
    )
    if attempt < max_attempts:
        retry = enqueue(
            conn,
            workstream=task.workstream,
            type=task.type,
            payload={**(task.payload or {}), "attempt": attempt + 1, "nudge": verdict.reason},
            priority=task.priority,
        )
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_WORK_RETRY,
                task_id=task.id,
                payload={"attempt": attempt, "retry_task_id": str(retry.id),
                         "reason": verdict.reason},
            )
        )
        return RunResult(
            task_id=str(task.id), task_type=task.type, kind="work",
            outcome="retry", detail=f"re-enqueued as {retry.id} (attempt {attempt + 1})",
        )
    # Terminal (failed after exhausting retries): fire a retro per policy so the
    # studio learns from the failure (on_fail + always both trigger here).
    detail = f"verify failed after {attempt} attempt(s): {verdict.reason}"
    _maybe_enqueue_retro(
        conn, task, sink, enqueue=enqueue,
        outcome="failed", detail=detail, retro_mode=retro_mode,
    )
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="work",
        outcome="failed", detail=detail,
    )


def _handle_retro(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    model_registry,
    heartbeat: Heartbeater,
    complete: Completer,
    worker_id: str,
    run_retro: Callable[..., Any],
) -> RunResult:
    """Dispatch a ``retro`` task: distill + store lessons, then commit.

    A retro NEVER enqueues another task (no ``enqueue`` seam is threaded here), so
    the learning loop cannot recurse into a retro-of-a-retro.
    """
    heartbeat(conn, task.id, worker_id)
    result = run_retro(conn, task, sink, model_registry=model_registry)
    heartbeat(conn, task.id, worker_id)
    complete(conn, task.id, result=result.model_dump(), status=TaskStatus.DONE)
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="retro",
        outcome="done", detail=f"distilled {result.lessons_count} lesson(s)",
    )


def run_once(
    conn: Any,
    worker_id: str,
    sink: Optional[EventSink] = None,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig] = None,
    model_registry=None,
    skills: Optional[SkillRegistry] = None,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_WORK_ATTEMPTS,
    retro_mode: Optional[str] = None,
    claim: Claimer = claim_task,
    heartbeat: Heartbeater = heartbeat,
    complete: Completer = complete_task,
    enqueue: Enqueuer = enqueue_task,
    block: Blocker = block_task,
    run_pm: Callable[..., Any] = run_pm_tick,
    run_exec: Callable[..., Any] = run_executor,
    run_verify: Callable[..., Any] = run_verify_default,
    run_retro: Callable[..., Any] = run_retro_default,
) -> Optional[RunResult]:
    """Claim one task and drive it to a terminal state (the single testable unit).

    Returns ``None`` when nothing is claimable (the caller sleeps), else a
    :class:`RunResult`. Dispatch is by ``task.type``: ``pm.tick`` plans + enqueues
    work; ``work.*`` runs Executor + Verifier and commits/fails per the verdict.
    Every seam (claim/heartbeat/complete/enqueue + the three role handlers) is
    injectable so the whole loop runs with no database in tests.
    """
    sink = sink or NullEventSink()
    resolved_retro_mode = _resolve_retro_mode(retro_mode)
    task = claim(conn, worker_id=worker_id, assignee=assignee, workstream=workstream)
    if task is None:
        return None

    if task.type == PM_TICK_TYPE:
        return _handle_pm_tick(
            conn, task, sink,
            model_registry=model_registry, enqueue=enqueue,
            heartbeat=heartbeat, complete=complete, worker_id=worker_id, run_pm=run_pm,
            skills=skills,
        )

    if task.type == RETRO_TASK_TYPE:
        # A retro distills lessons from a finished episode. It never enqueues
        # another task, so pm.tick / retro never trigger a retro (no loop).
        return _handle_retro(
            conn, task, sink,
            model_registry=model_registry,
            heartbeat=heartbeat, complete=complete, worker_id=worker_id,
            run_retro=run_retro,
        )

    if task.type.startswith("work."):
        return _handle_work(
            conn, task, sink,
            registry=registry, config=config, model_registry=model_registry,
            heartbeat=heartbeat, complete=complete, enqueue=enqueue, block=block,
            worker_id=worker_id, max_attempts=max_attempts,
            run_exec=run_exec, run_verify=run_verify,
            retro_mode=resolved_retro_mode,
        )

    # Unknown task type — fail it explicitly rather than silently dropping it.
    complete(
        conn, task.id,
        result={"error": f"no handler for task type {task.type!r}"},
        status=TaskStatus.FAILED,
    )
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="unknown",
        outcome="failed", detail="no handler for task type",
    )


class ResumeResult(BaseModel):
    """What one :func:`resume_approved` pass did — task ids advanced, for logging."""

    #: Tasks re-queued because their approval was granted (will retry + execute).
    resumed: list[str] = []
    #: Tasks failed because their approval was denied (the 🔴 action was refused).
    failed: list[str] = []


def resume_approved(
    conn: Any,
    sink: Optional[EventSink] = None,
    *,
    complete: Completer = complete_task,
    requeue: Blocker = requeue_blocked_task,
) -> ResumeResult:
    """Advance tasks parked ``blocked`` on an approval a human has now resolved.

    One bounded pass over the currently-blocked tasks (no loops):

    - **approved** (a live grant) → re-queue the task so a worker retries; on that
      retry :func:`runtime.enforce.invoke` finds the grant and executes (then
      consumes it). Emits ``approval.resumed``.
    - **denied** → fail the task (``complete_task`` ``failed``, force): the 🔴
      action was refused, so the work cannot proceed.
    - **still pending / missing** → left untouched for a later pass.

    Call it from the worker/supervisor loop (like the stale-task sweep). It never
    executes a tool itself — it only moves task state; the actual 🔴 execution
    happens on the worker's normal retry through the enforced ``invoke`` path.
    """
    sink = sink or NullEventSink()
    result = ResumeResult()
    for task in find_blocked_tasks(conn):
        approval_id = (task.result or {}).get("blocked_on_approval")
        if not approval_id:
            continue
        approval = get_approval(conn, approval_id)
        if approval is None:
            continue

        if approval.status == STATUS_APPROVED:
            if requeue(conn, task.id) is not None:
                sink.emit(
                    make_event(
                        workstream=task.workstream,
                        type=EVENT_APPROVAL_RESUMED,
                        task_id=task.id,
                        payload={"approval_id": approval_id, "status": "queued"},
                    )
                )
                result.resumed.append(str(task.id))
        elif approval.status == STATUS_DENIED:
            failed = complete(
                conn, task.id,
                result={"approved": False, "reason": approval.reason,
                        "blocked_on_approval": approval_id},
                status=TaskStatus.FAILED, force=True,
            )
            if failed is not None:
                result.failed.append(str(task.id))
    return result


# --- Config + loop ----------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def default_scratch_dir() -> str:
    return os.environ.get(
        "WORKER_SCRATCH_DIR",
        os.path.join(tempfile.gettempdir(), "ai_studio_scratch"),
    )


def run(
    worker_id: Optional[str] = None,
    *,
    idle_sleep_s: Optional[float] = None,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
) -> None:
    """Loop: claim + service tasks; sleep ``idle_sleep_s`` only when the queue is
    empty. Reconnects on a dropped connection and never lets one bad task kill the
    driver. Runs keyless — ``call_model`` falls back to dry-run with no keys."""
    worker_id = worker_id or os.environ.get("WORKER_ID") or f"worker-{uuid4().hex[:8]}"
    idle_sleep_s = (
        _env_float("WORKER_IDLE_SLEEP_S", DEFAULT_IDLE_SLEEP_S)
        if idle_sleep_s is None
        else idle_sleep_s
    )
    max_attempts = _env_int("WORKER_MAX_WORK_ATTEMPTS", DEFAULT_MAX_WORK_ATTEMPTS)
    registry = build_registry(default_scratch_dir())
    config = load_policy()
    # Discover skills once at startup; the PM composes relevant, reviewed ones
    # into its plan prompt on demand (ADR-0008). Empty registry if none exist.
    skills = SkillRegistry.discover()
    log.info("worker %s: %d skill(s) available", worker_id, len(skills))

    log.info("worker %s starting (scratch=%s)", worker_id, default_scratch_dir())
    conn: Optional[psycopg.Connection] = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            # Advance any task whose 🔴 approval a human has resolved since last pass
            # (grant → re-queue for retry+execute; deny → fail). Bounded, one pass.
            resume_approved(conn, DbEventSink(conn))
            result = run_once(
                conn, worker_id, DbEventSink(conn),
                registry=registry, config=config, skills=skills,
                assignee=assignee, workstream=workstream, max_attempts=max_attempts,
            )
            if result is None:
                time.sleep(idle_sleep_s)
            else:
                log.info(
                    "worker %s: %s %s -> %s (%s)",
                    worker_id, result.kind, result.task_id, result.outcome, result.detail,
                )
        except Exception:
            log.exception("worker pass failed; will retry after idle sleep")
            conn = _safe_close(conn)
            time.sleep(idle_sleep_s)


def _safe_close(conn: Optional[psycopg.Connection]) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return None


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
