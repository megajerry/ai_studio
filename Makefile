# AI Studio — convenience targets for the M0 infra spine.
DC := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; else echo "docker-compose"; fi)

.PHONY: help onboard up down restart ps logs health clean migrate readiness test coverage evals gateway-up gateway-token gateway-provision

help:
	@echo "AI Studio — targets:"
	@echo "  make onboard   Collect secrets/config into .env (scripts/onboarding.sh)"
	@echo "  make up        Start infra (./bootstrap: up + health check)"
	@echo "  make down      Stop infra (keep volumes)"
	@echo "  make restart   Restart infra"
	@echo "  make ps        Show service status"
	@echo "  make logs      Tail logs"
	@echo "  make health    Run the health check"
	@echo "  make migrate   Apply runtime DB migrations (event log + task queue)"
	@echo "  make readiness Cold-start readiness self-check (imports/migrations/demo/config/compose)"
	@echo "  make test      Run the full test suite (runtime + spokesman + evals)"
	@echo "  make coverage  Run the suite under coverage + print the report (needs pytest-cov)"
	@echo "  make evals     Run the evaluation harness (Verifier P/R, PM structural, telemetry)"
	@echo "  make gateway-up      Start the remote task gateway (ADR-0028; profile: gateway)"
	@echo "  make gateway-token IDENTITY=<name> [SCOPES=…]  Mint token; writes digest into .env"
	@echo "  make gateway-provision IDENTITY=<name>         Mint + start/recreate gateway"
	@echo "  make clean     Stop and REMOVE volumes (destroys local data)"

onboard:
	./scripts/onboarding.sh

up:
	./bootstrap

down:
	$(DC) down

restart:
	$(DC) restart

ps:
	$(DC) ps

logs:
	$(DC) logs -f --tail=100

health:
	./scripts/healthcheck.sh

migrate:
	python -m runtime.migrate

# Cold-start readiness self-check: does a fresh clone actually bootstrap? Runs
# real checks (imports, migrations apply to a throwaway schema, demo green,
# config/secret coverage, compose coherence) and exits non-zero on a FAIL.
readiness:
	python -m runtime.readiness

# DB-backed tests seed synthetic tasks with no teardown, so the sanctioned test
# flow declares the target DB DISPOSABLE via AI_STUDIO_TEST_DB=1 (ADR-0029). This
# opt-in is scoped to these make targets ONLY — it is never exported globally, so
# a prod host that runs the studio (not `make test`) never marks its DB disposable.
test:
	AI_STUDIO_TEST_DB=1 python -m pytest runtime/tests/ spokesman/tests/ gateway/tests/ evals/ -q

# Remote (non-LAN) task access — ADR-0028 / docs/remote-task-access.md.
gateway-up:
	$(DC) --profile gateway up -d --build task-gateway
	$(DC) --profile gateway ps task-gateway

# Mint a credential and write its digest into .env automatically (no paste).
# Prints the one-time secret on stdout — give that to the remote as TASK_GATEWAY_TOKEN.
# IDENTITY required, e.g. `make gateway-token IDENTITY=offhost-cursor`
# Optional: SCOPES=read,enqueue,claim,complete
gateway-token:
	@test -n "$(IDENTITY)" || (echo "IDENTITY= is required (e.g. offhost-cursor)" >&2; exit 2)
	@python3 -m gateway.client mint --identity $(IDENTITY) --scopes $(or $(SCOPES),read,enqueue,claim,complete)

# Mint (writes .env) + recreate the gateway so the new digest is live.
gateway-provision: gateway-token gateway-up

# Empirical quality: run the suite under coverage and print the % (needs
# `pip install -r runtime/requirements-dev.txt`). Config lives in .coveragerc.
coverage:
	AI_STUDIO_TEST_DB=1 python -m pytest runtime/tests/ spokesman/tests/ gateway/tests/ evals/ \
		--cov --cov-config=.coveragerc --cov-report=term-missing --cov-report=html -q
	@echo "HTML coverage written to htmlcov/index.html"

# Run the evaluation harness itself (seeded-defect Verifier P/R + PM structural +
# telemetry quality rollup). Writes a JSON + markdown report to state/.
evals:
	AI_STUDIO_TEST_DB=1 python -m evals --json state/eval-report.json --markdown state/eval-report.md

clean:
	$(DC) down -v
