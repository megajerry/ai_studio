# Retro: Spokesman converse shipped as a classifier, not an agent

- **Date:** 2026-07-27
- **Scope:** `spokesman/converse.py` first implementation (ADR-0026)
- **Trigger:** Stakeholder feedback — chat could not carry a normal conversation

## Facts

- ADR-0026 required Spokesman to be the full human↔studio interface: answer
  naturally, route requirements via the task queue, tools for studio actions.
- What shipped: regex/heuristic **intent classification**, then for `answer` a
  **status brief pasted under `_Re: <user text>`**. No model-authored reply.
- Keyword tools (`status` / approve / …) and queue side effects worked; dialogue
  did not.
- Fix (same branch): model-first agent loop via `call_model`;
  `studio_status` / `enqueue_goal` / `request_prep` / `propose_handoff` /
  `end_handoff` are tools; dry-run returns parseable agent JSON (like PM
  `plan_goal`).

## Root cause (not the symptom)

1. **Inverted control.** Treated “conversation” as *route then template*,
   because SMS shortcuts and queue wiring were familiar and easy to test.
   The model’s job (interpret the prompt) was demoted to an optional classifier
   that usually fell through to regex.
2. **Shipped the architecture diagram, not the product.** ADR boxes
   (answer / enqueue / prep / handoff) became an enum + switch, not an agent
   with tools. Status dump looked like “grounded answering” without being one.
3. **Keyless convenience bias.** Avoiding a real `call_model` reply path made
   dry-run “green” while teaching the wrong default behavior.
4. **Did not dogfood.** No realistic “hi / what’s up / please build X” session
   before calling it done — unit tests asserted routing, not conversation.

## Lessons (imperative)

1. **Human-facing roles are agents first; tools second.** Never replace prompt
   interpretation with a classifier + canned paste.
2. **If the interface is chat, the acceptance test is a normal conversation** —
   greetings and open questions must get model replies, not status wallpaper.
3. **Dry-run must exercise the real control flow** (agent JSON + tools), not a
   parallel “status under `_Re:`” path that only exists in production’s failure
   mode.
4. **Prefer enqueueing studio work to the task queue** when building features
   inside the studio; when implementing in-session anyway, still build the
   product shape (agent + tools), not a scaffold that “looks wired.”

## Keep / stop

- **Keep:** queue-only PM coordination; keyword fast path for approve/deny/decide;
  grounding for outbound claims; prep cache as latency aid.
- **Stop:** regex-first “conversation”; answering by echoing status under the
  user’s words; calling a feature done when only the side-effect paths are tested.
