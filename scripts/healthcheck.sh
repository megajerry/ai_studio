#!/usr/bin/env bash
# AI Studio — M0 health check. Polls each infra service until healthy (or times
# out). Run after `./bootstrap` / `make up`. Self-verifies the clone on the host.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
[[ -f .env ]] && { set -a; source .env 2>/dev/null || true; set +a; }

PGUSER="${POSTGRES_USER:-aistudio}"
PGDB="${POSTGRES_DB:-aistudio}"
QDRANT_HTTP_PORT="${QDRANT_HTTP_PORT:-6333}"
MINIO_API_PORT="${MINIO_API_PORT:-9000}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"

RETRIES="${HEALTHCHECK_RETRIES:-30}"
SLEEP="${HEALTHCHECK_SLEEP:-3}"

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

# check NAME "command..."  → returns 0 if the probe succeeds
check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  ✓ %s\n' "$name"; return 0
  fi
  printf '  … %s\n' "$name"; return 1
}

probe_all() {
  local ok=0
  check "postgres"       $DC exec -T postgres pg_isready -U "$PGUSER" -d "$PGDB" || ok=1
  check "redis"          bash -c "$DC exec -T redis redis-cli ping | grep -q PONG" || ok=1
  check "qdrant"         curl -fsS "http://localhost:${QDRANT_HTTP_PORT}/healthz" || ok=1
  check "minio"          curl -fsS "http://localhost:${MINIO_API_PORT}/minio/health/live" || ok=1
  check "prometheus"     curl -fsS "http://localhost:${PROMETHEUS_PORT}/-/healthy" || ok=1
  check "grafana"        bash -c "curl -fsS http://localhost:${GRAFANA_PORT}/api/health | grep -q ok" || ok=1
  check "otel-collector" bash -c "$DC ps otel-collector | grep -Eq 'Up|running'" || ok=1
  return $ok
}

echo "Waiting for AI Studio services to become healthy…"
for i in $(seq 1 "$RETRIES"); do
  echo "attempt $i/$RETRIES:"
  if probe_all; then
    echo "All services healthy. ✅"
    echo "  Grafana:    http://localhost:${GRAFANA_PORT} (admin / see GRAFANA_ADMIN_PASSWORD)"
    echo "  Prometheus: http://localhost:${PROMETHEUS_PORT}"
    echo "  MinIO:      http://localhost:${MINIO_API_PORT}"
    echo "  Qdrant:     http://localhost:${QDRANT_HTTP_PORT}"
    exit 0
  fi
  sleep "$SLEEP"
done

echo "Some services did not become healthy in time. ❌" >&2
echo "Inspect with: $DC ps  and  $DC logs" >&2
exit 1
