# Go-live runbook

The ordered steps to take AI Studio from a fresh `git clone` to a running studio
on the target Mac, plus a pre-flight checklist, how to verify each subsystem is
live, and the stakeholder decisions the launch is gated on.

**Honest scope.** Everything below runs **keyless / dry-run** until you supply
real provider keys and WhatsApp credentials — the platform is designed to boot,
migrate, and run its full agent loop with **no secrets** (dry-run model + search
calls). "Dry-run" means outbound model/search/WhatsApp calls are *logged, not
made*. The steps that flip the studio from dry-run to **live** are called out
explicitly (they need the stakeholder decisions in the last section).

The self-sufficiency invariant (CLAUDE.md / CONTRIBUTING.md): *a change isn't
done until a fresh `git clone` on the target machine can bootstrap and run it.*
The readiness self-check below is how you prove that before flipping anything on.

---

## Pre-flight checklist

Run these from the repo root **before** go-live. The readiness check is the
gate — do not proceed to "bring up infra" while it reports a `FAIL`.

- [ ] **Prereqs present** — Docker runtime + `docker compose` v2, `git`, `bash`,
      `curl`, `openssl` (see [`PREREQS.md`](../PREREQS.md)). Verify:
      `docker info` succeeds, `docker compose version` prints v2.x.
- [ ] **Python deps installed** — `pip install -r runtime/requirements.txt -r spokesman/requirements.txt pytest`.
- [ ] **Readiness self-check is green** — the cold-start gate:

      ```bash
      python -m runtime.readiness        # or: make readiness
      ```

      It exits non-zero on any real `FAIL`. It runs REAL checks:
      package + declared-dependency imports; migrations form a contiguous
      `0001..000N` sequence AND apply cleanly to a throwaway isolated schema;
      `python -m runtime.demo` exits 0; **config/secret coverage** (every env var
      the runtime + spokesman actually read is documented in `.env.example` or
      collected by `scripts/onboarding.sh` — a secret the code needs that
      cold-start never asks for is a `FAIL`); and `docker-compose.yml` /
      `bootstrap` / `Makefile` reference no dangling files. `HOST-REQUIRED`
      lines are the checks only the target Mac can confirm (below) — they are not
      failures.
- [ ] **Full test suite passes** — `make test` (runtime + spokesman + evals),
      all keyless; DB-backed tests skip cleanly when no Postgres is reachable.

---

## Launch sequence (ordered)

### 1. Onboarding — collect secrets/config

```bash
./scripts/onboarding.sh          # or: make onboard
```

Writes a **git-ignored** `.env` (chmod 600). Safe to re-run; existing values are
preserved. It never echoes secret values and refuses to write into a tracked
path (ADR-0011). You can leave anything you don't have yet blank and re-run
later — the studio boots keyless. At minimum it generates the local infra
passwords (`POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`, …).

Verify it's git-ignored: `git check-ignore .env` prints `.env`.

### 2. Bootstrap — bring up the M0 infra spine  *(HOST-REQUIRED)*

```bash
./bootstrap                      # or: make up
```

Starts the Layer-0 services with `docker compose up -d` and then runs the health
check. Requires a running Docker runtime and a `.env` with the infra passwords.

### 3. Migrate — apply the runtime schema  *(HOST-REQUIRED)*

```bash
python -m runtime.migrate        # or: make migrate
python -m runtime.migrate --status   # list applied vs pending
```

Forward-only, idempotent; each migration runs in its own transaction and is
recorded in `schema_migrations`. Verify: `--status` shows every
`runtime/migrations/000N` as `applied`.

### 4. Health — verify the spine is live  *(HOST-REQUIRED)*

```bash
make health                      # scripts/healthcheck.sh
```

Polls each service until healthy. Expect ✓ for `postgres`, `redis`, `qdrant`,
`minio`, `prometheus`, `grafana`, `otel-collector`. Then open Grafana at
http://localhost:3000 (user `admin`, password = `GRAFANA_ADMIN_PASSWORD`).

### 5. Demo — see the studio operate end-to-end (keyless)

```bash
python -m runtime.demo
```

