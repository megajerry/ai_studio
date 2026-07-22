"""Provider abstraction: dry-run stubs, synthetic usage, and env-gated availability.

No network is ever attempted. httpx.post is patched to explode so any accidental
real call fails loudly (belt-and-suspenders for the "no HTTP in tests" rule).
"""

from __future__ import annotations

import httpx
import pytest

from runtime.model.providers import (
    AnthropicProvider,
    DryRunProvider,
    GoogleProvider,
    OpenAIProvider,
    get_adapter,
)


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
    # An unwired provider (e.g. the budget open-weight entry) has no adapter.
    assert get_adapter("openweight") is None


def test_adapter_complete_refuses_without_key(monkeypatch):
    # Structural adapters raise (never touch httpx) when their key is absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicProvider().complete("claude-opus-4.8", [{"role": "user", "content": "hi"}])
