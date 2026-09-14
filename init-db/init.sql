-- Tenant RAG schema — Maple Court
--
-- The langchain_pg_embedding table, with the nine filter columns from
-- data/manifest.json promoted out of the JSONB blob so retrieval can narrow in
-- SQL before the vector search runs. This corpus needs to filter on property,
-- doc type and — above all — the date a document was in force.
--
-- The column list is the FILTER_COLUMNS list in scripts/build_manifest.py.
-- Keep the two in step.
--
-- IMPORTANT: the pgvector image only runs docker-entrypoint-initdb.d against an
-- EMPTY data directory. Editing this file does nothing to a database that already
-- exists; you have to drop the volume:
--
--     docker compose down -v && docker compose up -d postgres

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS langchain_pg_embedding (
    langchain_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content            text,
    embedding          vector(1536),   -- text-embedding-3-small
    langchain_metadata jsonb,

    -- --- promoted filter columns ---
    category           text,           -- coarse: 6 values
    doc_id             text,
    property_id        text,
    doc_type           text,           -- fine: 17 values
    version            integer,
    effective_from     date,
    effective_to_eff   date,           -- 9999-12-31 sentinel when open-ended
    is_template        boolean,
    disposition_hint   text            -- 'answer' | 'escalate'
);

-- Ingest contract for the two date columns
-- -----------------------------------------------------------------------------
-- These are real DATE columns, so ingest must pass datetime.date objects
-- (date.fromisoformat on the manifest's ISO strings). asyncpg does not coerce a
-- str into a date and will raise on insert if handed one.
--
-- The same two fields stay as ISO strings inside langchain_metadata, which has to
-- remain JSON-serialisable — a date object there will fail to serialise.
--
-- If the vector store turns out to stringify metadata column values before the
-- insert, the fallback is to make both columns `text`: ISO-8601 sorts
-- lexicographically in the same order it sorts chronologically, so every range
-- comparison below keeps working unchanged, including the 9999-12-31 sentinel.

-- Currency is the filter on nearly every query:
--   effective_from <= :as_of AND effective_to_eff >= :as_of
CREATE INDEX IF NOT EXISTS idx_emb_effective
    ON langchain_pg_embedding (effective_from, effective_to_eff);

-- Scoping a question to one property, and optionally one kind of document.
CREATE INDEX IF NOT EXISTS idx_emb_scope
    ON langchain_pg_embedding (property_id, doc_type);

CREATE INDEX IF NOT EXISTS idx_emb_category
    ON langchain_pg_embedding (category);

-- The lexical arm of hybrid retrieval (app/hybrid.py).
--
-- An expression index rather than a generated tsvector column: a generated column
-- would appear in information_schema and utils.verify_schema() would report it as
-- an unexpected column. The cost of the expression form is that a query only uses
-- the index if it repeats the expression exactly, which is why app/hybrid.py
-- builds both this DDL and the query from one FTS_EXPR constant.
--
-- This statement is duplicated in ingest._create_index(), deliberately. This file
-- only runs against an empty data directory, so an existing database never sees it;
-- creating the index during ingest is what applies it without `docker compose down -v`.
-- Both are IF NOT EXISTS, so whichever runs second is a no-op.
CREATE INDEX IF NOT EXISTS idx_emb_fts
    ON langchain_pg_embedding USING GIN (to_tsvector('english', content));

-- The HNSW vector index is NOT created here. ingest.py builds it with
-- aapply_vector_index() after the embeddings are loaded, which is the right
-- order: building an HNSW index on an empty table and then filling it is slower
-- and gives a worse graph than building it over the finished data.
