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

# Run as a non-root user.
RUN useradd --create-home --uid 10003 runtime \
    && chown -R runtime:runtime /app
USER runtime

# `import runtime` resolves; the compose `command:` selects which service to run.
ENV PYTHONPATH=/app
# Default to the worker; each compose service overrides `command:`.
CMD ["python", "-m", "runtime.worker"]
