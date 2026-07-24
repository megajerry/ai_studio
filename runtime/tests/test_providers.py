"""Provider abstraction: dry-run stubs, synthetic usage, and env-gated availability.

No network is ever attempted. httpx.post is patched to explode so any accidental
real call fails loudly (belt-and-suspenders for the "no HTTP in tests" rule).
"""

from __future__ import annotations

import httpx
import pytest

from runtime.model.providers import (
    AnthropicProvider,
    CursorCliProvider,
    DryRunProvider,
    GoogleProvider,
    OpenAIProvider,
    ProviderFallback,
    get_adapter,
)
from runtime.model.providers import cursor_cli as cursor_mod


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - only fires on a real call
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def test_dryrun_is_always_available_and_deterministic():
    p = DryRunProvider()
    assert p.available() is True
    msgs = [{"role": "user", "content": "x" * 400}]
    a = p.complete("claude-opus-4.8", msgs)
    b = p.complete("claude-opus-4.8", msgs)
    assert a.text == b.text  # deterministic
    assert a.provider == "dryrun"


def test_dryrun_synthetic_tokens_scale_with_input():
    p = DryRunProvider()
    small = p.complete("m", [{"role": "user", "content": "hi"}])
    big = p.complete("m", [{"role": "user", "content": "y" * 4000}])
    assert big.usage.input_tokens > small.usage.input_tokens
    # ~4 chars/token: 4000 chars -> ~1000 input tokens.
    assert big.usage.input_tokens == 1000
    assert big.usage.output_tokens >= 1
    assert small.usage.input_tokens >= 1  # never zero


@pytest.mark.parametrize(
    "cls,env",
    [
        (AnthropicProvider, "ANTHROPIC_API_KEY"),
        (OpenAIProvider, "OPENAI_API_KEY"),
        (GoogleProvider, "GOOGLE_API_KEY"),
    ],
)
def test_available_reflects_env_key(cls, env, monkeypatch):
    monkeypatch.delenv(env, raising=False)
    assert cls().available() is False
    monkeypatch.setenv(env, "sk-test-not-real")
    assert cls().available() is True


def test_adapter_registry_maps_known_providers_only():
    assert isinstance(get_adapter("anthropic"), AnthropicProvider)
    assert isinstance(get_adapter("google"), GoogleProvider)
    assert isinstance(get_adapter("openai"), OpenAIProvider)
    assert isinstance(get_adapter("cursor-cli"), CursorCliProvider)
    # An unwired provider (e.g. the budget open-weight entry) has no adapter.
    assert get_adapter("openweight") is None


# --------------------------------------------------------------------------- #
# Cursor CLI adapter — agent-harness subprocess, guarded, keyless-safe
# --------------------------------------------------------------------------- #

def test_cursor_available_reflects_env_key(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert CursorCliProvider().available() is False
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    assert CursorCliProvider().available() is True


def test_cursor_dryrun_stub_without_key_never_shells_out(monkeypatch):
    # No CURSOR_API_KEY -> deterministic stub, NO subprocess (keyless-green).
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)

    def boom(*a, **k):  # pragma: no cover - only fires on an accidental shell-out
        raise AssertionError("cursor-agent was launched in a test")

    monkeypatch.setattr(cursor_mod.subprocess, "run", boom)
    p = CursorCliProvider()
    msgs = [{"role": "user", "content": "x" * 400}]
    a = p.complete("cursor-composer", msgs)
    b = p.complete("cursor-composer", msgs)
    assert a.text == b.text  # deterministic, exactly like DryRunProvider
    assert a.provider == "cursor-cli"
    assert a.usage.input_tokens >= 1


def test_cursor_dryrun_mode_never_shells_out_even_with_key(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.setattr(
        cursor_mod.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("shelled out in dry-run")),
    )
    comp = CursorCliProvider().complete("cursor-composer", [{"role": "user", "content": "hi"}])
    assert comp.provider == "cursor-cli"


def test_cursor_timeout_raises_provider_fallback(monkeypatch):
    import subprocess as _sp

    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)

    def hang(*a, **k):
        raise _sp.TimeoutExpired(cmd="cursor-agent", timeout=1.0)

    monkeypatch.setattr(cursor_mod.subprocess, "run", hang)
    with pytest.raises(ProviderFallback):
        CursorCliProvider(timeout_s=1.0).complete("cursor-composer", [{"role": "user", "content": "go"}])


def test_cursor_nonzero_exit_raises_provider_fallback(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)

    class _Proc:
        returncode = 2
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(cursor_mod.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(ProviderFallback):
        CursorCliProvider().complete("cursor-composer", [{"role": "user", "content": "go"}])


def test_cursor_unparseable_output_raises_provider_fallback(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)

    class _Proc:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    monkeypatch.setattr(cursor_mod.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(ProviderFallback):
        CursorCliProvider().complete("cursor-composer", [{"role": "user", "content": "go"}])


def test_cursor_parses_result_field_on_success(monkeypatch):
    import json as _json

    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    captured = {}

    class _Proc:
        returncode = 0
        stdout = _json.dumps({"result": "the assistant reply"})
        stderr = ""

    def _run(argv, **k):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(cursor_mod.subprocess, "run", _run)
    comp = CursorCliProvider().complete("cursor-composer", [{"role": "user", "content": "go"}])
    assert comp.text == "the assistant reply"
    assert comp.provider == "cursor-cli"
    # The agent-harness CLI shape: -p <prompt> --output-format json. The API key
    # is NEVER on the argv (forwarded via the child's environment, by name).
    assert "-p" in captured["argv"] and "--output-format" in captured["argv"]
    assert "json" in captured["argv"]
    assert "cur-not-real" not in " ".join(captured["argv"])


def test_adapter_complete_refuses_without_key(monkeypatch):
    # Structural adapters raise (never touch httpx) when their key is absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicProvider().complete("claude-opus-4.8", [{"role": "user", "content": "hi"}])
