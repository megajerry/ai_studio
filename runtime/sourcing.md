# Model Sourcing agent (ADR-0005)

The **Sourcing agent** keeps the model registry current. Model options reprice and
reshuffle roughly monthly, so picking and pricing models is a *continuous* job. The
Sourcing agent researches credible sources (LMArena, provider pricing/docs) and
proposes registry updates **through the normal PR + review loop** — so model choices
stay traceable and human-approvable and never drift silently.

It is the runtime analogue of opening a PR: it does **not** touch GitHub (out of
process). It produces a **reviewable candidate** artifact plus the ADR-0005
**approval envelope**, and it **never mutates the live registry** directly.

- Code: [`runtime/roles/sourcing.py`](roles/sourcing.py) — `run_sourcing(conn, task, sink, …)`.
- Dispatch: [`runtime/worker.py`](worker.py) — a `research.models` / `sourcing`
  task routes to `run_sourcing` (no loop; enqueues nothing).
- Registry it proposes against: [`runtime/model/registry.py`](model/registry.py)
  (`ModelSpec` / `Registry`).

## The flow: research → propose → (🛑 | auto + 📣)

```
task(research.models | sourcing)
  → resolve candidates (payload `candidates`/`models`; absent → refresh current registry)
  → for each candidate: search(role="sourcing", …)      # policy-gated cached gateway, net.fetch
        → synthesize a ModelSpec whose PROVENANCE is grounded in the search URLs
        → classify against the CURRENT registry (the approval envelope)
  → call_model(dry-run)                                  # traceability only; does NOT decide
  → write proposals/models.candidate.yaml                # policy-gated fs.write; the reviewable candidate
  → envelope:
       any new-provider / new-tier / budget-increasing → 🛑 request_approval("adopt model registry update")
       else all in-band                                  → auto-adopt + 📣 sourcing.autoadopted
  → emit sourcing.proposed (ids / counts / provenance-hash / decision)
```

Every hop goes through a sanctioned seam — never agent-direct (CLAUDE.md invariants
1-3):

- **Search only via the gateway** — `search(conn, role="sourcing", …)`
  ([`runtime/search/gateway.py`](search/gateway.py)): policy-gated on `net.fetch`,
  cached, keyless dry-run. A role lacking `net.fetch` is **denied** (`search.denied`,
  raises `SearchDenied`; nothing fetched, nothing cached).
- **Model call only via `call_model`** — a routed/costed/logged dry-run synthesis
  step. Its text is logged for traceability but does **not** decide the proposal;
  the candidate specs + the envelope decision are derived **deterministically**, so
  the loop is reproducible keyless.
- **File write only via the policy-gated tool layer** —
  `invoke(role="sourcing", tool_name="filesystem", op="write", …)` writes the
  candidate to a review path under the confined tool root (git-ignored). A role
  without `fs.write` is **denied** (nothing written); the classification still runs.

## Evidence over claims (ADR-0014)

A candidate's `provenance` is derived from the **search results the gateway actually
returned** (their URLs) plus the sourcing date — never copied from a bare
`provenance` claim in the payload. The proposed prices come from the candidate list,
but the provenance that makes them reviewable is grounded in what was gathered. A
candidate with no corroborating source is marked `(no corroborating source)` in the
proposal.

## The approval envelope (ADR-0005 / ADR-0006)

Each candidate is classified against the **current** registry; the whole proposal
takes the stricter path if **any** candidate needs approval:

| Situation | Decision |
| --- | --- |
| New provider (not in the registry) | 🛑 approval |
| New tier (objective/scope-affecting) or `scope_affecting: true` in the payload | 🛑 approval |
| Budget-increasing (higher input **or** output price than the tier's current reference) | 🛑 approval |
| In-band swap (known provider, same tier, price ≤ the current reference) | 📣 auto-adopt |

- 🛑 is a **real** `runtime.approvals.request_approval` row ("adopt model registry
  update", tier `🛑`) — the proposal awaits a human, exactly like a PR awaiting
  review. Nothing is adopted until a human resolves it.
- 📣 emits `sourcing.autoadopted` — the swap stays within the approved cost/quality
  band, so it self-adopts and merely informs (ADR-0006).

## Events (leak nothing)

`sourcing.*` events carry **model ids, counts, a provenance hash, and the decision**
— never a secret/API key, and never the raw provenance URLs (only their hash). The
raw URLs live only inside the reviewable candidate file, not in the event log.

- `sourcing.proposed` — `model_ids`, `candidate_count`, `provenance_hash`,
  `decision`, `new_provider_count`, `budget_increasing_count`, `candidate_written`,
  `autoadopted`.
- `sourcing.autoadopted` (📣) — `model_ids`, `candidate_count`, `provenance_hash`,
  `decision`.
- 🛑 approvals flow through `approval.requested` (owned by `runtime.approvals`).
- `search.*` events (dims/latency/provider only) come from the gateway.

## The candidate artifact

The proposal is written to `proposals/models.candidate.yaml` (under the confined
tool root; git-ignored). It is a `models:` block of the proposed `ModelSpec`s (each
tagged with its per-spec `_decision`) plus a header noting the date, proposal
decision, and provenance hash. **This is not the live registry** — adopting a
candidate into `runtime/models.yaml` is a separate, reviewed step. The candidate
name is refused if it collides with a live registry filename (belt-and-suspenders).

## Policy grant (least privilege)

`policy.example.yaml` grants the `sourcing` role exactly `fs.read`, `fs.write`,
`net.fetch`. It has no `spend.money`/`deploy`/`shell` — the 🛑 approval it *raises*
is a request for a human, not a capability it holds.

## Invariants

- **No loop.** A sourcing task enqueues nothing (the worker threads no `enqueue`
  seam) — it produces the candidate + the decision and stops.
- **Never mutates the live registry.** It only writes the candidate proposal.
- **Keyless.** Runs fully in dry-run (search + model), no API key required.

## Tests

[`runtime/tests/test_sourcing.py`](tests/test_sourcing.py): pure-logic (synthesis
grounded in evidence, the envelope classification, YAML rendering), worker-wiring
(dispatch + no loop for both task types), and live-DB (gateway-gated research,
`net.fetch` denial, in-band auto-adopt + 📣, new-provider **and** budget-increase 🛑
approval, candidate write denied without `fs.write`, events leak no secret/URL,
live registry never mutated, no loop).
