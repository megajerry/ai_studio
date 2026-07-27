# AI Studio task gateway — scoped remote access to the task queue (ADR-0028).
# Single-purpose FastAPI service; config/secrets come from the environment at
# runtime (ADR-0011) — no token, digest or DB credential is baked into the image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TASK_GATEWAY_PORT=8081

WORKDIR /app

# Dependencies first for layer caching. The gateway imports the runtime package
# (runtime.tasks is the only way it touches state), so install both sets.
COPY gateway/requirements.txt /app/gateway/requirements.txt
COPY runtime/requirements.txt /app/runtime/requirements.txt
RUN pip install --no-cache-dir \
      -r /app/gateway/requirements.txt \
      -r /app/runtime/requirements.txt

COPY gateway/ /app/gateway/
COPY runtime/ /app/runtime/

# Run as a non-root user.
RUN useradd --create-home --uid 10002 gateway \
    && chown -R gateway:gateway /app
USER gateway

EXPOSE 8081

ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn gateway.app:app --host 0.0.0.0 --port ${TASK_GATEWAY_PORT:-8081}"]
