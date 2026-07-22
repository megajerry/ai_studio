# `skills/` — the studio's Agent Skills library

Reusable capability packages for roles, in the **Agent Skills open standard**
(ADR-0008). Each skill is a directory with a `SKILL.md`; the runtime loads them
on demand and injects only the relevant, reviewed ones into a role's prompt. See
[`runtime/skills.md`](../runtime/skills.md) for the loader/registry/injection API.

## What ships here

| Skill | Role | What it does |
| --- | --- | --- |
| `define-success-criteria/` | PM | Confidence gate: fix ONE checkable success criterion + marker before executing. |
| `retrospective/` | Retro | Distill a finished/failed task into durable, scoped lessons. |
| `code-review/` | Reviewer | Independent correctness/safety/least-privilege review before a change lands. |

All three are `reviewed: true`, `source: in-repo`.

## Golden rules

1. **Skills are instructions, not actions.** A `SKILL.md` body is text injected
   into a prompt. Loading a skill never runs anything. Any side effect still
   goes through the policy-gated tool layer (`runtime.enforce.invoke`) with its
   capability gate + 🟢/🟡/🔴 approval tiers. A skill's `resources`/scripts are
   **never auto-executed**.
2. **Review before use (treat skills like code).** Only `reviewed: true` skills
   are injected. Unreviewed skills are skipped (logged) unless a caller passes
   `allow_unreviewed=True`. Supply-chain risk is real — prefer audited sources.
3. **Relevance + context discipline.** Skills load on demand; the registry
   selects only skills matching the task and caps the count (ADR-0013). Write
   tight `triggers` so a skill is found when — and only when — it's relevant.

## Authoring a new skill

1. Create `skills/<skill-name>/SKILL.md` with `---`-fenced YAML frontmatter:

   ```markdown
   ---
   name: <skill-name>          # required, unique, matches the directory
   description: <one line>      # required
   triggers: [<keywords>, <roles>, <task-types>]
   when_to_use: <human-readable relevance>
   reviewed: false              # until audited — see the review step
   source: <in-repo | library-name | url>
   ---

   # <Title>
   Step-by-step instructions for the role...
   ```

2. Put any helper scripts/templates as sibling files and list them under
   `resources:`. Remember they are reference material — nothing runs them
   automatically.
3. Keep the body imperative, generalizable, and terse (ADR-0013): guidance a
   role applies, not a retelling of one task.

## Reviewing a skill (the gate)

Before flipping `reviewed: true`:

- **Inspect every line** of the body and any resource, especially for imported
  skills — treat it like a code review (see the `code-review` skill).
- Confirm it asks for no more capability than the task needs, and that any
  destructive/costly step defers to the 🔴 approval path rather than implying
  auto-execution.
- Set an accurate `source` (provenance). Prefer audited libraries
  (`anthropics/skills`, etc.); record where an imported skill came from.
- Only then set `reviewed: true`. An unreviewed skill is safe to keep in the
  library — it simply won't be injected until reviewed.