Drives the full agent loop (scheduler → PM plans + decomposes → Executor + tool
+ model call → independent Verifier → commit) against the real DB, fully
dry-run, and prints the replayable event trail. Exit 0 = the studio operates.
With no DB it prints a notice and exits 0 (deferred to host).

### 6. Provider keys — flip model + search calls to **live**

Re-run `./scripts/onboarding.sh` and fill the provider key(s) you're funding
(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`, plus
`EMBEDDINGS_PROVIDER` + its key for memory, and optionally the search provider
keys `TAVILY_API_KEY` / `EXA_API_KEY` / `BRAVE_API_KEY`). Until a key is set (or
when `MODELS_DRY_RUN=1` / `SEARCH_DRY_RUN=1`), those calls stay dry-run stubs.
**This is a stakeholder decision** (which providers, what budget — below).

Verify live vs dry-run: a real (keyed) run emits `model.call` events with a real
`provider`/`model` and non-zero cost in the event trail; dry-run emits the stub.

### 7. First workstream — pick the initial vertical  *(stakeholder decision)*

A vertical is **config, not code**: create `workstreams/<name>/config.yaml`
(charter, role overlays, budget cap, policy grants, checkers, memory seed) and
the runtime picks it up (see the workstream-config demo in `runtime.demo` and
[`docs/architecture.md`](architecture.md)). Set the budget `cap_usd` here — the
studio respects it. Nothing runs a vertical until the stakeholder chooses one.

### 8. WhatsApp Spokesman — stakeholder channel (opt-in)  *(HOST-REQUIRED)*

Opt-in; the M0 spine is unaffected by a plain `docker compose up`.

```bash
python -m runtime.migrate                                    # apply migrations first (incl. spokesman tables)
docker compose --profile spokesman up -d --build spokesman   # --build is REQUIRED after any git pull
```

**Always use `--build`** when (re)deploying the Spokesman: its code is baked into
the image, so `restart` / `up -d` without `--build` keeps the OLD code and ignores
`docker-compose.yml` env changes.

Needs the WhatsApp Meta Cloud API credentials (`WHATSAPP_*`), a chosen
`WHATSAPP_VERIFY_TOKEN`, the `SPOKESMAN_API_TOKEN`, a **public webhook tunnel**
(`TUNNEL_PROVIDER` + e.g. `CLOUDFLARED_TUNNEL_TOKEN`) so Meta can reach the
inbound webhook, **and — because the Spokesman is a model-first agent (ADR-0026) —
a model-provider key (`ANTHROPIC_API_KEY` or a fallback) in `.env`.** Run with
`SPOKESMAN_DRY_RUN=1` to log outbound sends without calling WhatsApp. Full setup:
[`docs/spokesman-whatsapp.md`](spokesman-whatsapp.md).

Verify: `GET /health` returns OK **and its nested `model` block shows
`"dry_run":false`** (a `true` there means the chat brain is a keyword stub, not a
real LLM — see the Troubleshooting section in the Spokesman runbook); a test
`/notify` (with the `X-Spokesman-Token` header) is delivered (or logged in
dry-run).

---

## What only the host can verify (HOST-REQUIRED)

These cannot be checked off-host; `runtime.readiness` lists them as
`HOST-REQUIRED` rather than failing:

- `docker compose up -d` actually brings up the spine.
- Live health of postgres / redis / qdrant / minio / prometheus / grafana
  (`make health`).
- The Spokesman service is reachable and its webhook round-trips.
- The public tunnel is up and Meta can reach the inbound webhook.

## Stakeholder decisions the launch is gated on

Cold-start (onboarding + bootstrap) cannot make these for you:

1. **Provider API keys** — which model/embedding/search providers to fund, and
   their keys (step 6). Keyless until provided.
2. **Budget ceiling** — the monthly USD cap the studio must respect (set per
   workstream `budget.cap_usd`, step 7).
3. **First vertical / product** — the initial workstream to actually run (step 7).
4. **WhatsApp provisioning** — the Meta Cloud API number/token/app-secret and a
   public tunnel (step 8).

---

## After go-live

Re-run `python -m runtime.readiness` (and `make test`) after any change that
touches bootstrap, migrations, dependencies, env vars, or compose — a change
isn't done until readiness is green on a fresh clone. Day-to-day ops:
`make ps | logs | health | down | clean`.
