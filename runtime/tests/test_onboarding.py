"""Onboarding collects the Cursor config (grep-level, no shell execution).

We don't run the interactive script in CI; we assert the cold-start onboarding
prompts for CURSOR_API_KEY (secret) + the optional CODING_WORKER_CMD, and that
both are written out to the git-ignored .env (ADR-0011). Keeps the substrate's
config path honest without baking any secret.
"""

from __future__ import annotations

from pathlib import Path

_ONBOARDING = Path(__file__).resolve().parents[2] / "scripts" / "onboarding.sh"


def _script() -> str:
    return _ONBOARDING.read_text(encoding="utf-8")


def test_onboarding_prompts_for_cursor_key_as_secret():
    text = _script()
    # Prompted, and as a hidden `secret` (never echoed / never a plain value).
    assert "prompt_var CURSOR_API_KEY" in text
    assert "prompt_var CURSOR_API_KEY" in "\n".join(
        ln for ln in text.splitlines() if ln.strip().startswith("prompt_var CURSOR_API_KEY") and "secret" in ln
    )


def test_onboarding_prompts_for_optional_coding_worker_cmd():
    assert "prompt_var CODING_WORKER_CMD" in _script()


def test_onboarding_writes_cursor_config_to_env():
    text = _script()
    # Both keys appear in the write-out loop so a re-run preserves them.
    assert "CURSOR_API_KEY CODING_WORKER_CMD" in text
