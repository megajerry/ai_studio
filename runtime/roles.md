# `runtime/` M3c — roles + worker (the studio operates end-to-end)

The minimal set of roles + the on-demand worker that make the studio **operate
end-to-end in dry-run** (no API keys, no Docker). This is the first time the merged
substrate runs as one loop: task queue + event log (M1), the policy-gated tool
path (M2), and the single instrumented model call (M3b), driven by three roles.

Everything here obeys the CLAUDE.md invariants: agents don't call agents
(coordination is via the queue/events), agents don't touch the host (side effects
only through a tool via `invoke`), mutations go through **verify → commit**, and
every action emits an event. It runs **fully keyless** — `call_model` falls back
to the dry-run provider when no key is present.

## Layout

| File | Purpose |
| --- | --- |
| `roles/pm.py` | `run_pm_tick` — understand → confidence-gate → **decompose**: parse a structured `Plan` from `call_model(task_type=plan)`, then enqueue ONE `work.*` task **per work item** (emits `pm.planned` w/ count+ids); or push back (`pm.pushback` + 🛑 approval) / clarify (`pm.needs_clarification`) |
| `roles/executor.py` | `run_executor` — DO the work: a policy-gated `filesystem` write + a `call_model` dry-run call |
| `roles/verifier.py` | `verify` — INDEPENDENT verify→commit gate (read-only); returns pass/fail |
| `roles/reviewer.py` | `run_review` — INDEPENDENT risk/disaster guard (read-only): reads a finished episode's trail + artifact, computes fact-based risk signals; emits `review.passed`/`review.flagged` (+ 🚨 `review.alarm` / 🛑 approval on HIGH) |
| `roles/retro.py` | `run_retro` — distill 1-3 durable **lessons** from an episode's trail into Knowledge memory; emits `retro.completed` (count only) |
| `roles/researcher.py` | `run_research` — mine **external** best-practice: `search()` via the policy-gated gateway (`net.fetch`) → `call_model(task_type=research)` dry-run → distill 1-3 **lessons** into Knowledge memory (+ optional `reviewed: false` candidate skill, off by default); emits `research.completed` (topic-hash + counts only); enqueues nothing (no loop) |
| `roles/lessons.py` | `inject_lessons`/`compose_lessons` — auto-inject the recalled lessons into a role's prompt (`### Lessons`), the deterministic apply-the-lesson step |
| `worker.py` | `run_once` (claim → dispatch → heartbeat → verify→commit; triggers Retro on terminal work), `run()`/`main()` |
| `demo.py` | `python -m runtime.demo` — four acts against a live DB (skips w/o DB): end-to-end loop, learning loop, Reviewer guard, and the **Researcher** (search gateway → distilled recallable lessons) |
| `tests/test_roles.py` | role units + policy-gate refusals (🔴 delete/shell) — keyless, no DB |
| `tests/test_worker.py` | full loop via an in-memory fake queue; verify-fail re-enqueue |
| `tests/test_retro.py` | lesson distillation + retro trigger policy + NO retro-loop; live-DB run_retro |
| `tests/test_reviewer.py` | pure `assess_risks` signals + `WORKER_REVIEW` trigger policy + NO review-loop; live-DB `run_review` (evidence beats a lying model, HIGH → 🚨/🛑, events leak no secrets) |
| `tests/test_researcher.py` | finding distillation (bounded, adaptive-lite) + research dispatch + NO research-loop; live-DB `run_research` (gateway-gated search, `net.fetch` denial, recallable lessons, events leak no bodies, drafted skill `reviewed: false` excluded by inject gate) |
| `tests/test_lessons.py` | lesson injection: bounded/scoped, behavior-preserving with no lessons |

> A role is `prompt + skills + tools` (architecture §3). Today the prompt is an
> **inline string template** on each role and the "skills" layer (Agent Skills
> standard, ADR-0008) is deferred to a later milestone. Roles act on the world
> only through `invoke` (tools) and `call_model` (models).

## The operating loop (architecture §4, ADR-0004/0009)

