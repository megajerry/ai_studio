# `runtime/memory/` — four-layer memory (M4)

The memory subsystem from architecture §7. Agents **never read all of memory**;
they read within their **scope**. Memory is organized into four layers and a
recall is always addressed to exactly one layer within a scope, so a narrower
layer's rows never bleed into a broader query and one workstream/project can't
read another's.

Runs **fully keyless**: dry-run (deterministic) embeddings + a Postgres
brute-force cosine vector store. No pgvector, no Qdrant, no API key needed here.

## Layers

```
Episode → Project → Knowledge → Long-term
```

| Layer | What it holds | Scope key |
| --- | --- | --- |
| `episode` | scratch memory for one run/session | `(workstream, project, episode)` |
| `project` | memory shared across a project's episodes | `(workstream, project)` |
| `knowledge` | durable know-how — incl. the **Retro lessons corpus** | `workstream` (+ global `*`) |
| `longterm` | studio-wide facts | global |

## Scope & visibility rule (enforced on `recall`)

A recall targets **exactly one layer** and returns only items of that layer that
match the layer's scope columns:

- **episode** → needs `workstream` + `project` + `episode`; sees only that episode.
- **project** → needs `workstream` + `project`; sees only that project's
  `project`-layer items (NOT its episodes' memory).
- **knowledge** → needs `workstream`; sees that workstream's knowledge, plus the
  global corpus (`workstream = '*'`) when `include_global_knowledge=True`.
- **longterm** → global (visible to every workstream).

Consequences: workstream A can't recall workstream B's project memory; an
episode's memory never surfaces in a broader project/knowledge query; global
knowledge/long-term is shared deliberately, never by accident.

The rule has one definition in two mirror forms in `models.py`:
`scope_where(layer, scope, ...)` builds the SQL `WHERE` for the candidate fetch,
and `in_scope(item, layer, scope, ...)` is the equivalent pure predicate
(unit-tested with no DB, and applied again in Python as defense in depth). On
**write**, `remember` validates + canonicalizes the scope per layer (e.g. a
`project` write blanks `episode`) so every stored row matches exactly one recall
shape.

## Embeddings (`embed.py`)

`embed(text) -> list[float]` routes through the model registry's **embedding
tier** (ADR-0005; Anthropic has no embedding model, so the tier points at
google/openai/voyage). It mirrors the M3b provider pattern:

- **Default = `DryRunEmbeddingProvider`** — keyless, networkless, **deterministic**:
  signed feature-hashing of lower-cased character 3-grams (+ whole words) into a
  fixed `EMBED_DIM=256` vector, then L2-normalized. Identical text → identical
  vector; similar text → higher cosine. Makes offline search reproducible.
- **Real adapters** (`google` / `openai` / `voyage`) are structural: each reads
  its key from env **inside itself** (ADR-0011, invariant 5), imports `httpx`
  lazily, never logs the key, and is **not exercised in tests**. They activate
  automatically when a key is present (and `MODELS_DRY_RUN` is not set).

`embed()` emits nothing — the memory API is the only place that touches the log.

## Vector store (`vector.py`)

- **`PostgresVectorStore` (default, fully runnable here)** — fetches only the
  scope/layer-filtered candidate rows from `memory_items` via SQL, then computes
  **cosine similarity in Python** and returns the top-k. `cosine()` is pure and
  robust (returns 0.0 on a degenerate/mismatched vector rather than raising).
- **`QdrantVectorStore` (structural stub, NOT used in tests)** — documents the
  host swap-in (architecture §8). `qdrant-client` is a lazy import so this module
  never requires the dependency; methods raise `NotImplementedError` until the
  host wires it up. Both backends sit behind the `VectorStore` protocol, so
  swapping Postgres → Qdrant never touches callers.

## API (`api.py`)

```python
from runtime.memory import Scope, MemoryLayer, remember, recall
from runtime.memory import add_lesson, recall_lessons

remember(conn, Scope(workstream="productivity", project="m4"),
         MemoryLayer.PROJECT, "deploy uses blue-green")     # emits memory.remembered
items = recall(conn, Scope(workstream="productivity", project="m4"),
               MemoryLayer.PROJECT, "how do we deploy", k=5) # emits memory.recalled

# Retro lessons corpus (Knowledge layer):
add_lesson(conn, "productivity", "run migrations before tests")
add_lesson(conn, "productivity", "budget the supervisor context", global_lesson=True)
lessons = recall_lessons(conn, "some-workstream", "migrations", k=5)  # incl. global
```

- `remember(conn, scope, layer, text, metadata=None)` — embed → insert; emit
  `memory.remembered` with **layer/scope/id/dims only — never the text or the
  embedding**.
- `recall(conn, scope, layer, query, k=5)` — embed query → scope+layer vector
  search → items; emit `memory.recalled` with the **count only**.
- `add_lesson` / `recall_lessons` — thin Knowledge-layer helpers for the Retro
  loop that grows the lessons corpus and injects it into future work. Lessons are
  workstream-scoped by default; `global_lesson=True` stores under the global
  corpus (`'*'`) so every workstream can recall it (`include_global=True` by
  default on recall).

Events carry counts/ids only (invariants 5 & 6): the log stays replayable without
leaking remembered content or vectors.

## Schema (`migrations/0005_memory.sql`)

`memory_items` — `id`, `layer` (CHECK enum), `workstream`, `project`, `episode`,
`text`, `metadata jsonb`, `embedding double precision[]` (a plain float array —
brute-force search, no pgvector), `created_at`. Indexes on `(layer, workstream)`
and `(workstream, project)`. Forward-only and idempotent (`CREATE ... IF NOT
EXISTS`), like `0004`.

## Tests

`runtime/tests/test_memory.py`: pure-logic tests (embedding determinism +
similar-closer, L2 norm, cosine, scope predicate) run with **no DB**; DB
round-trip / scope-isolation / brute-force-nearest / lessons / migration-idempotent
tests use a live Postgres and **skip cleanly** when none is reachable.

```bash
export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
python -m runtime.migrate           # applies 0005 (idempotent)
python -m pytest runtime/tests/ -q  # all pass; DB tests skip only when no DB
```
