# AI Studio runtime services — worker / scheduler / supervisor / trajectory
# worker (ADR-0033). One image, four compose services differing only by their
# `python -m runtime.<svc>` command. Config/secrets come from the environment at
# runtime (ADR-0011) — nothing sensitive is baked into the image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for layer caching.
COPY runtime/requirements.txt /app/runtime/requirements.txt
RUN pip install --no-cache-dir -r /app/runtime/requirements.txt

# Application code.
COPY runtime/ /app/runtime/
# The worker dispatches `spokesman.prep` (runtime.worker._handle_spokesman_prep
# → `from spokesman.converse import run_prep_task`; the dryrun provider lazy-imports
# it too), so the `spokesman` package MUST ship in this image or the task raises
# ModuleNotFoundError and churns. Its prep import chain (converse → context →
# runtime_bridge → handoff) resolves with ONLY runtime/requirements.txt deps
# (psycopg / pydantic / PyYAML / httpx) — no fastapi/uvicorn/twilio — so no extra
# pip install is needed here (proven by tests/test_runtime_image_completeness.py).
COPY spokesman/ /app/spokesman/
# Vertical config (ADR-0018 overlays / budget / policy) is resolved from
# `<repo>/workstreams` by runtime.workstream.config (_REPO_ROOT=/app in-image).
# Ship it so a vertical's config.yaml applies in-container instead of silently
# falling back to base defaults.
COPY workstreams/ /app/workstreams/

# Run as a non-root user.
RUN useradd --create-home --uid 10003 runtime \
    && chown -R runtime:runtime /app
USER runtime

# `import runtime` resolves; the compose `command:` selects which service to run.
ENV PYTHONPATH=/app
# Default to the worker; each compose service overrides `command:`.
CMD ["python", "-m", "runtime.worker"]
