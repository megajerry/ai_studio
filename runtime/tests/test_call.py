"""The single instrumented call site: routing + cost + event emission (no DB/net).

Uses a MemoryEventSink to assert the emitted ``model.routed`` / ``model.call``
events and that cost is computed from registry prices. httpx.post is patched to
explode so the dry-run path is proven to attempt NO real HTTP.
"""

from __future__ import annotations

import httpx
import pytest

from runtime.enforce import MemoryEventSink
import runtime.model.call as call_mod
from runtime.model.call import EVENT_MODEL_CALL, call_model, select_provider
from runtime.model.registry import Usage, cost_usd, load_registry
from runtime.model.router import EVENT_MODEL_ROUTED
from runtime.model.providers import DryRunProvider, ProviderFallback


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


def test_coding_task_routes_to_cursor_dryrun_stub(monkeypatch):
    # Keyless: an agentic coding task routes to the Cursor substrate and returns
    # the deterministic dry-run stub (no CURSOR_API_KEY, no shell-out).
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    reg = load_registry()
    sink = MemoryEventSink()
    comp = call_model("builder", "agentic", MESSAGES, quality="high", registry=reg, sink=sink)
    routed = [e for e in sink.events if e.type == EVENT_MODEL_ROUTED][0]
    assert routed.payload["model"] == "cursor-composer"
    assert routed.payload["provider"] == "cursor-cli"
    # Keyless, the wrapper serves the routed cursor model via the dry-run path
    # (like every provider) — a deterministic stub, no shell-out, suite stays green.
    assert comp.model_id == "cursor-composer"
    assert comp.provider == "dryrun"
    assert comp.text.startswith("[dry-run:cursor-composer]")


def test_provider_fallback_falls_back_to_metered_model(monkeypatch):
    # Simulate the Cursor CLI hanging (its adapter raises ProviderFallback). The
    # call wrapper must walk the coding chain to the metered fallback (Opus) and
    # still return a completion + emit a model.call event for the model that
    # actually served the call. No cursor-agent is ever launched.
    reg = load_registry()
    sink = MemoryEventSink()

    class _Hanging:
        name = "cursor-cli"

        def complete(self, model_id, messages, **opts):
            raise ProviderFallback("simulated cursor-agent -p hang")

    real_select = call_mod.select_provider

    def _select(spec, *, force_dry_run=False):
        if spec.id == "cursor-composer":
            return _Hanging()
        return real_select(spec, force_dry_run=force_dry_run)

    monkeypatch.setattr(call_mod, "select_provider", _select)

    comp = call_model("builder", "agentic", MESSAGES, quality="high", registry=reg, sink=sink)
    # Fell back to the metered model and produced a real (dry-run) completion.
    assert comp.model_id == "claude-opus-4.8"
    call_ev = [e for e in sink.events if e.type == EVENT_MODEL_CALL][0]
    assert call_ev.payload["model"] == "claude-opus-4.8"  # accounted to the server
    assert call_ev.payload["provider"] == "dryrun"


def test_provider_fallback_reraises_when_no_fallback_left(monkeypatch):
    # If the failing model is last in the chain, there is nothing to fall to and
    # the error surfaces (never silently swallowed).
    reg = load_registry()

    class _Hanging:
        name = "cursor-cli"

        def complete(self, model_id, messages, **opts):
            raise ProviderFallback("hang")

    # Point the coding chain at only the (failing) cursor entry so there is no
    # metered fallback left after it.
    reg.policy.tiers["coding"] = ["cursor-composer"]
    monkeypatch.setattr(call_mod, "select_provider", lambda spec, *, force_dry_run=False: _Hanging())
    with pytest.raises(ProviderFallback):
        call_model("builder", "agentic", MESSAGES, quality="high", registry=reg)


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
