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
from runtime.model.providers.anthropic import _api_model_id
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
    a = p.complete("claude-opus-4-8", msgs)
    b = p.complete("claude-opus-4-8", msgs)
    assert a.text == b.text  # deterministic
    assert a.provider == "dryrun"


def test_anthropic_api_model_id_hyphenates_dotted_versions():
    """Anthropic Messages API rejects dotted ids (404); hyphenate them."""
    assert _api_model_id("claude-opus-4.8") == "claude-opus-4-8"
    assert _api_model_id("claude-haiku-4.5") == "claude-haiku-4-5"
    assert _api_model_id("claude-sonnet-5") == "claude-sonnet-5"
    assert _api_model_id("claude-opus-4-8") == "claude-opus-4-8"


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
# Cursor CLI adapter — agent-harness run INSIDE the Docker sandbox, keyless-safe
# --------------------------------------------------------------------------- #


class _FakeSandbox:
    """A stand-in :class:`SandboxRunner` that records the command it was asked to
    run and returns a canned ``(exit_code, stdout, stderr)`` — no Docker, no host
    process. Injected into the provider so tests never launch a real container."""

    def __init__(self, *, exit_code: int = 0, stdout: str = '{"result": "ok"}', stderr: str = ""):
        self.calls: list[str] = []
        self._exit, self._stdout, self._stderr = exit_code, stdout, stderr

    def run(self, command: str, **kwargs):  # matches SandboxRunner protocol
        self.calls.append(command)
        return self._exit, self._stdout, self._stderr


def test_cursor_available_reflects_env_key(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert CursorCliProvider().available() is False
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    assert CursorCliProvider().available() is True


def test_cursor_dryrun_stub_without_key_never_runs_sandbox(monkeypatch):
    # No CURSOR_API_KEY -> deterministic stub, sandbox is never touched (keyless).
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)

    fake = _FakeSandbox()
    p = CursorCliProvider(sandbox=fake)
    msgs = [{"role": "user", "content": "x" * 400}]
    a = p.complete("cursor-composer", msgs)
    b = p.complete("cursor-composer", msgs)
    assert a.text == b.text  # deterministic, exactly like DryRunProvider
    assert a.provider == "cursor-cli"
    assert a.usage.input_tokens >= 1
    assert fake.calls == []  # keyless path never reaches the sandbox


def test_cursor_dryrun_mode_never_runs_sandbox_even_with_key(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    fake = _FakeSandbox()
    comp = CursorCliProvider(sandbox=fake).complete(
        "cursor-composer", [{"role": "user", "content": "hi"}]
    )
    assert comp.provider == "cursor-cli"
    assert fake.calls == []  # dry-run short-circuits before the sandbox


def test_cursor_never_runs_a_host_subprocess(monkeypatch):
    # SECURITY: with a key present the provider must go through the injected
    # sandbox, never a raw host subprocess. Blow up if ANY host process spawns.
    import subprocess as _sp

    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("host subprocess spawned")),
    )
    fake = _FakeSandbox(stdout='{"result": "sandboxed reply"}')
    comp = CursorCliProvider(sandbox=fake).complete(
        "cursor-composer", [{"role": "user", "content": "go"}]
    )
    assert comp.text == "sandboxed reply"
    assert len(fake.calls) == 1  # ran once, in the sandbox


def test_cursor_fail_closed_without_sandbox_raises_fallback(monkeypatch):
    # Key present but no sandbox injected AND Docker absent -> ProviderFallback,
    # NOT a host execution (fail-closed).
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    monkeypatch.setattr(cursor_mod, "_build_default_sandbox", lambda timeout_s: None)
    with pytest.raises(ProviderFallback):
        CursorCliProvider().complete("cursor-composer", [{"role": "user", "content": "go"}])


def test_cursor_sandbox_timeout_raises_provider_fallback(monkeypatch):
    from runtime.sandbox import TIMEOUT_EXIT_CODE

    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    fake = _FakeSandbox(exit_code=TIMEOUT_EXIT_CODE, stdout="", stderr="killed")
    with pytest.raises(ProviderFallback):
        CursorCliProvider(sandbox=fake, timeout_s=1.0).complete(
            "cursor-composer", [{"role": "user", "content": "go"}]
        )


def test_cursor_nonzero_exit_raises_provider_fallback(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    fake = _FakeSandbox(exit_code=2, stdout="", stderr="boom")
    with pytest.raises(ProviderFallback):
        CursorCliProvider(sandbox=fake).complete(
            "cursor-composer", [{"role": "user", "content": "go"}]
        )


def test_cursor_unparseable_output_raises_provider_fallback(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    fake = _FakeSandbox(exit_code=0, stdout="not json at all", stderr="")
    with pytest.raises(ProviderFallback):
        CursorCliProvider(sandbox=fake).complete(
            "cursor-composer", [{"role": "user", "content": "go"}]
        )


def test_cursor_parses_result_field_on_success(monkeypatch):
    import json as _json

    monkeypatch.setenv("CURSOR_API_KEY", "cur-not-real")
    monkeypatch.setenv("CURSOR_MODEL", "some-model")
    monkeypatch.delenv("MODELS_DRY_RUN", raising=False)
    fake = _FakeSandbox(stdout=_json.dumps({"result": "the assistant reply"}))
    comp = CursorCliProvider(sandbox=fake).complete(
        "cursor-composer", [{"role": "user", "content": "go"}]
    )
    assert comp.text == "the assistant reply"
    assert comp.provider == "cursor-cli"
    # The agent-harness CLI shape: -p <prompt> --output-format json. The API key
    # is NEVER in the command (it rides the sandbox env allowlist, by name).
    command = fake.calls[0]
    assert "-p" in command and "--output-format json" in command
    assert "--model some-model" in command
    assert "cur-not-real" not in command


def test_cursor_sandbox_forwards_only_allowlist_not_host_secrets(monkeypatch):
    # SECURITY (invariant 5): the sandbox the provider builds forwards ONLY
    # CURSOR_API_KEY into the container; every other host secret is withheld.
    from runtime.sandbox import DockerSandboxRunner

    host_env = {
        "OPENAI_API_KEY": "sk-HOST",
        "ANTHROPIC_API_KEY": "sk-ant-HOST",
        "DATABASE_URL": "postgresql://secret@host/db",
        "CURSOR_API_KEY": "cur-not-real",
        "PATH": "/usr/bin",
    }
    runner = DockerSandboxRunner(
        allowed_env=cursor_mod._SANDBOX_ALLOWED_ENV, env=host_env
    )
    argv, cli_env = runner.build_invocation(
        "cursor-agent -p x --output-format json", "aistudio-sbx-test"
    )
    joined = " ".join(argv)
    # The key is forwarded by NAME only (never its value on the argv/ps list).
    assert "-e" in argv and "CURSOR_API_KEY" in argv
    assert "cur-not-real" not in joined
    # Host secrets are absent from both the argv AND the docker-client env.
    for secret_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DATABASE_URL"):
        assert secret_name not in argv
        assert secret_name not in cli_env
    for secret_val in ("sk-HOST", "sk-ant-HOST"):
        assert secret_val not in joined
    # Only the allowlisted key's value reaches the docker client (for -e resolve).
    assert cli_env.get("CURSOR_API_KEY") == "cur-not-real"


def test_adapter_complete_refuses_without_key(monkeypatch):
    # Structural adapters raise (never touch httpx) when their key is absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicProvider().complete("claude-opus-4-8", [{"role": "user", "content": "hi"}])
