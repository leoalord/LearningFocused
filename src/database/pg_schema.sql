-- LearningFocused knowledge store: one Postgres for keyword, vector and graph search.
-- Idempotent: safe to run on every deploy (`python -m src.database.pg_store migrate`).
-- Embeddings are OpenAI text-embedding-3-small (1536 dims), the same model Chroma uses.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;

-- Every feed the daily job checks (podcast RSS, Substack, YouTube channels, social accounts).
CREATE TABLE IF NOT EXISTS sources (
    key             text PRIMARY KEY,
    kind            text NOT NULL,
    name            text NOT NULL,
    url             text,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    cadence         text NOT NULL DEFAULT 'daily' CHECK (cadence IN ('daily', 'weekly', 'manual')),
    enabled         boolean NOT NULL DEFAULT true,
    last_checked_at timestamptz,
    last_ok_at      timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per episode, article or video. Derived from chunk metadata on sync.
CREATE TABLE IF NOT EXISTS documents (
    doc_id       text PRIMARY KEY,
    source_type  text,
    title        text,
    url          text,
    published_at timestamptz,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- One row per retrievable chunk (same ids and `type` values as the Chroma collection).
-- Writers upsert the document row first; deleting a document drops its chunks.
CREATE TABLE IF NOT EXISTS chunks (
    id         text PRIMARY KEY,
    doc_id     text REFERENCES documents (doc_id) ON DELETE CASCADE,
    type       text NOT NULL,
    content    text NOT NULL,
    metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding  vector(1536),
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_type_idx ON chunks (type);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_metadata_idx ON chunks USING gin (metadata jsonb_path_ops);

-- Daily job runs and per-source results (replaces reading ingest_ledger/*.json by hand).
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id      text PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    ok          boolean,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- source_key is deliberately not a foreign key: check history outlives a source that
-- is later removed from src/feeds/sources.json.
CREATE TABLE IF NOT EXISTS source_checks (
    run_id     text NOT NULL REFERENCES ingest_runs (run_id) ON DELETE CASCADE,
    source_key text NOT NULL,
    ok         boolean,
    skipped    boolean NOT NULL DEFAULT false,
    error      text,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source_key)
);

-- Graph extraction bookkeeping (src/pipeline/index_graph.py), one row per source passage
-- version: a passage is re-extracted only when its text (content_hash) changes. Stats are
-- what the model proposed vs what passed the schema filter (src/pipeline/graph_quality.py).
CREATE TABLE IF NOT EXISTS ingested_docs (
    source_key                text NOT NULL,
    content_hash              text NOT NULL,
    source_type               text,
    doc_id                    text,
    canonical_url             text,
    title                     text,
    status                    text NOT NULL CHECK (status IN ('started', 'completed', 'failed')),
    graph_doc_id              text,          -- id of the passage's Document vertex in the graph
    first_ingested_at         timestamptz NOT NULL DEFAULT now(),
    last_ingested_at          timestamptz NOT NULL DEFAULT now(),
    last_ingest_run_id        text,
    extract_model             text,
    raw_nodes                 integer,
    kept_nodes                integer,
    raw_rels                  integer,
    kept_rels                 integer,
    rels_dropped_for_endpoint integer,
    dropped_node_types        jsonb,         -- {"Place": 3}: off-vocabulary types the model proposed
    dropped_rel_types         jsonb,
    note                      text,
    error                     text,
    PRIMARY KEY (source_key, content_hash)
);

CREATE INDEX IF NOT EXISTS ingested_docs_status_idx ON ingested_docs (status);

-- Keep updated_at honest on every UPDATE (inserts use the column default).
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER sources_updated_at BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE OR REPLACE TRIGGER documents_updated_at BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE OR REPLACE TRIGGER chunks_updated_at BEFORE UPDATE ON chunks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
