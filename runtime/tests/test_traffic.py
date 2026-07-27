"""Unit tests for prod/test traffic tagging (ADR-0030)."""

from __future__ import annotations

import os

import pytest

from runtime.traffic import (
    TRAFFIC_PROD,
    TRAFFIC_TEST,
    default_traffic,
    is_test_payload,
    tag_payload,
)


def test_tag_payload_defaults_to_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_STUDIO_TRAFFIC", raising=False)
    monkeypatch.delenv("AI_STUDIO_TEST_DB", raising=False)
    assert tag_payload({"goal": "ship it"}) == {
        "goal": "ship it",
        "traffic": TRAFFIC_PROD,
    }


def test_tag_payload_honors_explicit_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_STUDIO_TRAFFIC", "test")
    assert default_traffic() == TRAFFIC_TEST
    assert tag_payload({})["traffic"] == TRAFFIC_TEST
    # Explicit wins over env.
    assert tag_payload({"traffic": "prod"})["traffic"] == TRAFFIC_PROD
    assert is_test_payload({"traffic": "test"}) is True
    assert is_test_payload({"traffic": "prod"}) is False
    assert is_test_payload({"goal": "test debris phrase"}) is False


def test_test_db_env_implies_test_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_STUDIO_TRAFFIC", raising=False)
    monkeypatch.setenv("AI_STUDIO_TEST_DB", "1")
    assert default_traffic() == TRAFFIC_TEST
