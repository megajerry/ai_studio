#!/bin/sh
# AI Studio Postgres entrypoint wrapper (host-restricted remote access, ADR-0017).
# =============================================================================
# Renders the pg_hba allowlist from PG_ALLOWED_HOSTS at container start, then
# hands off to the stock postgres entrypoint pointed at the rendered file. This is
# what makes the remote allowlist ENV-DRIVEN: change PG_ALLOWED_HOSTS + restart.
#
# Wired in docker-compose.yml via `entrypoint:` + read-only mounts of this script,
# render-pg-hba.sh, and pg_hba.conf.template. POSIX sh (alpine busybox).
set -eu

RENDERED=/tmp/pg_hba.conf
render-pg-hba.sh /etc/postgresql/pg_hba.conf.template > "$RENDERED"

echo "pg-entrypoint: rendered $RENDERED (PG_ALLOWED_HOSTS='${PG_ALLOWED_HOSTS:-}')" >&2

# listen_addresses='*' only means "accept on all container interfaces"; actual
# exposure is gated by pg_hba (this allowlist) + the host port binding (PG_BIND_IP)
# + the firewall. Never rely on listen_addresses alone to restrict access.
exec docker-entrypoint.sh postgres \
    -c hba_file="$RENDERED" \
    -c listen_addresses="${PG_LISTEN_ADDRESSES:-*}" \
    -c password_encryption=scram-sha-256
