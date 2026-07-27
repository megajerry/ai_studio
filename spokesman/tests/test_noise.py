"""Unit tests for stakeholder test/demo noise filtering."""

from __future__ import annotations

from spokesman.noise import is_noise_task_type, is_noise_workstream


def test_real_workstream_kept() -> None:
    assert not is_noise_workstream("productivity")
    assert not is_noise_workstream("growth")


def test_fixture_workstreams_filtered() -> None:
    assert is_noise_workstream("test")
    assert is_noise_workstream("test-spk-abc123")
    assert is_noise_workstream("skilllc-c9388667dd27")
    assert is_noise_workstream("gw-1535963393cf-other")
    assert is_noise_workstream("pm-research-deadbeefcafe")
    assert is_noise_workstream("curate-a2bcd82b")
    assert is_noise_workstream("")  # empty is not a real vertical


def test_demo_task_types_filtered() -> None:
    assert is_noise_task_type("work.demo")
    assert is_noise_task_type("work.probe")
    assert not is_noise_task_type("pm.tick")
    assert not is_noise_task_type("work.task")