```
scheduler.tick_once ──enqueue──> pm.tick
        │
        ▼  worker.run_once (claim pm.tick)
   PM: obtain a structured Plan (call_model role=pm task_type=plan)  ──emits──> model.routed, model.call
       parse JSON → Plan{restated_goal, success_criteria, confidence, feasible, work_items}
       CONFIDENCE GATE:
         · not feasible          → pm.pushback + 🛑 approval (request_approval)   [NO work]
         · confidence < threshold → pm.needs_clarification                        [NO work]
         · else → DECOMPOSE: enqueue ONE work.* task per work_item
                  (payload: goal, criterion, marker) ──emits──> pm.planned (count+ids), task.created ×N
        │
        ▼  worker.run_once (claim each work.* task)   [heartbeats around each phase]
   Executor: call_model(role=exec task_type=execute)         ──emits──> model.routed, model.call
             invoke(role=executor, filesystem, op=write ...)  ──emits──> policy.decision, tool.invoked
   Verifier: call_model(role=verifier task_type=verify)      ──emits──> model.routed, model.call
             invoke(role=verifier, filesystem, op=read ...)    ──emits──> policy.decision, tool.invoked
             deterministic check: marker present in artifact? ──emits──> verify.passed | verify.failed
        │
        ├─ pass → complete_task(status=done)                  ──emits──> task.finished   ← verify→commit
        └─ fail → complete_task(status=failed) + bounded re-enqueue (attempt+1)
                                                              ──emits──> task.finished, work.retry
```

**The PM contract — understand → gate → decompose (ADR-0003).** The PM is the
supervisor and the only role that plans. It does not hard-code a single work task;
it obtains a **structured `Plan`** (a `pydantic` model: `restated_goal`,
`success_criteria`, a self-scored `confidence` ∈ [0,1], `feasible` + `reason`, and
`work_items`) by parsing the planning `call_model` output (defensively — unparseable
output degrades to a safe low-confidence fallback, never a crash). It then runs the
**confidence gate**:

- **not feasible** → *push back* (a first-class output, ADR-0003): emit
  `pm.pushback` and raise a 🛑 human approval via `approvals.request_approval`
  (objective/scope concern, ADR-0006). No work is enqueued.
- **confidence < `PM_CONFIDENCE_THRESHOLD`** (env, default `0.6`) or nothing to
  decompose → *clarify*: emit `pm.needs_clarification` instead of executing.
- **otherwise** → *decompose*: enqueue ONE `work.*` task **per `WorkItem`**, each
  carrying its own concrete, marker-based `criterion` in the payload so the
  Verifier still checks a real artifact per item; emit `pm.planned` with the item
  **count + task ids** (never prompt/secret text).

Keyless, the plan comes from the dry-run provider's deterministic
`build_dry_run_plan` (2–3 marker-based items derived from the goal); a real model
wired later returns the same JSON schema from the natural-language prompt, so no PM
code changes. `call_model`, `enqueue`, and `request_approval` are injectable seams
so every gate branch is unit-tested with no database.

**verify → commit is enforced**: a `work.*` task never becomes `done` until the
Verifier returns `passed`. The Verifier is a *separate* role, granted only
`fs.read`, so it can inspect but never "fix" the work it judges (independence).

## Validator doctrine — evidence over claims (ADR-0014)

Every **validator** in the studio — the Verifier today, the future
**Reviewer / Whistle-blower** ([ADR-0003](../docs/decisions/0003-workstream-operating-model.md)),
and any review/audit agent — adopts one doctrine: **trust only evidence you
observe yourself, never the author's claim.** LLMs default to accepting stated
claims as true; for a validator that is a defect.

- The doctrine is packaged as the reusable, reviewed **`rigorous-review`** skill
  (`skills/rigorous-review/SKILL.md`), injected on demand into a validator's
  prompt via the registry (`skills.inject.compose_prompt`), exactly as the PM
  injects `define-success-criteria`.
- Evidence hierarchy: (1) run the command/test and read its real output; (2) read
  the actual code path; (3) inspect logs/metrics/DB rows/artifacts. NOT the
  author's summary, comments, commit message, or an unrun "the test asserts X".
- Per claim: cite the specific evidence and a verdict CONFIRMED / UNVERIFIED /
  REFUTED; unobtainable evidence ⇒ UNVERIFIED — **never approve on trust.**
- The **Verifier** already lives this: its verdict is the deterministic re-read of
  the ACTUAL artifact against the success criterion (`_check`), NOT the Executor's
  `result.ok`. A false "done" claim over an artifact that fails the criterion
  still FAILS (`runtime/tests/test_roles.py::test_verify_evidence_beats_false_done_claim`).
