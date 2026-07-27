# AI Studio — convenience targets for the M0 infra spine.
DC := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; else echo "docker-compose"; fi)

.PHONY: help onboard up down restart ps logs health clean migrate readiness test coverage evals gateway-up gateway-token

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
	@echo "  make gateway-up      Start the remote task gateway (ADR-0027; profile: gateway)"
	@echo "  make gateway-token IDENTITY=<name> [SCOPES=read,enqueue,claim]  Mint a remote token"
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

test:
	python -m pytest runtime/tests/ spokesman/tests/ gateway/tests/ evals/ -q

# Remote (non-LAN) task access — ADR-0027 / docs/remote-task-access.md.
gateway-up:
	$(DC) --profile gateway up -d --build task-gateway
	$(DC) --profile gateway ps task-gateway

gateway-token:
	@python3 -m gateway.client mint --identity $(IDENTITY) --scopes $(or $(SCOPES),read,enqueue,claim)

# Empirical quality: run the suite under coverage and print the % (needs
# `pip install -r runtime/requirements-dev.txt`). Config lives in .coveragerc.
coverage:
	python -m pytest runtime/tests/ spokesman/tests/ gateway/tests/ evals/ \
		--cov --cov-config=.coveragerc --cov-report=term-missing --cov-report=html -q
	@echo "HTML coverage written to htmlcov/index.html"

# Run the evaluation harness itself (seeded-defect Verifier P/R + PM structural +
# telemetry quality rollup). Writes a JSON + markdown report to state/.
evals:
	python -m evals --json state/eval-report.json --markdown state/eval-report.md

clean:
	$(DC) down -v
