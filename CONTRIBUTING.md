# Contributing to AI Studio

This repo is worked on by **agents and humans**. The same lifecycle applies to
both. It exists so that every change is reviewed, traceable, and reversible —
the same properties we demand of the system we're building.

## Development lifecycle (mandatory)

Any actor that intends to change this repo follows this loop:

1. **Branch.** Never commit to `main`. Create a feature branch named:

   ```
   <agent-workflow-identity>/<task-summary>
   ```

   - `agent-workflow-identity` — who is doing the work (the agent's workflow
     role, e.g. `bootstrap`, `infra`, `builder`, `research`, `reviewer`, or a
     human's handle).
   - `task-summary` — 1–2 words describing the task, kebab-cased
     (e.g. `scaffold`, `event-log`, `policy-engine`).

   Examples: `bootstrap/scaffold`, `infra/compose`, `builder/search-tool`.

2. **Iterate — commit early and often.** The owning agent develops and self-checks
   on the branch, **committing frequently** as it goes. Every meaningful step gets
   its own commit — for traceability, progress snapshots, and the ability to
   revert. **Push the branch** after commits so progress is visible on the remote.
   No review is needed for these in-branch commits; keep going until the change is
   ready (builds/health-checks pass, docs and any ADRs updated).

3. **Review — only at merge.** The review round is triggered **only by the merge
   back to `main`**, not by in-branch commits. Open a PR, then **spawn a dedicated
   review agent**. The reviewer and the owning agent iterate until the reviewer
   **approves**. The reviewer is a separate actor from the author — never
   self-approve. The review must be **evidence-based** ([ADR-0014](docs/decisions/0014-validation-rigor.md)):
   the reviewer runs the tests/code, reads the actual code path, and greps for
   itself — an unverified claim (the PR description, a comment, "tests pass") is
   treated as unproven, never as fact.

4. **Merge & clean up.** Once approved, merge into `main`, **push `main`**
   (push after review/QA — don't wait to be asked), then **archive/delete the
   feature branch** (locally and on the remote). Keep `main` the single source of
   truth.

## Rules

- **`main` is protected by convention.** All changes arrive via reviewed PRs;
  only the merge to `main` needs review, and merges are never self-approved.
- **Commit often on your branch; push it.** Frequent commits are the expected
  progress record (snapshots + revert points), reviewed only when merged.
- **One task per branch.** Keep branches small and focused so review is cheap.
- **Record architecture-affecting decisions** as an ADR in `docs/decisions/`
  within the same PR (see [`docs/decisions/0001-...`](docs/decisions/0001-record-architecture-decisions.md)).
- **Never commit secrets.** Use `.env` (git-ignored) and document new variables
  in `.env.example`.
- **Run tests against a DISPOSABLE database only (ADR-0029).** DB-backed tests
  seed synthetic `work.*` / `pollute-*` tasks and never tear down, so the suite
  **refuses to touch a non-disposable DB**: it skips loudly unless `AI_STUDIO_TEST_DB=1`
  is set OR the target DB name matches a test pattern (`*_test` / contains `test`).
  Use `make test` (it sets the opt-in for you) or point `DATABASE_URL` at a `*_test`
  database. Never set `AI_STUDIO_TEST_DB` on a host that runs the real studio.
  `python -m runtime.demo` is exempt — it self-cleans its own workstreams and is
  safe to run on the host.
  - **CI must use `make test`** (or assert zero unexpected skips). A bare `pytest`
    against a reachable non-disposable DB skips the whole DB suite yet still exits 0,
    so an all-skipped run could otherwise be mistaken for a green pass.
- **Respect the invariants** in [`CLAUDE.md`](CLAUDE.md). A change that breaks one
  must carry an ADR justifying it.
- **Self-sufficiency.** A change isn't done until a fresh `git clone` on the
  target machine can bootstrap and run it — update bootstrap scripts / prereqs
  / `.env.example` accordingly. **Prove it with the cold-start readiness check:**
  `python -m runtime.readiness` (or `make readiness`) must be green — it verifies
  imports, migrations, the demo, config/secret coverage (every env var the code
  reads is documented in `.env.example` or collected by onboarding), and compose
  coherence. See the [go-live runbook](docs/go-live.md).

## Commit & PR conventions

- Commit messages: imperative mood, scoped (`infra: add postgres to compose`).
- PR description states: what changed, why, how it was verified, and any new
  env vars / prereqs.
