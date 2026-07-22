"""The single instrumented call site: routing + cost + event emission (no DB/net).

Uses a MemoryEventSink to assert the emitted ``model.routed`` / ``model.call``
events and that cost is computed from registry prices. httpx.post is patched to
explode so the dry-run path is proven to attempt NO real HTTP.
"""

from __future__ import annotations

import httpx
import pytest

from runtime.enforce import MemoryEventSink
from runtime.model.call import EVENT_MODEL_CALL, call_model, select_provider
from runtime.model.registry import Usage, cost_usd, load_registry
from runtime.model.router import EVENT_MODEL_ROUTED
from runtime.model.providers import DryRunProvider


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - only fires on a real call
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    # Keyless by construction: force every adapter to look unavailable so the
    # wrapper picks dry-run even without MODELS_DRY_RUN set.
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)


MESSAGES = [{"role": "user", "content": "summarize this " * 20}]


def test_call_model_runs_keyless_and_emits_both_events():
    reg = load_registry()
    sink = MemoryEventSink()
    comp = call_model("pm", "plan", MESSAGES, quality="high", registry=reg, sink=sink)
    assert comp.provider == "dryrun"
    assert sink.types() == [EVENT_MODEL_ROUTED, EVENT_MODEL_CALL]


def test_model_call_event_has_correctly_computed_cost():
    reg = load_registry()
    sink = MemoryEventSink()
    comp = call_model("pm", "plan", MESSAGES, quality="high", registry=reg, sink=sink)

    call_ev = [e for e in sink.events if e.type == EVENT_MODEL_CALL][0]
    spec = reg.get("claude-opus-4.8")
    expected = cost_usd(spec, comp.usage)
    assert call_ev.payload["cost_usd"] == pytest.approx(expected)
    assert expected > 0
    # The event carries the ADR-0012 signal set.
    for key in (
        "model", "provider", "role", "input_tokens", "output_tokens",
        "cached_tokens", "cost_usd", "latency_ms",
    ):
        assert key in call_ev.payload
    assert call_ev.payload["model"] == "claude-opus-4.8"
    assert call_ev.payload["role"] == "pm"


def test_event_payload_never_contains_prompt_text():
    reg = load_registry()
    sink = MemoryEventSink()
    call_model("pm", "plan", MESSAGES, quality="high", registry=reg, sink=sink)
    for ev in sink.events:
        assert "summarize this" not in str(ev.payload)


def test_select_provider_prefers_dryrun_when_key_absent():
    reg = load_registry()
    # google adapter exists but no key -> dry-run.
    assert isinstance(select_provider(reg.get("gemini-3.1-pro")), DryRunProvider)
    # openweight has no adapter at all -> dry-run.
    assert isinstance(select_provider(reg.get("deepseek-v4.5")), DryRunProvider)


def test_force_dry_run_overrides_even_with_key(monkeypatch):
    reg = load_registry()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    assert isinstance(
        select_provider(reg.get("claude-opus-4.8"), force_dry_run=True), DryRunProvider
    )


def test_env_dry_run_flag_forces_dryrun(monkeypatch):
    reg = load_registry()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    assert isinstance(select_provider(reg.get("claude-opus-4.8")), DryRunProvider)


def test_call_without_conn_skips_db_accounting():
    # task_id given but conn None -> no DB touch, no error (keyless/DB-less).
    from uuid import uuid4

    reg = load_registry()
    sink = MemoryEventSink()
    comp = call_model(
        "exec", "execute", MESSAGES, registry=reg, sink=sink, task_id=uuid4()
    )
    assert comp.usage.total_tokens > 0
    call_ev = [e for e in sink.events if e.type == EVENT_MODEL_CALL][0]
    assert call_ev.task_id is not None  # event still correlates to the task
