"""
hybrid.py: the tsvector arm of hybrid retrieval, and the RRF fusion that joins it
to the dense arm.

Summary:
- lexical_search() runs a PostgreSQL full-text query over `content`, filtered to the
  documents in force on a given date, and returns langchain Documents shaped exactly
  like the ones the vector store returns.
- currency_sql() is the SQL spelling of that date filter. It is the SQL twin of
  rag.currency_filter() and the single definition of the predicate for every raw-SQL
  caller - see the warning on it.
- rrf_fuse() combines ranked lists from both arms by Reciprocal Rank Fusion.

tsvector vs BM25
-------------------------------
- tsvector matches the token. On this database `555-0199` returns exactly one row and
`$75` exactly two, all correct. So the lexical arm behaves less like a ranker and
more like a near-exact filter, and recall is its whole job.
- `ts_rank` is not BM25: no IDF, no term-frequency saturation, and length
normalisation only if you ask for it. That gap shows when you have to order hundreds
of partial matches. Here the lexical arm returns one or two rows for the queries it
exists to serve, and RRF consumes only its *ordering* and never its scores, so the
gap has nothing to act on. A `pg_search`/ParadeDB/`rum` dependency plus an index
rebuild would buy a difference a 208-chunk corpus cannot demonstrate.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from langchain_classic.docstore.document import Document

from .utils import (
    METADATA_COLUMNS,
    METADATA_JSON_COLUMN,
    PROPERTY_ID,
    TABLE_NAME,
    _connect,
)

# Gate for the whole hybrid path. Default on; set false to fall back to dense-only.
HYBRID_RETRIEVAL = os.getenv("HYBRID_RETRIEVAL", "true").lower() == "true"

# RRF constant 60 is the default value from the original paper:
# Large enough that the top few ranks of an arm are worth similar amounts, so one arm 
# cannot dominate fusion by being confident.
RRF_K = int(os.getenv("RRF_K", "60"))

# Per-arm weights. Equal by default.
HYBRID_DENSE_WEIGHT = float(os.getenv("HYBRID_DENSE_WEIGHT", "1.0"))
HYBRID_LEXICAL_WEIGHT = float(os.getenv("HYBRID_LEXICAL_WEIGHT", "1.0"))

FTS_CONFIG = "english"
FTS_INDEX_NAME = "idx_emb_fts"

# Define expression index.
FTS_EXPR = f"to_tsvector('{FTS_CONFIG}', content)"

FTS_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {FTS_INDEX_NAME} "
    f"ON {TABLE_NAME} USING GIN ({FTS_EXPR})"
)

FTS_QUERY = f"websearch_to_tsquery('{FTS_CONFIG}', ${{n}})"


def currency_sql(
    as_of: date,
    property_id: str = PROPERTY_ID,
    category: str | None = None,
    doc_type: str | None = None,
) -> tuple[str, list[Any]]:
    """The in-force predicate as SQL, with asyncpg params numbered from $1.

    THIS IS THE SAME RULE AS rag.currency_filter() AND MUST STAY THAT WAY.

    The corpus deliberately keeps superseded documents - payment_policy v1 and v2
    sit alongside the v3 in force, and their text differs in exactly the numbers
    residents ask about. Any retrieval arm that reaches this table without this
    predicate can answer a late-fee question with `$50` from the 2024 policy, and
    the answer will look fine: fluent, plausible, correctly cited to a real
    document. That is the failure this whole system exists to prevent.

    The dense arm gets the rule through currency_filter(), a langchain filter dict.
    This arm is raw SQL and cannot reuse that dict, so the rule is written twice, in
    two languages. scripts/verify_currency.py and scripts/verify_hybrid_currency.py
    are what keep the two honest; if you change one, run both.

    A document is in force when effective_from <= as_of <= effective_to_eff. Not
    is_latest_version, not is_superseded - those are structural and disagree with
    currency whenever a document is signed before it takes effect.
    effective_to_eff carries a sentinel for open-ended documents (it reads back as
    `infinity`, which compares greater than every date), so the range test needs no
    NULL branch.

    Params are numbered from $1 so callers append their own after; see
    lexical_search().
    """
    params: list[Any] = [property_id, as_of]
    clauses = [
        "property_id = $1",
        "effective_from <= $2",
        "effective_to_eff >= $2",
    ]
    if category:
        params.append(category)
        clauses.append(f"category = ${len(params)}")
    if doc_type:
        params.append(doc_type)
        clauses.append(f"doc_type = ${len(params)}")
    return " AND ".join(clauses), params


def _to_document(row) -> Document:
    """Rebuild the Document the vector store would have returned for this row.

    Shape parity matters more than it looks. These Documents flow straight into
    DiverseRetriever (reads metadata["doc_id"]), CohereRerank, the DOCUMENT_PROMPT
    template (citation, section_ref, template_note - a missing key raises at format
    time) and the source list in the API payload. The vector store builds its
    metadata as the langchain_metadata JSON overlaid with the promoted columns, and
    sets Document.id from langchain_id; this does the same, in the same order.

    Raw asyncpg hands back jsonb as a str rather than a dict - the vector store gets
    a dict because SQLAlchemy decodes it - so the JSON column is parsed here.
    """
    raw = row[METADATA_JSON_COLUMN]
    metadata: dict[str, Any] = json.loads(raw) if raw else {}
    for col in METADATA_COLUMNS:
        metadata[col] = row[col]
    return Document(
        page_content=row["content"],
        metadata=metadata,
        id=str(row["langchain_id"]),
    )


async def lexical_search(
    query: str,
    as_of: date,
    *,
    limit: int,
    property_id: str = PROPERTY_ID,
    category: str | None = None,
    doc_type: str | None = None,
) -> list[Document]:
    """Full-text search over chunk content, restricted to documents in force.

    Returns at most `limit` Documents, best-ranked first, or an empty list.

    The empty list is a normal outcome, not an error path. A conversational question
    is mostly stop words: websearch_to_tsquery('english', 'can I & should I? rent!')
    reduces to the single lexeme 'rent', and a question made entirely of stop words
    reduces to nothing at all. An empty tsquery matches no row, so this returns [] and
    fusion degrades to dense-only on its own - which is the right behaviour, because a
    question with no distinctive terms is precisely the kind the dense arm handles.

    Be careful what you assume survives tokenisation. Measured on this database,
    `$75` lexes to '75' (the `$` is dropped, so `$75` and `75` are the same query) and
    `(562) 555-0199` lexes to '562', '555', '-0199'. Matching works, but it matches
    fragments; do not write tests that expect the punctuation to be preserved.

    ts_rank orders the result, but only the ordering leaves this function - RRF
    discards the scores, which is the point, since ts_rank values and cosine
    distances are not on comparable scales. langchain_id breaks ties so the ordering
    is stable: ts_rank has no IDF and ties are common on a corpus this small, and an
    unstable rank would make fusion non-deterministic.
    """
    predicate, params = currency_sql(
        as_of, property_id=property_id, category=category, doc_type=doc_type
    )
    tsquery = FTS_QUERY.format(n=len(params) + 1)
    params.append(query)
    params.append(limit)

    columns = ", ".join(
        f'"{c}"' for c in ["langchain_id", "content", METADATA_JSON_COLUMN, *METADATA_COLUMNS]
    )
    sql = f"""
        SELECT {columns}
        FROM {TABLE_NAME}
        WHERE {predicate}
          AND {FTS_EXPR} @@ {tsquery}
        ORDER BY ts_rank({FTS_EXPR}, {tsquery}) DESC, langchain_id
        LIMIT ${len(params)}
    """

    conn = await _connect()
    try:
        rows = await conn.fetch(sql, *params)
    finally:
        await conn.close()
    return [_to_document(r) for r in rows]


def rrf_fuse(
    arms: list[tuple[list[Document], float]],
    rrf_k: int = RRF_K,
) -> list[Document]:
    """Reciprocal Rank Fusion: score(d) = sum over arms of weight / (rrf_k + rank).

    Takes (ranked documents, weight) per arm and returns one list, best first.
    Ranks are 1-based within each arm and a document missing from an arm simply
    contributes nothing from it - there is no penalty term and no need to pad the
    shorter list.

    Fusing ranks rather than scores is the whole reason this works: a cosine
    distance and a ts_rank value are not on a common scale and no amount of
    normalisation makes them comparable, but "third-best in its arm" means the same
    thing in both.

    Deduplication is on langchain_id, exposed as Document.id. The two arms read the
    same row through different drivers and build different Python objects for it, so
    identity and equality both fail; only the primary key is reliable.

    On weights: both default to 1.0 and have not been tuned. The arms are unevenly
    informative per query - the lexical arm contributes almost nothing to a long
    conversational question and a great deal to a short exact one - so a fixed
    weight is arguably the wrong shape, and down-weighting the lexical arm when its
    parsed tsquery has few lexemes is the obvious refinement. It is deliberately not
    implemented: measured retrieval recall is already at ceiling, so there is nothing here
    that could show the refinement helping. Build the eval set, then tune.
    """
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for ranked, weight in arms:
        for rank, doc in enumerate(ranked, start=1):
            key = doc.id
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank)
            docs.setdefault(key, doc)
    # Sort on the key as well as the score so equal scores order deterministically.
    return [docs[key] for key in sorted(scores, key=lambda key: (-scores[key], key))]


def hybrid_config() -> dict[str, Any]:
    """The hybrid settings, for /health."""
    return {
        "enabled": HYBRID_RETRIEVAL,
        "rrf_k": RRF_K,
        "dense_weight": HYBRID_DENSE_WEIGHT,
        "lexical_weight": HYBRID_LEXICAL_WEIGHT,
        "fts_config": FTS_CONFIG,
        "fts_index": FTS_INDEX_NAME,
    }
