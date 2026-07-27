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


def test_hex_lookalike_words_are_not_noise() -> None:
    # Plain hex-only English words are REAL workstreams, not disposable ids: the old
    # ``(^|[_-])[0-9a-f]{6,}$`` matched them whole-name and silently dropped them from
    # status/dashboard/approvals. The hex must now be a delimiter-separated suffix
    # segment (never the whole name) of >= 8 chars.
    for name in ("facade", "decade", "deface", "abcdef", "accede", "efface", "beaded"):
        assert not is_noise_workstream(name), name


def test_generated_hex_suffix_workstreams_are_noise() -> None:
    # Genuinely generated throwaway names still classify as noise.
    assert is_noise_workstream("foo-a1b2c3d4")       # delimiter + 8-hex suffix
    assert is_noise_workstream("realws-0123abcd-other")
    assert is_noise_workstream("spk-deadbeef12")      # known generated prefix
    assert is_noise_workstream("skilllc-c9388667dd27")


def test_real_name_with_short_hexish_tail_is_kept() -> None:
    # A real workstream with a non-hex tail (or a short one) is not dropped.
    assert not is_noise_workstream("realws-a1b2c3d4-live")
    assert not is_noise_workstream("growth")
