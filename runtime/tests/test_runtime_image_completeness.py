"""Containerized-deployment completeness for the runtime image (ADR-0033).

The runtime image (``runtime/Dockerfile``) backs the worker/scheduler/supervisor/
trajectory compose services. The worker dispatches ``spokesman.prep`` →
``from spokesman.converse import run_prep_task`` (and the dryrun model provider
lazy-imports the same module), so the image MUST ship the ``spokesman`` package
or every prep task raises ``ModuleNotFoundError`` and churns. It must also ship
``workstreams/`` so vertical config (ADR-0018) resolves in-container. These are
pure static/import checks — no DB, no Docker daemon — so they run anywhere.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_DOCKERFILE = _REPO_ROOT / "runtime" / "Dockerfile"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"


def _dockerfile_text() -> str:
    return _RUNTIME_DOCKERFILE.read_text(encoding="utf-8")


def test_runtime_dockerfile_copies_spokesman_package():
    """The prep handler imports `spokesman`; the image must COPY it in."""
    assert "COPY spokesman/ /app/spokesman/" in _dockerfile_text(), (
        "runtime/Dockerfile must COPY spokesman/ — the worker's spokesman.prep "
        "handler does `from spokesman.converse import run_prep_task`."
    )


def test_runtime_dockerfile_copies_workstreams():
    """Vertical config (ADR-0018) resolves from `<repo>/workstreams` in-image."""
    assert "COPY workstreams/ /app/workstreams/" in _dockerfile_text(), (
        "runtime/Dockerfile must COPY workstreams/ so runtime.workstream.config "
        "finds /app/workstreams in-container instead of falling back to defaults."
    )


def test_spokesman_prep_import_chain_resolves():
    """`spokesman.converse.run_prep_task` imports with only runtime deps.

    Simulates the runtime image: the spokesman web/SMS stack (fastapi, uvicorn,
    python-multipart, twilio, starlette) and httpx's `[cli]` extras (click,
    pygments, rich) are NOT installed there. If the prep import chain needed any
    of them, this import would fail — matching the in-container failure.
    """
    blocked = {
        "fastapi",
        "uvicorn",
        "starlette",
        "multipart",
        "python_multipart",
        "twilio",
        "click",
        "pygments",
        "rich",
    }
    # Force a fresh, guarded import (a prior test may have cached these).
    for name in list(sys.modules):
        if name.split(".")[0] in blocked or name.split(".")[0] == "spokesman":
            del sys.modules[name]

    real_import = builtins.__import__

    def _guard(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError(
                f"No module named {name.split('.')[0]!r} "
                "(simulated: absent from the runtime image)"
            )
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _guard
    try:
        import spokesman.converse as converse  # noqa: F401
        from spokesman.converse import run_prep_task

        assert callable(run_prep_task)
    finally:
        builtins.__import__ = real_import


def _load_compose() -> dict:
    # safe_load resolves YAML anchors + `<<` merge keys, so a service's
    # `environment:` reflects the merged x-runtime-env anchor.
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_compose_is_valid_yaml():
    compose = _load_compose()
    assert isinstance(compose, dict)
    assert "services" in compose


def test_x_runtime_env_carries_cursor_api_key():
    """The shared runtime env anchor must forward CURSOR_API_KEY (finding 3)."""
    compose = _load_compose()
    anchor = compose.get("x-runtime-env") or {}
    assert "CURSOR_API_KEY" in anchor, (
        "x-runtime-env must forward CURSOR_API_KEY so the coding/agentic tier is "
        "not silently dry-run-stubbed in-container."
    )
    # And it must actually reach a runtime service via the merge.
    worker_env = compose["services"]["worker"]["environment"]
    assert "CURSOR_API_KEY" in worker_env