- The **Reviewer / Whistle-blower** (`roles/reviewer.py`) lives it too: every risk
  signal is computed from FACTS it observes itself (the real trail + a re-read of
  the actual artifact), never from a model's "looks fine". A monkeypatched lying
  model does not change its verdict (`test_reviewer.py::test_review_evidence_beats_lying_model_flags_hallucination`).

## Reviewer / Whistle-blower — the independent risk & disaster guard (ADR-0003)

ADR-0003 calls for a **Reviewer / Whistle-blower**: an independent guard that
"spot[s] anything that will lead to failure/disaster" — a *general* reviewer, run
at **adaptive intensity** (more review when the recent error rate is high). This
is that role, and it is deliberately **distinct from the Verifier**:

| | **Verifier** (`verifier.py`) | **Reviewer / Whistle-blower** (`reviewer.py`) |
| --- | --- | --- |
| Question | Does the artifact meet *this task's* success criterion? | Does anything about this episode look like failure/disaster? |
| Scope | One narrow criterion | The whole episode (trail + artifact + counters) |
| Timing | **Gate** — its pass is what makes a work task `done` | **After-the-fact** — runs on the *finished* episode; does NOT block/gate |
| Effect | pass → commit; fail → re-enqueue/fail | raises **signals** (`review.flagged`) + escalates (🚨/🛑); never blocks the queue |
| Privilege | `fs.read` (read-only) | `fs.read` (read-only) |

The Verifier can PASS a task the Reviewer still FLAGS — e.g. the criterion was met
but the episode blew its token budget, was re-kicked repeatedly, or a 🔴 delete kept
getting gated.

**Risk / disaster signals** (all fact-derived by the pure `assess_risks`, so reasons
carry counts/numbers only — never a secret, arg value, artifact body, or marker):

- **hallucinated success** — the trail claims done/verified but the *real* artifact
  does not back it (missing / unreadable / success marker absent) → HIGH. This is
  evidence-over-claim: it only fires when the claim can be **refuted** from facts
  (an unreadable-because-no-registry artifact stays UNVERIFIED, never a false flag).
- **budget blowout** — `spent_tokens` over `budget_tokens` → HIGH (≥90% → MEDIUM).
- **repeated failures / re-kicks** — `verify.failed` + `work.retry` + `task.rekicked`
  in the trail plus `retries`; ≥4 → HIGH, ≥2 → MEDIUM.
- **recurring policy denials** — `policy.decision` DENYs in the episode.
- **irreversible / costly actions gated** — `approval.requested` (🔴) events.

Verdict = `ReviewResult(ok | flagged, severity, reasons[])`. It emits
`review.passed` / `review.flagged`; a **HIGH** finding escalates — emits
`review.alarm` (🚨) and raises a 🛑 human approval via `approvals.request_approval`
(ADR-0006). The (traceability-only) model call has the `rigorous-review` skill
injected but does NOT decide anything.

**Adaptive trigger (`WORKER_REVIEW`, adaptive-lite).** The worker enqueues a
`review` task after a terminal `work.*` task per policy: `on_risk` (default) runs
the guard only when the episode looks risky (failed / re-kicked / over budget) —
"more review when the error rate is high", at minimal cost on the happy path;
`always` reviews every episode; `off` disables it. A `review` task dispatches to
`run_review` and **never enqueues another task**, so it can trigger neither another
review nor a retro — there is no review-loop (nor a review↔retro loop).

## The learning loop (ADR-0003)

The studio *learns from mistakes over time* structurally, not by hoping the model
remembers. A finished episode's lessons are distilled once, stored durably, and
**auto-injected** into future work:

```
work.* finishes (done|failed)
        │  worker enqueues a `retro` task  (WORKER_RETRO=on_fail|always|off)
        ▼  worker.run_once (claim retro)   [a retro NEVER enqueues another → no loop]
   Retro: read the episode's event trail (read_events, deterministic seq order)
          call_model(role=retro, task_type=retro)   ── traceability only (dry-run)
          distill 1-3 concise lessons (bounded; failures → prevention lesson)
          memory.add_lesson(...) → Knowledge layer   ──emits──> memory.remembered (id/dims only)
          ──emits──> retro.completed (lesson COUNT + task ref — NEVER the lesson text)
        │
        ▼  NEXT time any PM/Executor acts in this workstream
   recall_lessons(conn, workstream, query, k) → inject_lessons(prompt, …)
          bounded, delimited `### Lessons` section prepended to the role's prompt
          (workstream-scoped + shared global corpus; ADR-0013 bounded injection)
