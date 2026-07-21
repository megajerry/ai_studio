# Prerequisites — host setup (M0)

The host is a Mac. Everything runs in containers; nothing else is required beyond
a container runtime, git, and bash.

## Required

- **A Docker-compatible runtime + `docker compose` v2.** Any of:
  - [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  - [OrbStack](https://orbstack.dev/) (lightweight, recommended on Apple Silicon)
  - [colima](https://github.com/abiosoft/colima) (`brew install colima docker docker-compose`, then `colima start`)
- **git**, **bash**, **curl**, **openssl** (all preinstalled on macOS).

Verify: `docker info` succeeds and `docker compose version` prints v2.x.

## Ports (host)

Defaults (override in `.env`): Postgres `5432`, Redis `6379`, Qdrant `6333/6334`,
MinIO `9000/9001`, OTLP `4317/4318`, Prometheus `9090`, Grafana `3000`. Make sure
these are free (or set the `*_PORT` vars).

## First run

```bash
git clone <repo> && cd ai_studio
./scripts/onboarding.sh     # collect secrets/config into git-ignored .env
./bootstrap                 # start infra + health check   (or: make up)
```

Then open Grafana at http://localhost:3000 (user `admin`, password =
`GRAFANA_ADMIN_PASSWORD` from `.env`, default `admin` — change it).

## Common commands

`make ps` · `make logs` · `make health` · `make down` · `make clean` (removes volumes).

## Notes

- **M0 is Layer 0 only** — infra spine. The supervisor, scheduler, agent runtime,
  and Spokesman arrive in later milestones (see `docs/bootstrap-sequence.md`).
- Data persists in named Docker volumes; `make clean` destroys it.
- This is a local-first system: the Mac is the source of truth (ADR-0007).
