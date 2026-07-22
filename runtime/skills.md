# `runtime/skills/` — the skills layer (Agent Skills open standard, ADR-0008)

A role is `prompt + skills + tools` (architecture §3). This package is the
**skills** part: reusable capability packages, loaded **on demand** and injected
into a role's prompt only when relevant. It is pure Python — no DB, no network,
no keys.

> **Skills are instructions, not actions.** A skill contributes TEXT to a
> prompt. Loading or injecting a skill NEVER runs anything. Any side effect still
> goes through the policy-gated tool path (`runtime.enforce.invoke`) — a skill
> can *tell* a role to use a tool, but the capability gate + approval tiers
> (🟢/🟡/🔴) still decide whether it runs. **Skills do not bypass policy.**

## The `SKILL.md` format

A skill is a directory under the skills root containing a `SKILL.md`:

```markdown
---
name: define-success-criteria          # required, unique
description: One-line what/why.         # required
triggers: [pm, plan, success criteria]  # keywords/roles/task-types for selection
when_to_use: When the PM plans a task.  # human-readable relevance (optional)
reviewed: true                          # review gate — see below
source: in-repo                         # provenance
---

# Markdown body = the instructions injected into the prompt.
Step-by-step guidance for the role...
```

- **Frontmatter** is a `---`-fenced YAML mapping at the very top. `name` and
  `description` are required (open standard). `triggers`/`when_to_use` drive
  selection; `reviewed`/`source` drive the review gate.
- **Body** (everything after the closing `---`) is the instruction text.
- **Resources** (optional `resources: [...]` + sibling files) are scripts or
  templates referenced by relative path. They are **never auto-executed** by
  loading a skill.
- Malformed frontmatter (no/ broken fence, non-mapping YAML, invalid YAML,
  missing required field) raises a clear, path-qualified `SkillError` — the
  loader/registry **log and skip**, never crash.

## Files

| File | Purpose |
| --- | --- |
| `models.py` | `Skill` (pydantic model) + `SkillError`; `Skill.matches(query)` relevance. |
| `loader.py` | `parse_skill(text)` / `load_skill(path)` — split frontmatter + body, validate. |
| `registry.py` | `SkillRegistry.discover(root)` (walk `SKILL.md`), `.select(query, limit)`. |
| `inject.py` | `compose_prompt(base, skills)` / `compose(...)` — review gate + bounded section. |

## The select → review → inject flow

```python
from runtime.skills import SkillRegistry, compose_prompt

reg = SkillRegistry.discover()              # parse every SKILL.md under skills/
relevant = reg.select("pm plan", limit=3)   # ONLY relevant skills, capped
prompt = compose_prompt(base_prompt, relevant)   # inject REVIEWED ones only
```

1. **Discover** — parse all `SKILL.md` under the root (default repo `skills/`,
   overridable via `$AI_STUDIO_SKILLS_DIR` or `discover(root=...)`); index by name.
2. **Select** — `select(query, limit)` returns only skills whose
   name/triggers/description match the query (a role, task_type, or free text),
   ranked and **capped** at `limit` (default 3). This is the ADR-0013 context
   discipline: load only what's relevant, never everything.
3. **Review gate + inject** — `compose_prompt` injects **only `reviewed: true`**
   skills into a bounded, delimited `### Skills` section. An unreviewed skill is
   **skipped and logged** by default; including one requires an explicit
   `allow_unreviewed=True` (and is logged as a warning). "Treat skills like code
   — review before use" (ADR-0008).

## How roles use it

`runtime.roles.pm.run_pm_tick(..., skills=<SkillRegistry>)` composes its
confidence-gate prompt with the relevant reviewed skill(s) (e.g.
`define-success-criteria`). With no registry the role uses its inline base prompt
(behavior-preserving). The `worker.run()` loop and `runtime.demo` discover the
registry once at startup and thread it to the PM, so on-demand skill injection
runs in the live loop. Selection and the review gate are covered in
`runtime/tests/test_skills.py`.

## Adding / curating / reviewing a skill

See `skills/README.md` for the authoring + review checklist. In short: add a
`SKILL.md` under `skills/<name>/`, keep `reviewed: false` until a human/Reviewer
has audited it (supply-chain risk), then flip to `true` with accurate `source`
provenance. Import from audited libraries only.
