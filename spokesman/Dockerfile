# AI Studio Spokesman — WhatsApp stakeholder channel (ADR-0006).
# Small, single-purpose FastAPI service. Config/secrets come from the
# environment at runtime (ADR-0011); nothing sensitive is baked into the image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SPOKESMAN_PORT=8080

WORKDIR /app

# Install dependencies first for better layer caching. Spokesman imports the
# runtime package (bridge + grounding gate), so install both requirement sets.
COPY spokesman/requirements.txt /app/spokesman/requirements.txt
COPY runtime/requirements.txt /app/runtime/requirements.txt
RUN pip install --no-cache-dir \
      -r /app/spokesman/requirements.txt \
      -r /app/runtime/requirements.txt

# Application code — runtime is a hard import dependency of spokesman.app /
# runtime_bridge / grounding_gate (not an optional extra).
COPY spokesman/ /app/spokesman/
COPY runtime/ /app/runtime/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 spokesman \
    && chown -R spokesman:spokesman /app
USER spokesman

EXPOSE 8080

# Bind to SPOKESMAN_PORT (default 8080). state/ is mounted at runtime.
# PYTHONPATH=/app so `import runtime` / `import spokesman` resolve.
ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn spokesman.app:app --host 0.0.0.0 --port ${SPOKESMAN_PORT:-8080}"]
