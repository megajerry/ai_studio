"""Host-side mint writes digest into .env automatically (no paste step)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.auth import TokenSpecError
from gateway.client import (
    ENV_TOKENS,
    main,
    mint,
    read_dotenv_value,
    upsert_token_spec,
    write_dotenv_value,
)


def test_upsert_replaces_same_identity() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    first = f"offhost:read:{digest_a}"
    other = f"other:read|enqueue:{digest_a}"
    existing = f"{first} {other}"
    rotated = f"offhost:read|claim:{digest_b}"
    merged = upsert_token_spec(existing, rotated)
    parts = merged.split()
    assert parts[0] == rotated
    assert other in parts
    assert merged.count("offhost:") == 1
    assert digest_a in merged  # other identity's digest kept


def test_write_dotenv_upserts_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=1\nTASK_GATEWAY_TOKENS=\nBAR=2\n", encoding="utf-8")
    write_dotenv_value(env, ENV_TOKENS, "id:read:" + ("c" * 64))
    text = env.read_text(encoding="utf-8")
    assert "FOO=1" in text and "BAR=2" in text
    assert text.count("TASK_GATEWAY_TOKENS=") == 1
    assert read_dotenv_value(env, ENV_TOKENS).startswith("id:read:")


def test_mint_writes_digest_never_secret(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TASK_GATEWAY_TOKENS=\n", encoding="utf-8")
    out = mint(
        "offhost-cursor",
        ["read", "enqueue", "claim"],
        env_file=env,
        write_env=True,
    )
    stored = read_dotenv_value(env, ENV_TOKENS)
    assert out["spec"] == stored
    assert out["digest"] in stored
    assert out["token"] not in stored
    assert out["token"] not in env.read_text(encoding="utf-8")
    assert out["env_written"] is True

    # Re-mint same identity replaces the digest.
    out2 = mint(
        "offhost-cursor",
        ["read", "enqueue", "claim", "complete"],
        env_file=env,
        write_env=True,
    )
    stored2 = read_dotenv_value(env, ENV_TOKENS)
    assert out2["spec"] == stored2
    assert out["digest"] not in stored2
    assert stored2.count("offhost-cursor:") == 1


def test_mint_cli_writes_env(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    rc = main(
        [
            "mint",
            "--identity",
            "cli-id",
            "--scopes",
            "read,enqueue",
            "--env-file",
            str(env),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["env_written"] is True
    assert read_dotenv_value(env, ENV_TOKENS) == payload["spec"]
    assert payload["token"] not in env.read_text(encoding="utf-8")


def test_mint_no_write_env_leaves_file_alone(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TASK_GATEWAY_TOKENS=keep-me\n", encoding="utf-8")
    out = mint("x", ["read"], env_file=env, write_env=False)
    assert out["env_written"] is False
    assert read_dotenv_value(env, ENV_TOKENS) == "keep-me"


# --- Validation happens BEFORE any write (a typo must not brick startup) -----


@pytest.mark.parametrize(
    "identity,scopes,workstreams",
    [
        ("offhost", ["read", "superuser"], []),   # unknown scope
        ("offhost", [], []),                        # no scopes
        ("Bad Identity", ["read"], []),             # non-identifier identity
        ("offhost", ["read"], ["Bad WS"]),          # non-identifier workstream
        ("offhost", ["re ad"], []),                 # scope with a space
    ],
)
def test_mint_rejects_an_invalid_spec_and_writes_nothing(
    tmp_path: Path, identity, scopes, workstreams
) -> None:
    """A bad --scopes/--identity/--workstreams is refused at mint time, BEFORE the
    digest ever lands in .env — otherwise ``parse_token_spec`` would raise at
    gateway startup and refuse to boot for EVERY token."""
    env = tmp_path / ".env"
    env.write_text("TASK_GATEWAY_TOKENS=keep-me\n", encoding="utf-8")
    with pytest.raises(TokenSpecError):
        mint(identity, scopes, workstreams, env_file=env, write_env=True)
    # The file is untouched: the prior value stands, no new spec appended.
    assert read_dotenv_value(env, ENV_TOKENS) == "keep-me"


def test_mint_rejects_invalid_spec_even_when_env_file_is_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"  # does not exist yet
    with pytest.raises(TokenSpecError):
        mint("offhost", ["read", "superuser"], env_file=env, write_env=True)
    assert not env.exists()  # nothing created


def test_mint_cli_rejects_invalid_scopes_without_writing(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text("TASK_GATEWAY_TOKENS=keep-me\n", encoding="utf-8")
    rc = main(
        ["mint", "--identity", "cli-id", "--scopes", "read,superuser",
         "--env-file", str(env)]
    )
    assert rc == 2
    assert "error" in capsys.readouterr().err.lower()
    # No .env mutation on a rejected mint.
    assert read_dotenv_value(env, ENV_TOKENS) == "keep-me"


def test_mint_still_accepts_a_valid_spec(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    out = mint(
        "offhost-cursor", ["read", "enqueue", "claim"],
        ["video", "productivity"], env_file=env, write_env=True,
    )
    assert out["env_written"] is True
    assert read_dotenv_value(env, ENV_TOKENS) == out["spec"]
