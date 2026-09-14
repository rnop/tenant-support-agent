"""
utils.py: shared vector store setup used by ingest.py (writes) and rag.py (reads)

Summary:
- PGEngine (connection pool) is created at import time and shared by the whole app.
- OpenAI embedding model (`text-embedding-3-small`) is used for both ingestion and
  retrieval, so the two must never diverge.
- get_vector_store() connects to the table created by init-db/init.sql.
- The promoted metadata columns are read from data/manifest.json rather than
  hardcoded, so there is one definition of the filter schema (scripts/build_manifest.py)
  instead of three that can drift apart.
- verify_schema() checks the live table against that list, which catches the most
  common footgun: editing init.sql without dropping the volume, so the database
  silently keeps the old columns.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_openai import OpenAIEmbeddings
from langchain_postgres.v2.engine import PGEngine
from langchain_postgres.v2.async_vectorstore import AsyncPGVectorStore

DATA_DIR = os.getenv("DATA_DIR", "data")
MANIFEST_PATH = Path(DATA_DIR) / "manifest.json"

# The property's local timezone, not the server's. Whether a document is in force is
# decided by date boundaries, so "today" has to mean today at the property: a container
# running UTC is up to 8 hours ahead of Long Beach and would, for part of every
# evening, treat a document that takes effect tomorrow as already governing.
#
# Set per property rather than derived from the manifest's `jurisdiction` string,
# which is a human label ("Long Beach, CA") and not a tz identifier.
PROPERTY_TZ = ZoneInfo(os.getenv("PROPERTY_TZ", "America/Los_Angeles"))

PROPERTY_ID = os.getenv("PROPERTY_ID", "maple-court")


def today() -> date:
    """Current date at the property. Use this instead of date.today()."""
    return datetime.now(PROPERTY_TZ).date()

TABLE_NAME = "langchain_pg_embedding"
METADATA_JSON_COLUMN = "langchain_metadata"

PG_CONN_STR = os.getenv("DATABASE_URL")
if not PG_CONN_STR:
    raise RuntimeError("DATABASE_URL is not set")

PG_ENGINE = PGEngine.from_connection_string(PG_CONN_STR)

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")


def load_manifest() -> dict:
    """The generated manifest. Built by scripts/build_manifest.py."""
    if not MANIFEST_PATH.exists():
        raise RuntimeError(
            f"{MANIFEST_PATH} not found - run: python scripts/build_manifest.py"
        )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def metadata_columns() -> list[str]:
    """Metadata fields promoted to real SQL columns, per the manifest."""
    return list(load_manifest()["filter_columns"])


# Read once at import: the manifest does not change while the app is running, and
# every vector-store handle needs the same column list.
METADATA_COLUMNS = metadata_columns()


async def get_vector_store() -> AsyncPGVectorStore:
    return await AsyncPGVectorStore.create(
        engine=PG_ENGINE,
        embedding_service=embeddings,
        table_name=TABLE_NAME,
        metadata_json_column=METADATA_JSON_COLUMN,
        metadata_columns=METADATA_COLUMNS,
    )


async def _connect():
    """A plain asyncpg connection, outside PGEngine.

    Used for schema inspection and maintenance so those still work when the vector
    store itself cannot be constructed. asyncpg wants a bare postgres URL;
    DATABASE_URL carries SQLAlchemy's driver suffix for PGEngine's benefit.
    """
    import asyncpg

    return await asyncpg.connect(PG_CONN_STR.replace("+asyncpg", ""))


async def reset_property(property_id: str, index_name: str = "hnsw_idx") -> int:
    """Clear one property's chunks and drop the vector index. Returns rows deleted.

    Ingest has to be re-runnable: documents are added to the corpus over time and
    ingest is run again. The vector store only ever INSERTS, so without this a
    second run would silently duplicate every chunk - the store would answer from
    two copies of each document and nothing would look wrong.

    The index is dropped so it can be rebuilt over the finished data, which gives a
    better HNSW graph than leaving one that was built incrementally.

    The GIN full-text index added for the lexical arm is deliberately NOT dropped.
    The reason HNSW is rebuilt does not carry over: a GIN index is a plain inverted
    index and its contents do not depend on insertion order, so a drop and rebuild
    would produce a byte-identical index for the cost of rebuilding it.
    ingest._create_index() issues it as CREATE INDEX IF NOT EXISTS, which keeps
    ingest re-runnable either way.
    """
    conn = await _connect()
    try:
        result = await conn.execute(
            f"DELETE FROM {TABLE_NAME} WHERE property_id = $1", property_id
        )
        await conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        return int(result.split()[-1])
    finally:
        await conn.close()


async def verify_schema() -> dict:
    """Compare the live table's columns against what the manifest expects."""
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY ordinal_position
            """,
            TABLE_NAME,
        )
        present = {r["column_name"]: r["data_type"] for r in rows}

        if not present:
            return {
                "ok": False,
                "error": (
                    f"table {TABLE_NAME} does not exist - the init script did not run. "
                    "The pgvector image only runs init-db/ against an empty data "
                    "directory: docker compose down -v && docker compose up -d postgres"
                ),
            }

        expected = set(METADATA_COLUMNS)
        missing = sorted(expected - present.keys())
        extra = sorted(
            present.keys()
            - expected
            - {"langchain_id", "content", "embedding", METADATA_JSON_COLUMN}
        )

        row_count = await conn.fetchval(f"SELECT count(*) FROM {TABLE_NAME}")

        return {
            "ok": not missing,
            "table": TABLE_NAME,
            "rows": row_count,
            "missing_columns": missing,
            "unexpected_columns": extra,
            "error": (
                f"columns missing from {TABLE_NAME}: {missing}. init.sql is out of "
                "step with the manifest, or the volume predates the current schema."
            )
            if missing
            else None,
        }
    finally:
        await conn.close()
