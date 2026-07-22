"""Pure-logic unit tests — no database required."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from runtime.models import (
    Assignee,
    EventIn,
    Task,
    TaskStatus,
    build_database_url,
    is_stale,
    make_event,
)


# --- build_database_url -----------------------------------------------------


def test_database_url_prefers_explicit_url():
    env = {"DATABASE_URL": "postgresql://u:p@db:5432/x", "POSTGRES_USER": "ignored"}
    assert build_database_url(env) == "postgresql://u:p@db:5432/x"


def test_database_url_defaults_match_compose():
    # No POSTGRES_* set → docker-compose defaults, no password segment.
    assert build_database_url({}) == "postgresql://aistudio@localhost:5432/aistudio"


def test_database_url_assembles_from_postgres_vars():
    env = {
        "POSTGRES_USER": "bob",
        "POSTGRES_PASSWORD": "s3cret",
        "POSTGRES_DB": "studio",
        "POSTGRES_HOST": "pg",
        "POSTGRES_PORT": "6000",
    }
    assert build_database_url(env) == "postgresql://bob:s3cret@pg:6000/studio"


def test_database_url_encodes_special_chars_in_password():
    env = {"POSTGRES_PASSWORD": "p@ss:w/rd", "POSTGRES_USER": "a b"}
    url = build_database_url(env)
    assert "p%40ss%3Aw%2Frd" in url
    assert "a%20b" in url


# --- make_event -------------------------------------------------------------


def test_make_event_fills_defaults():
    ev = make_event(workstream="productivity", type="task.created")
    assert isinstance(ev, EventIn)
    assert ev.payload == {}
    assert ev.task_id is None
    assert ev.trace_id is None


def test_make_event_carries_trace_context_and_payload():
    tid = uuid4()
    ev = make_event(
        workstream="ws",
        type="t",
        task_id=tid,
        payload={"k": 1},
        trace_id="tr",
        span_id="sp",
    )
    assert ev.task_id == tid
    assert ev.payload == {"k": 1}
    assert (ev.trace_id, ev.span_id) == ("tr", "sp")


@pytest.mark.parametrize("bad", [{"workstream": "", "type": "t"}, {"workstream": "w", "type": ""}])
def test_make_event_rejects_empty_required_fields(bad):
    with pytest.raises(ValueError):
        make_event(**bad)


# --- is_stale (supervisor predicate) ----------------------------------------


def _task(**kw) -> Task:
    now = datetime.now(timezone.utc)
    base = dict(
        id=uuid4(),
        workstream="ws",
        type="t",
        status=TaskStatus.IN_PROGRESS,
        priority=0,
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return Task(**base)


def test_stale_when_heartbeat_older_than_threshold():
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    t = _task(heartbeat_at=now - timedelta(seconds=120))
    assert is_stale(t, 60, now=now) is True


def test_not_stale_when_heartbeat_recent():
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    t = _task(heartbeat_at=now - timedelta(seconds=30))
    assert is_stale(t, 60, now=now) is False


def test_stale_when_heartbeat_missing():
    assert is_stale(_task(heartbeat_at=None), 60) is True


@pytest.mark.parametrize(
    "status", [TaskStatus.QUEUED, TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED]
)
def test_never_stale_unless_in_progress(status):
    t = _task(status=status, heartbeat_at=None)
    assert is_stale(t, 0) is False


def test_naive_heartbeat_treated_as_utc():
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 7, 21, 11, 58, 0)  # 120s ago, no tzinfo
    assert is_stale(_task(heartbeat_at=naive), 60, now=now) is True


# --- model enum round-trips -------------------------------------------------


def test_task_accepts_assignee_and_budget_fields():
    t = _task(assignee=Assignee.OFFHOST, budget_tokens=1000, spent_tokens=250)
    assert t.assignee is Assignee.OFFHOST
    assert (t.budget_tokens, t.spent_tokens) == (1000, 250)