```

**Design properties (ADR-0003):**

- **Prompt-level prevention > runtime correction.** Applying a lesson is the
  deterministic `inject_lessons` step at prompt assembly — it does not depend on
  the model recalling anything.
- **Cross-episode accumulation > single-pass reflection.** Lessons persist in the
  Knowledge layer and compound across episodes; there is **no reflection loop**
  (≤ `MAX_LESSONS=3` per retro, single pass over the trail).
- **Adaptive intensity (adaptive-lite).** `WORKER_RETRO=on_fail` (default) runs a
  retro only on a failed episode — "more retro when the error rate is high" — at
  minimal token cost; `always` retros every episode; `off` disables the trigger.
- **No leakage.** `retro.completed` and the memory events carry only counts/ids;
  the lesson text lives in the Knowledge layer, never on the event log.
- **Behavior-preserving.** With no lessons (or no `conn`), `inject_lessons` returns
  the base prompt unchanged — the roles behave exactly as before.

## The Researcher — learning from outside (ADR-0003)

Where the Retro learns from an episode's **own** trail, the **Researcher**
(`roles/researcher.py`) mines **external** best-practice/tools into the same
reusable Knowledge corpus. It runs on a `research` task and acts only through the
sanctioned seams — never agent-direct (architecture §9):

```
research task (payload.topic|question)
        ▼  worker.run_once (claim research)   [a research task NEVER enqueues → no loop]
   Researcher: search(conn, role="researcher", query=topic)   ── policy-gated on net.fetch,
               cached, keyless dry-run   ──emits──> search.cache_miss/hit, search.provider_call
                                                    (provider/count/latency + query HASH — no bodies)
          call_model(role=researcher, task_type=research)      ── traceability only (dry-run);
                                                    digest is titles/urls only (no fetched body)
          distill 1-3 reusable lessons (bounded; fast-moving domain → +revisit lesson)
          memory.add_lesson(...) → Knowledge layer   ──emits──> memory.remembered (id/dims only)
          [optional, off by default] draft a candidate SKILL.md via the policy-gated
                 invoke(role=researcher, tool_name="filesystem", op="write") →
                 frontmatter `reviewed: false` + `source` provenance  ⇒ the inject gate
                 (`filter_injectable`) NEVER auto-injects it (review-before-use, ADR-0008)
          ──emits──> research.completed (topic-HASH + counts + skill_drafted bool — NEVER bodies)
        │
        ▼  the distilled lessons are recall_lessons-able and auto-injected into future work
