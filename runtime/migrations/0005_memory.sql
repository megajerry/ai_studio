-- 0005 — memory: the four-layer memory subsystem (architecture §7, ADR-0005).
--
-- Agents never read all of memory; they read within their SCOPE. Memory is
-- organized into four layers — episode → project → knowledge → long-term — and a
-- recall is always addressed to exactly one layer within a scope, so a narrower
-- layer's rows never bleed into a broader query and one workstream/project can't
-- read another's (the isolation rule; see runtime/memory.md).
--
-- Backing store: Postgres for the rows + a plain float array for the embedding,
-- searched by BRUTE-FORCE cosine similarity in Python over the scope-filtered
-- candidates (NO pgvector, NO Qdrant dependency here — Qdrant is a host-only
-- swap-in; see runtime/memory/vector.py). The lessons corpus from the Retro loop
-- lives in the `knowledge` layer (see add_lesson/recall_lessons).
--
-- Forward-only and idempotent (like 0004): CREATE ... IF NOT EXISTS is a no-op on
-- re-run, and the migration runner skips already-applied files anyway.

CREATE TABLE IF NOT EXISTS memory_items (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    layer      text NOT NULL
                 CHECK (layer IN ('episode', 'project', 'knowledge', 'longterm')),
    workstream text NOT NULL,              -- scope root; '*' = global (knowledge/longterm)
    project    text,                       -- set for episode/project layers
    episode    text,                       -- set for the episode layer only
    text       text NOT NULL,              -- the remembered content
    metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding  double precision[],         -- plain float vector; brute-force cosine in Python
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Candidate fetch for a layer within a workstream (episode/project/knowledge/longterm).
CREATE INDEX IF NOT EXISTS memory_items_layer_ws_idx ON memory_items (layer, workstream);
-- Candidate fetch scoped to a project (episode/project layers).
CREATE INDEX IF NOT EXISTS memory_items_ws_project_idx ON memory_items (workstream, project);
