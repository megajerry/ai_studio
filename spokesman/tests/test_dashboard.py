"""Stakeholder HTML dashboard — render + auth gate (no live DB required)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.dashboard import render_dashboard
from spokesman.runtime_bridge import DashboardSnapshot, StudioStatus

from .conftest import make_settings


def test_render_dashboard_includes_task_and_agent_stats() -> None:
    snap = DashboardSnapshot(
        status=StudioStatus(
            queued=2, in_progress=1, blocked=1, done=4, failed=0,
            pending_approvals=1, spent_tokens=1200,
        ),
        by_status={"up_for_grabs": 2, "blocked": 1, "merged": 4},
        by_agent_type={"executor": 3, "(unclaimed)": 4},
        by_assignee={"host": 5, "offhost": 2},
        by_workstream={"productivity": 7},
        recent_event_types={"task.created": 5, "model.call": 3},
        open_trajectories=1,
        closed_trajectories=2,
        pending_approval_ids=["11111111-1111-1111-1111-111111111111"],
        noise_hidden=5400,
    )
    html = render_dashboard(snap, dry_run=True)
    assert "AI Studio" in html
    assert "Tasks by agent type" in html
    assert "executor" in html
    assert "DRY-RUN" in html
    assert "11111111-1111-1111-1111-111111111111" in html
    assert "5,400 task rows hidden" in html
    assert "Test/demo noise filtered out" in html


def test_dashboard_requires_token(tmp_path) -> None:
    settings = make_settings(tmp_path, api_token="secret-token")
    client = TestClient(
        create_app(
            settings=settings,
            connect=lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        )
    )
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard?token=wrong").status_code == 401


def test_privacy_policy_is_public(tmp_path) -> None:
    settings = make_settings(tmp_path, api_token="secret-token")
    client = TestClient(
        create_app(
            settings=settings,
            connect=lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        )
    )
    res = client.get("/privacy")
    assert res.status_code == 200
    assert "Privacy Policy" in res.text
    assert "Twilio" in res.text


def test_terms_are_public(tmp_path) -> None:
    settings = make_settings(tmp_path, api_token="secret-token")
    client = TestClient(
        create_app(
            settings=settings,
            connect=lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        )
    )
    res = client.get("/terms")
    assert res.status_code == 200
    assert "Terms of Service" in res.text
    assert "/privacy" in res.text
