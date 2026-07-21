# AI Studio — convenience targets for the M0 infra spine.
DC := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; else echo "docker-compose"; fi)

.PHONY: help onboard up down restart ps logs health clean

help:
	@echo "AI Studio — targets:"
	@echo "  make onboard   Collect secrets/config into .env (scripts/onboarding.sh)"
	@echo "  make up        Start infra (./bootstrap: up + health check)"
	@echo "  make down      Stop infra (keep volumes)"
	@echo "  make restart   Restart infra"
	@echo "  make ps        Show service status"
	@echo "  make logs      Tail logs"
	@echo "  make health    Run the health check"
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

clean:
	$(DC) down -v
