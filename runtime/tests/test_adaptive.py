"""Adaptive orchestration intensity — pure decision logic + live-DB telemetry (ADR-0003).

Two layers, both fully evidence-based:

- **Pure** (no DB): the deterministic decision core (:func:`_scale` /
  :func:`_scale_research`), the budget-fraction normalizer, env-config parsing,
  and the behavior-preserving OFF passthrough (proven by passing ``conn=None`` —
  a disabled helper must never touch the DB). Also proves outputs are BOUNDED to
  the legal mode set and that the budget throttle beats the error escalation.
- **Live DB** (SKIPs cleanly with no reachable Postgres): the error-rate + activity
  readers compute from SEEDED telemetry (real ``tasks`` rows + ``events``:
  ``verify.failed`` / ``review.flagged`` / abandonment / ``task.finished``), and
  the public ``review_mode`` / ``retro_mode`` escalate on a high recent error rate
  and relax on clean + tight budget — end to end. A hermetic worker-wiring test
  proves ``run_once`` applies the resolved modes (escalated → MORE review/retro).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from runtime import adaptive, db
from runtime.adaptive import (
    AdaptiveConfig,
    IntensityDecision,
    RESEARCH_EAGER,
    RESEARCH_NORMAL,
    RESEARCH_OFF,
    _scale,
    _scale_research,
    budget_fraction,
    recent_activity,
    recent_error_rate,
    research_cadence,
    resolve_modes,
    retro_mode,
    review_mode,
)
from runtime.enforce import MemoryEventSink
from runtime.models import Task, TaskStatus, make_event
from runtime.roles.verifier import VerifyResult
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry
from runtime.worker import EVENT_RETRO_TRIGGERED, EVENT_REVIEW_TRIGGERED, run_once


# ============================================================================
# Pure — deterministic decision core (no DB)
# ============================================================================

_ENABLED = AdaptiveConfig(enabled=True)
_REVIEW_KW = dict(escalate_mode="always", guard_mode="on_risk", off_mode="off")
_RETRO_KW = dict(escalate_mode="always", guard_mode="on_fail", off_mode="off")


def test_scale_escalates_toward_always_on_high_error_rate():
    # High error rate, ample budget → escalate to `always` (more review/retro).
    assert _scale("on_risk", 0.9, None, _ENABLED, **_REVIEW_KW) == "always"
    assert _scale("on_fail", 0.9, None, _ENABLED, **_RETRO_KW) == "always"
    # At the threshold exactly (>=) escalates too.
    assert _scale("on_risk", _ENABLED.high_error_rate, None, _ENABLED, **_REVIEW_KW) == "always"


def test_scale_relaxes_toward_off_when_clean_and_budget_tight():
    # Clean (rate <= low) AND budget tight → relax to `off`.
    assert _scale("on_risk", 0.0, 0.10, _ENABLED, **_REVIEW_KW) == "off"
    assert _scale("on_fail", 0.05, 0.12, _ENABLED, **_RETRO_KW) == "off"
    # Clean but budget ample → stay at base (don't relax for no reason).
    assert _scale("on_risk", 0.0, None, _ENABLED, **_REVIEW_KW) == "on_risk"
    assert _scale("on_risk", 0.0, 0.9, _ENABLED, **_REVIEW_KW) == "on_risk"


def test_budget_throttle_beats_escalation_when_nearly_exhausted():
    # High error rate but budget CRITICAL → throttle to `off` (never pile on).
    assert _scale("on_risk", 1.0, 0.02, _ENABLED, **_REVIEW_KW) == "off"
    assert _scale("on_fail", 1.0, 0.0, _ENABLED, **_RETRO_KW) == "off"
    # High error rate + budget TIGHT (not critical) → guard mode, not `always`
    # (still catch risky episodes, but don't escalate to reviewing everything).
    assert _scale("on_risk", 1.0, 0.10, _ENABLED, **_REVIEW_KW) == "on_risk"
    assert _scale("on_fail", 1.0, 0.10, _ENABLED, **_RETRO_KW) == "on_fail"


def test_scale_midrange_error_returns_base_unchanged():
    # Between low and high thresholds, ample budget → leave the base policy alone.
    assert _scale("on_risk", 0.3, None, _ENABLED, **_REVIEW_KW) == "on_risk"
    assert _scale("always", 0.3, None, _ENABLED, **_REVIEW_KW) == "always"
    assert _scale("off", 0.3, None, _ENABLED, **_REVIEW_KW) == "off"


def test_scale_output_is_bounded_to_legal_modes():
    legal = {"always", "on_risk", "off"}
    for rate in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
        for frac in (None, 0.0, 0.03, 0.1, 0.5, 1.0):
            out = _scale("on_risk", rate, frac, _ENABLED, **_REVIEW_KW)
            assert out in legal, (rate, frac, out)


def test_research_cadence_pure_rules():
    # Fast-moving (activity high) → eager; erroring → eager.
    assert _scale_research("normal", 0.0, 20, None, _ENABLED) == RESEARCH_EAGER
    assert _scale_research("normal", 0.9, 0, None, _ENABLED) == RESEARCH_EAGER
    # Budget critical throttles hard → off.
    assert _scale_research("normal", 0.9, 50, 0.01, _ENABLED) == RESEARCH_OFF
    # Fast-moving but budget tight → normal (throttled down from eager).
    assert _scale_research("normal", 0.9, 50, 0.1, _ENABLED) == RESEARCH_NORMAL
    # Calm + tight → off; calm + ample → base.
    assert _scale_research("normal", 0.0, 0, 0.1, _ENABLED) == RESEARCH_OFF
    assert _scale_research("normal", 0.0, 0, None, _ENABLED) == RESEARCH_NORMAL


def test_budget_fraction_normalizes_all_inputs():
    assert budget_fraction(None) is None
    assert budget_fraction(True) is None  # bool is NOT a budget number
    assert budget_fraction(0.42) == pytest.approx(0.42)
    assert budget_fraction(-1.0) == 0.0 and budget_fraction(2.0) == 1.0  # clamped

    class _Status:  # duck-typed BudgetStatus
        def __init__(self, cap_usd, rem_usd, cap_tokens, rem_tokens):
            self.cap_usd, self.remaining_usd = cap_usd, rem_usd
            self.cap_tokens, self.remaining_tokens = cap_tokens, rem_tokens

    # Tightest resource governs: usd 0.5 vs tokens 0.2 → 0.2.
    assert budget_fraction(_Status(10.0, 5.0, 100, 20)) == pytest.approx(0.2)
    # No configured cap → None (uncapped).
    assert budget_fraction(_Status(None, None, None, None)) is None
    # Only one cap configured → that one.
    assert budget_fraction(_Status(10.0, 1.0, None, None)) == pytest.approx(0.1)


def test_config_from_env_parses_flags_and_thresholds():
    off = AdaptiveConfig.from_env({})
    assert off.enabled is False  # default off → behavior-preserving
    on = AdaptiveConfig.from_env({
        "ADAPTIVE_INTENSITY": "on",
        "ADAPTIVE_ERROR_WINDOW": "5",
        "ADAPTIVE_HIGH_ERROR_RATE": "0.7",
        "ADAPTIVE_BUDGET_CRITICAL": "0.03",
    })
    assert on.enabled is True and on.error_window == 5
    assert on.high_error_rate == 0.7 and on.budget_critical == 0.03
    # Bad values fall back to defaults (never crash); window floored at 1.
    bad = AdaptiveConfig.from_env({"ADAPTIVE_INTENSITY": "1", "ADAPTIVE_ERROR_WINDOW": "-9",
                                   "ADAPTIVE_HIGH_ERROR_RATE": "notafloat"})
    assert bad.enabled is True and bad.error_window == 1
    assert bad.high_error_rate == AdaptiveConfig.high_error_rate


def test_disabled_is_behavior_preserving_and_reads_no_db():
    # conn=None proves a disabled helper NEVER touches the database.
    off = AdaptiveConfig(enabled=False)
    assert review_mode(None, "ws", "on_risk", 0.01, config=off) == "on_risk"
    assert retro_mode(None, "ws", "on_fail", 0.01, config=off) == "on_fail"
    assert research_cadence(None, "ws", "normal", 0.01, config=off) == "normal"
    d = resolve_modes(None, "ws", base_review="on_risk", base_retro="on_fail",
                      budget_remaining=0.01, config=off)
    assert d.adaptive is False
    assert (d.review, d.retro, d.research) == ("on_risk", "on_fail", "normal")


# ============================================================================
# Live DB — telemetry readers + public policy on seeded facts
# ============================================================================

pytestmark_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    from runtime.migrate import migrate

    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _seed_episode(conn, ws, *, errored: bool, kind: str = "verify.failed",
                  ttype: str = "work.demo"):
    """Seed one WORK episode: a real task row + a task.finished event, optionally
    marked errored via the given signal (verify.failed / task.rekicked /
    abandoned / review.flagged). Returns the task id."""
    from runtime.events import append_event
    from runtime.tasks import enqueue_task

    t = enqueue_task(conn, workstream=ws, type=ttype, payload={})
    if errored:
        if kind == "abandoned":
            append_event(conn, make_event(workstream=ws, type="task.transition",
                                          task_id=t.id, payload={"to": "abandoned"}))
        elif kind == "review.flagged":
            # review.flagged is emitted on the REVIEW task but names the work
            # episode via target_task_id — attribution must follow the target.
            append_event(conn, make_event(workstream=ws, type="review.flagged",
                                          task_id=uuid4(),
                                          payload={"target_task_id": str(t.id),
                                                   "severity": "high"}))
        else:
            append_event(conn, make_event(workstream=ws, type=kind, task_id=t.id,
                                          payload={"reason": "x"}))
    append_event(conn, make_event(workstream=ws, type="task.finished", task_id=t.id,
                                  payload={"status": "failed" if errored else "done"}))
    return t.id


@pytestmark_db
def test_recent_error_rate_from_seeded_telemetry(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    # 3 of 4 work episodes went wrong → 0.75.
    _seed_episode(conn, ws, errored=True)
    _seed_episode(conn, ws, errored=True)
    _seed_episode(conn, ws, errored=True)
    _seed_episode(conn, ws, errored=False)
    assert recent_error_rate(conn, ws, config=_ENABLED) == pytest.approx(0.75)
    assert recent_activity(conn, ws, config=_ENABLED) == 4


@pytestmark_db
def test_recent_error_rate_clean_workstream_is_zero(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    for _ in range(4):
        _seed_episode(conn, ws, errored=False)
    assert recent_error_rate(conn, ws, config=_ENABLED) == 0.0
    # No recent work at all → 0.0 (no evidence → not risky), not a crash.
    empty = f"adapt-empty-{uuid4().hex[:8]}"
    assert recent_error_rate(conn, empty, config=_ENABLED) == 0.0


@pytestmark_db
def test_error_attribution_across_signal_types(conn):
    # abandonment and review.flagged (target) both count as errored episodes.
    ws = f"adapt-{uuid4().hex[:12]}"
    _seed_episode(conn, ws, errored=True, kind="abandoned")
    _seed_episode(conn, ws, errored=True, kind="review.flagged")
    _seed_episode(conn, ws, errored=True, kind="task.rekicked")
    _seed_episode(conn, ws, errored=False)
    assert recent_error_rate(conn, ws, config=_ENABLED) == pytest.approx(0.75)


@pytestmark_db
def test_window_bounds_which_episodes_count(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    # Oldest episode errored; then 3 clean. A window of 3 excludes the old error.
    _seed_episode(conn, ws, errored=True)
    for _ in range(3):
        _seed_episode(conn, ws, errored=False)
    assert recent_error_rate(conn, ws, window=3, config=_ENABLED) == 0.0
    assert recent_error_rate(conn, ws, window=4, config=_ENABLED) == pytest.approx(0.25)


@pytestmark_db
def test_review_and_retro_escalate_on_high_error_rate_live(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    for _ in range(4):
        _seed_episode(conn, ws, errored=True)
    # High error rate, ample budget → escalate both toward `always`.
    assert review_mode(conn, ws, "on_risk", None, config=_ENABLED) == "always"
    assert retro_mode(conn, ws, "on_fail", None, config=_ENABLED) == "always"


@pytestmark_db
def test_clean_and_tight_budget_relaxes_live(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    for _ in range(4):
        _seed_episode(conn, ws, errored=False)
    # Clean + budget tight (10% remaining) → relax both toward `off`.
    assert review_mode(conn, ws, "on_risk", 0.10, config=_ENABLED) == "off"
    assert retro_mode(conn, ws, "on_fail", 0.10, config=_ENABLED) == "off"
    # Clean + ample budget → stay at the base policy.
    assert review_mode(conn, ws, "on_risk", None, config=_ENABLED) == "on_risk"


@pytestmark_db
def test_budget_throttle_prevents_piling_on_when_exhausted_live(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    for _ in range(4):
        _seed_episode(conn, ws, errored=True)
    # Even with a HIGH error rate, a near-exhausted budget throttles to `off`.
    assert review_mode(conn, ws, "on_risk", 0.02, config=_ENABLED) == "off"
    assert retro_mode(conn, ws, "on_fail", 0.02, config=_ENABLED) == "off"
    # Tight (not critical) → guard mode, not the full `always` pile-on.
    assert review_mode(conn, ws, "on_risk", 0.10, config=_ENABLED) == "on_risk"


@pytestmark_db
def test_resolve_modes_end_to_end_live(conn):
    ws = f"adapt-{uuid4().hex[:12]}"
    for _ in range(4):
        _seed_episode(conn, ws, errored=True)
    d = resolve_modes(conn, ws, base_review="on_risk", base_retro="on_fail",
                      budget_remaining=None, config=_ENABLED)
    assert d.adaptive is True and d.error_rate == pytest.approx(1.0)
    assert (d.review, d.retro) == ("always", "always")


# ============================================================================
# Worker wiring (hermetic) — run_once applies the resolved intensity
# ============================================================================


class _FakeQueue:
    """Minimal in-memory queue for the worker loop (no DB)."""

    def __init__(self, sink: MemoryEventSink) -> None:
        self.sink = sink
        self.tasks: dict = {}
        self.order: list = []

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None, depends_on=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type,
                 status=TaskStatus.UP_FOR_GRABS, priority=priority, payload=payload or {},
                 created_at=now, updated_at=now)
        self.tasks[t.id] = t
        self.order.append(t.id)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None):
        ids = [i for i in self.order if self.tasks[i].status is TaskStatus.UP_FOR_GRABS
               and (workstream is None or self.tasks[i].workstream == workstream)]
        if not ids:
            return None
        t = self.tasks[ids[0]].model_copy(update={"status": TaskStatus.IN_PROGRESS,
                                                  "claimed_by": worker_id})
        self.tasks[t.id] = t
        return t

    def heartbeat(self, conn, task_id, worker_id):
        return self.tasks.get(task_id)

    def transition(self, conn, task_id, to, **kw):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        upd = {"status": to}
        if kw.get("result") is not None:
            upd["result"] = kw["result"]
        t = t.model_copy(update=upd)
        self.tasks[task_id] = t
        return t

    def queued_of_type(self, type_: str) -> list:
        return [t for t in self.tasks.values() if t.type == type_
                and t.status is TaskStatus.UP_FOR_GRABS]


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _seams(q: _FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, transition=q.transition,
                enqueue=q.enqueue)


def _pass(conn, task, result, s, **kw):
    return VerifyResult(passed=True, reason="ok")


def test_worker_applies_escalated_modes_from_resolver(tmp_path):
    """A resolver returning escalated modes makes a CLEAN, passing episode still
    trigger BOTH a review and a retro (more oversight) — proving run_once uses the
    resolved (not the static base) modes."""
    sink = MemoryEventSink()
    q = _FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m"})

    def escalate(conn, workstream, *, base_review, base_retro):
        return IntensityDecision(review="always", retro="always", research="eager",
                                 error_rate=1.0, budget_fraction=None, activity=9,
                                 adaptive=True)

    # Base modes are the quiet defaults; adaptive escalation overrides them.
    r = run_once(None, "w", sink, registry=_registry(tmp_path),
                 config=None, run_verify=_pass, review_mode="on_risk", retro_mode="on_fail",
                 resolve_intensity=escalate, **_seams(q))
    assert r.outcome == "done"
    assert EVENT_REVIEW_TRIGGERED in sink.types()  # escalated: review even on clean done
    assert EVENT_RETRO_TRIGGERED in sink.types()   # escalated: retro even on clean done


def test_worker_default_resolver_off_preserves_static_behavior(tmp_path, monkeypatch):
    """With ADAPTIVE_INTENSITY unset (default off), the DEFAULT resolver passes the
    static base modes through: a clean pass with on_risk/on_fail triggers NEITHER
    review nor retro — identical to today's behavior (and touches no DB)."""
    monkeypatch.delenv("ADAPTIVE_INTENSITY", raising=False)
    sink = MemoryEventSink()
    q = _FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m"})
    # conn=None: if the default resolver read telemetry/budget it would crash.
    r = run_once(None, "w", sink, registry=_registry(tmp_path), config=None,
                 run_verify=_pass, review_mode="on_risk", retro_mode="on_fail", **_seams(q))
    assert r.outcome == "done"
    assert EVENT_REVIEW_TRIGGERED not in sink.types()
    assert EVENT_RETRO_TRIGGERED not in sink.types()