```

**Design properties:**

- **Gateway only, never agent-direct.** The Researcher cannot fetch the network
  itself; every search is the choke-point `search()` (policy → cache → provider →
  cache → memory). A role lacking `net.fetch` is DENIED before any provider call.
- **Adaptive intensity (ADR-0003).** A fast-moving domain (AI/LLM/security/…)
  earns an extra "treat this as perishable — re-research before relying on it"
  lesson. Output is bounded (≤ `MAX_LESSONS=3`, single pass).
- **Review-before-use.** A drafted candidate skill is always `reviewed: false`,
  so it is excluded from prompt injection until a human reviews + flips it.
- **No leakage / no loop.** `research.completed` carries the topic *hash* + counts
  + ids only (never the raw topic, result bodies, or lesson text); a research task
  enqueues nothing.
- **Note (future PM wiring).** The PM's low-confidence branch
  (`pm.needs_clarification`) *could* enqueue a `research` task to gather
  best-practice before re-planning — not wired here (the PM is unchanged), noted
  as the intended hook.

### Roles & least privilege (policy.example.yaml)

| Role | Granted capabilities | Why |
| --- | --- | --- |
| `pm` | `fs.read` | plans + calls a model; never does the work |
| `executor` | `fs.read`, `fs.write` | writes the scratch artifact (🟡). **No `fs.delete`/`shell.exec`** → those DENY |
| `verifier` | `fs.read` | read-only independent check |
| `reviewer` | `fs.read` | read-only independent risk/disaster guard — inspects the trail + artifact but can never touch the work it judges |
| `retro` | *(none)* | reads the event trail + calls a model; writes lessons to Knowledge memory (not a host tool). No tool capabilities needed |
| `researcher` | `fs.read`, `net.fetch` | searches ONLY via the policy-gated gateway (`net.fetch` 🟢); distills into Knowledge lessons (not a host tool). Drafting a candidate skill needs `fs.write` (🟡) — off by default and DENIED under the least-privilege default, so a draft is an explicit, granted opt-in |

A 🔴 tool from a role that lacks the capability → **DENY** (e.g. executor→delete);
a 🔴 tool from a role that *has* it → **NEEDS_APPROVAL** (never auto-executes).
Both are asserted in the tests.

## Worker

`run_once(conn, worker_id, sink, *, registry, config, …)` is the single testable
unit:

1. `claim_task` (M1) — grab+start the highest-priority grabbable `up_for_grabs` task (deps met), or `None` (caller idles).
2. dispatch by `task.type`: `pm.tick` → PM; `work.*` → Executor then Verifier;
   `retro` → Retro; `review` → Reviewer; `research` → Researcher. The `retro` /
   `review` / `research` handlers get NO `enqueue` seam, so none can spawn another
   task (no loop).
3. `heartbeat` around each work phase (liveness is the **worker's** job, not the
   role's — the supervisor re-kicks a task whose heartbeat goes stale, M3a).
4. verify pass → `complete_task(done)`; fail → bounded re-enqueue (`work.retry`,
   `attempt+1`) up to `WORKER_MAX_WORK_ATTEMPTS`, else `complete_task(failed)`.

Every seam (`claim`/`heartbeat`/`complete`/`enqueue` + the three role handlers) is
injectable, so the whole loop is driven in tests with an in-memory fake queue and
no database.

`run()` + `main()` are the on-demand driver: claim + service tasks, sleeping
`WORKER_IDLE_SLEEP_S` only when the queue is empty; reconnects on a dropped
connection and never lets one bad task kill the driver (mirrors the
supervisor/scheduler). `python -m runtime.worker`.

### How it fits with supervisor + scheduler (M3a)

- **scheduler** (`runtime.scheduler`) enqueues the `pm.tick` pulse without pileup.
- **worker** (this milestone) materializes on demand to claim + service any task.
- **supervisor** (`runtime.supervisor`) is the liveness backstop: it re-kicks
  in-progress tasks whose heartbeat went stale and force-fails ones that exhaust
  their retries. The worker's heartbeats are what keep a healthy task off the
  supervisor's radar.

All three are **non-LLM** drivers; the reasoning lives only in the roles, and even
there only through `call_model`.

## Demo

```bash
python -m runtime.demo      # forces MODELS_DRY_RUN; needs Postgres
```

Runs tick → PM → work → Executor + Verifier → done against a real DB and prints
the event trail, then a **second act** demonstrating the learning loop: a work
task fails → Retro distills a lesson → the next PM prompt for that workstream is
shown to include the recalled `### Lessons` section (prints `lesson learned: N`).
With **no** database it prints a notice and exits 0 (deferred to host
verification) — it never hangs.

## Config (env)

- `WORKER_ID` — stable worker identity (default `worker-<rand>`).
- `WORKER_SCRATCH_DIR` — the FilesystemTool root (default a temp dir).
- `WORKER_IDLE_SLEEP_S` — poll gap when the queue is empty (default 5s).
- `WORKER_MAX_WORK_ATTEMPTS` — verify-fail re-enqueues before failing (default 2).
- `WORKER_RETRO` — when a terminal work task triggers a Retro: `on_fail`
  (default) | `always` | `off` (the learning-loop trigger; adaptive-lite).
- `WORKER_REVIEW` — when a terminal work task triggers the Reviewer/Whistle-blower:
  `on_risk` (default) | `always` | `off` (the risk-guard trigger; adaptive-lite —
  `on_risk` fires only on a failed / re-kicked / over-budget episode).
- `PM_CONFIDENCE_THRESHOLD` — the PM confidence gate threshold (default `0.6`);
  a plan scoring below it yields `pm.needs_clarification` instead of executing.
- `MODELS_DRY_RUN=1` — force keyless dry-run (the demo sets this).

## Verify

```bash
pip install -r runtime/requirements.txt pytest
python -m py_compile runtime/*.py runtime/roles/*.py
pytest runtime/tests/                 # full loop keyless; DB e2e skips w/o Postgres
python -m runtime.demo                # end-to-end on the host (skips w/o DB)
```

No network, no keys, no Docker required for the tests: the loop runs on the
dry-run provider with a temp-dir FilesystemTool and a `MemoryEventSink`/fake queue;
the DB end-to-end test (`test_integration_db.py::test_worker_full_loop_pm_to_done`)
skips cleanly when no Postgres is reachable.
