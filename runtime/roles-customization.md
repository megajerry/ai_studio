# Role customization seams — how a vertical specializes the shared roles

The platform (this repo, the Productivity horizontal — ADR-0002) owns the **role
procedures**: the PM's confidence gate + decomposition, the Executor's tool+model
step, the Verifier's independent verify→commit gate, and the shared learning /
retro / reviewer / telemetry. A **vertical** (a game, a video channel, a product)
should be able to inject its *own* objective, framing, prompt, and *domain checks*
**without editing role code** — config, not a fork.

Two disjoint seams make that possible (both behavior-preserving until a vertical
opts in — the platform's own tests stay byte-identical):

1. a **role prompt-assembly layer** — `runtime/roles/prompt.py`;
2. a **pluggable verify-checker registry** — `runtime/roles/checkers.py`.

Everything else (event log, task queue, policy engine, learning loop, retro,
reviewer, telemetry) is reused unchanged.

## 1. Prompt-assembly layer (`compose_role_prompt`)

Every role now builds its prompt through **one** assembler instead of an inline
string, layering, in this fixed order, each in a bounded, clearly-delimited
section (context discipline, ADR-0013):

```
compose_role_prompt(
    role_base,                    # 1. shared platform persona for the role
    workstream_charter=None,      # 2. the vertical's mission + operating context
    role_overlay=None,            # 3. the vertical's specialization of THIS role
    skills=None,                  # 4. selected reviewed skills   (ADR-0008)
    lessons=None,                 # 5. recalled durable lessons    (ADR-0003)
    task=None,                    # 6. per-task specifics
) -> str
```

| Layer | Section header | Source | Who supplies it |
| --- | --- | --- | --- |
| role base | *(the persona, verbatim)* | platform | the role module (`_PLAN_PROMPT`, `_EXEC_PROMPT`, `_VERIFY_PROMPT`) |
| workstream charter | `### Workstream charter` | vertical **config** | workstream-bootstrap primitive |
| role overlay | `### Role overlay` | vertical **config** | workstream-bootstrap primitive |
| skills | `### Skills` | reviewed on-demand skills | role's `registry.select(query)` |
| lessons | `### Lessons` | prior retros | `recall_lesson_texts(...)` |
| task | `### Task` | per-task | caller |

Reuse, not reinvention: the **skills** layer calls
`runtime.skills.compose_prompt` (so the ADR-0008 review gate still applies — an
unreviewed skill is never injected) and the **lessons** layer calls
`runtime.roles.lessons.compose_lessons`, exactly as the roles did inline.

**Behavior-preserving default.** With no charter/overlay/task, and the same
skills/lessons a role passed before, the output is *identical* to the previous
inline composition (`compose_lessons(compose_prompt(base, skills), lessons)`).
Charter and overlay default to `None`; roles accept them as pass-through
parameters (`run_pm_tick(..., charter=, overlay=)`, `run_executor(...)`,
`verify(...)`), so a vertical opts in via config and nothing changes until it does.

Assembling a prompt never executes anything — every layer is TEXT. Any action
still flows through the policy-gated tool path (`invoke`).

## 2. Pluggable verify-checker registry

The Verifier decides `done` on **evidence it observes itself**, never the
Executor's claim (ADR-0014). *How* that evidence is checked is the vertical's
business: a text task wants "the success marker is present"; a video task wants a
`video_audit` (duration/loudness/captions). The registry makes the check
pluggable **without touching the Verifier**.

### The contract

```python
# runtime/roles/checkers.py
class CheckResult(BaseModel):
    passed: bool
    facts: dict     # concrete evidence the checker OBSERVED (traceability)
    reason: str

# A Checker: gather evidence for the task/artifact and judge `require`.
def check(conn, task, artifact_ref: ArtifactRef, require) -> CheckResult: ...
```

- `ArtifactRef` bundles what a checker needs to gather evidence — the artifact
  `path` plus the **policy-gated read seam** (`ref.read_text(task)` re-reads the
  real artifact as the read-only `verifier` role) and the Executor's `result`
  (available but NOT trusted).
- `CheckerRegistry` maps a criterion `check` name → a `Checker`.
  `default_registry()` pre-registers the horizontal **`marker`** checker (the
  historical marker-in-file gate). `DEFAULT_REGISTRY` is the process-wide default.

### Structured criterion (with back-compat)

The Verifier resolves the task payload to a `(check, require)` pair
(`resolve_criterion`):

- **structured** (preferred): `payload["check"] = {"check": name, "require": ...}`;
- **back-compat**: no `check` field → the `marker` checker on `payload["marker"]`
  (`fallback_marker`). Every existing task keeps verifying exactly as before.

The verdict is decided on the checker's returned **facts** — a false "done" claim
over a non-conforming artifact still FAILS. `verify(...)` still emits
`verify.passed` / `verify.failed` and drives the same transition, so the learning
/ retro / reviewer / telemetry layers keep applying untouched. An unknown `check`
name raises `UnknownChecker` (a clear misconfiguration error, never a silent pass).

### How a vertical plugs in (e.g. `video_audit`)

```python
from runtime.roles.checkers import CheckResult, default_registry

def video_audit(conn, task, ref, require):
    read = ref.read_text(task)                       # observe real evidence
    content = read.result.output if read.result and read.result.ok else ""
    seconds = parse_duration(content)                # facts, not claims
    ok = seconds >= require.get("min_seconds", 0)
    return CheckResult(passed=ok, facts={"duration_seconds": seconds},
                       reason=f"clip is {seconds}s")

registry = default_registry()                        # keeps `marker` for text tasks
registry.register("video_audit", video_audit)

# The PM enqueues a work task whose criterion selects the domain check:
#   payload["check"] = {"check": "video_audit",
#                       "require": {"min_seconds": 30, "captions": True}}
verify(conn, task, result, sink, registry=tools, config=policy, checkers=registry)
```

A runnable example checker + the full dispatch/back-compat/unknown-name/facts
tests live in `runtime/tests/test_role_seams.py`.

## Relationship to the workstream-bootstrap primitive

These two seams are the **prompt-assembly layer** and the **verify-checker
registry** halves of the *Workstream-bootstrap primitive* (state/backlog.md). The
remaining halves — the workstream config/registration record that *supplies* the
charter/overlay/skill-set/checker-set, and the cross-workstream request contract —
are separate follow-ups; these seams are the code they plug into.
