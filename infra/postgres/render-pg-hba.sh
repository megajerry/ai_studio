#!/bin/sh
# Render pg_hba.conf from the template, expanding the PG_ALLOWED_HOSTS allowlist.
# =============================================================================
# POSIX sh (postgres:*-alpine ships busybox, no bash). Emits the final
# pg_hba.conf to stdout so it can be piped to a writable file at container start
# (see docker-entrypoint-wrapper.sh) or rendered ahead of time on the host.
#
# Usage:
#   PG_ALLOWED_HOSTS="192.168.1.0/24 10.0.0.5/32" \
#     infra/postgres/render-pg-hba.sh [template] > pg_hba.conf
#
# Env:
#   PG_ALLOWED_HOSTS  space/comma-separated CIDR allowlist of authorized hosts
#                     (empty => no remote access; local/internal only)
#   POSTGRES_DB       database the remote rules grant (default: aistudio)
#   POSTGRES_USER     role the remote rules grant   (default: aistudio)
#
# Exit codes: 0 ok; 2 refused an internet-wide CIDR (allowlist must be specific).
set -eu

TEMPLATE="${1:-$(dirname "$0")/pg_hba.conf.template}"
DB="${POSTGRES_DB:-aistudio}"
ROLE="${POSTGRES_USER:-aistudio}"
MARKER='# @PG_ALLOWED_HOSTS@'

# Build one `host` rule per authorized CIDR. Commas or whitespace both separate.
rules=""
allow="$(printf '%s' "${PG_ALLOWED_HOSTS:-}" | tr ',' ' ')"
for cidr in $allow; do
    case "$cidr" in
        0.0.0.0/0 | ::/0)
            echo "render-pg-hba: refusing internet-wide CIDR '$cidr' — the allowlist must name specific hosts/subnets" >&2
            exit 2
            ;;
    esac
    rules="${rules}host    ${DB}    ${ROLE}    ${cidr}    scram-sha-256
"
done

if [ -z "$rules" ]; then
    rules="# (PG_ALLOWED_HOSTS empty — no remote hosts authorized; local/internal only)"
fi

# Substitute the marker line with the rendered allowlist rules; pass the rest
# through verbatim so the security-contract header is preserved.
while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$MARKER" ]; then
        printf '%s\n' "$rules"
    else
        printf '%s\n' "$line"
    fi
done < "$TEMPLATE"
