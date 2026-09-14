"""
verify_hybrid_currency.py: prove the lexical arm honours the in-force date filter

verify_currency.py guards the dense arm. It cannot guard the lexical one: that arm
is raw SQL and carries its own copy of the predicate (hybrid.currency_sql), written
separately from the langchain filter dict the dense arm uses (rag.currency_filter).
Two spellings of one rule is two things that can drift.

The failure this catches is quiet. The corpus keeps payment_policy v1 and v2
alongside the v3 in force, and all three say "A late fee of $..." in near-identical
prose. A lexical arm missing the date predicate would happily return the 2024 policy
for a late-fee question, the reranker would rank it well because it is genuinely
on-topic, and the model would answer "$50" with a correct citation to a real
document. Nothing looks wrong.

Four things are checked:

  A. The lexical arm alone, on a term distinctive to a superseded document ($50),
     returns no out-of-force chunk.
  B. Both arms, run broadly enough that relevance is not doing the filtering,
     return doc_id sets containing no out-of-force document.
  C. A stop-word-only question yields an empty tsquery: the lexical arm returns an
     empty list rather than raising, and fusion degrades to dense-only.
  D. The lexical arm actually finds things.

D is not padding. A and B are satisfied by an arm that returns nothing at all, and
an arm broken that way would leave the pipeline silently dense-only while every
currency check stayed green.

Run inside the app container, after ingest:
    docker compose exec app python scripts/verify_hybrid_currency.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

sys.path.insert(0, "/app")

from app.hybrid import lexical_search                      # noqa: E402
from app.rag import currency_filter, fetch_candidates      # noqa: E402
from app.utils import _connect, get_vector_store, PROPERTY_ID  # noqa: E402

# Deliberately larger than the corpus: these cases are about what the date filter
# excludes, so relevance ranking must not be the thing doing the excluding.
WIDE = 200

# A. (as_of, query) - terms that appear in a superseded document.
SUPERSEDED_TERM_CASES = [
    ("2026-09-11", "$50"),          # payment_policy v1 and v2 both say $50; v3 says $75
    ("2026-09-11", "late fee"),
    ("2025-04-01", "$50"),          # the day v3 took effect - v2 ended the day before
]

# B. (as_of, broad query) - matches much of the corpus, across versions.
#
# Spelled with `or` on purpose. websearch_to_tsquery ANDs bare terms, so a list of
# six words matches almost nothing and the case would pass without the date filter
# ever being consulted. As an OR this matches 169 of the 208 chunks and reaches all
# 21 documents, 22 chunks of which are out of force on 2026-09-11 - so anything the
# arm excludes, it excludes because of the predicate and not because of relevance.
BROAD_QUERY = "rent or notice or resident or days or fee or property"
BROAD_CASES = [
    ("2026-09-11", BROAD_QUERY),
    ("2024-09-01", BROAD_QUERY),
    ("2024-03-01", BROAD_QUERY),
]

# C. Questions that reduce to an empty tsquery.
EMPTY_QUERY_CASES = ["the a of and", "what about it", "is it?"]

# D. (query, doc_type the lexical arm must surface)
RECALL_CASES = [
    ("$75", "payment_policy"),
    ("555-0199", "property_fact_sheet"),
    ("14 consecutive days", "building_rules"),
]

AS_OF_RECALL = "2026-09-11"


async def in_force_doc_ids(conn, as_of: date) -> set[str]:
    """The documents in force on `as_of`, read straight from the table.

    Computed here from the raw date columns rather than by calling currency_sql(),
    on purpose: a test that asks the code under test what the right answer is only
    proves the code agrees with itself.
    """
    rows = await conn.fetch(
        "SELECT DISTINCT doc_id, effective_from, effective_to_eff "
        "FROM langchain_pg_embedding WHERE property_id = $1",
        PROPERTY_ID,
    )
    return {
        r["doc_id"]
        for r in rows
        if r["effective_from"] <= as_of <= r["effective_to_eff"]
    }


def _report(label: str, ok: bool, detail: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<34} {detail}")
    return not ok


async def main() -> int:
    store = await get_vector_store()
    conn = await _connect()
    failures = 0

    try:
        print("A. lexical arm alone, terms distinctive to a superseded document")
        for as_of_s, query in SUPERSEDED_TERM_CASES:
            as_of = date.fromisoformat(as_of_s)
            allowed = await in_force_doc_ids(conn, as_of)
            docs = await lexical_search(query, as_of, limit=WIDE)
            got = {d.metadata["doc_id"] for d in docs}
            leaked = sorted(got - allowed)
            failures += _report(
                f"{as_of_s} {query!r}",
                not leaked,
                f"{len(docs)} chunks from {len(got)} docs"
                + (f"  LEAKED {leaked}" if leaked else ""),
            )

        print("\nB. both arms, broad query - relevance is not the filter here")
        for as_of_s, query in BROAD_CASES:
            as_of = date.fromisoformat(as_of_s)
            allowed = await in_force_doc_ids(conn, as_of)

            lex = await lexical_search(query, as_of, limit=WIDE)
            dense = await store.asimilarity_search(
                query, k=WIDE, filter=currency_filter(as_of)
            )
            for arm, docs in (("lexical", lex), ("dense", dense)):
                leaked = sorted({d.metadata["doc_id"] for d in docs} - allowed)
                failures += _report(
                    f"{as_of_s} {arm}",
                    not leaked,
                    f"{len(docs)} chunks, {len(allowed)} docs in force"
                    + (f"  LEAKED {leaked}" if leaked else ""),
                )

            # And the fused set the pipeline actually builds.
            fused = await fetch_candidates(store, query, as_of, limit=WIDE)
            leaked = sorted({d.metadata["doc_id"] for d in fused} - allowed)
            failures += _report(
                f"{as_of_s} fused",
                not leaked,
                f"{len(fused)} chunks"
                + (f"  LEAKED {leaked}" if leaked else ""),
            )

    finally:
        await conn.close()

    print("\nC. stop-word-only questions degrade to dense-only, without raising")
    as_of = date.fromisoformat(AS_OF_RECALL)
    for query in EMPTY_QUERY_CASES:
        try:
            lex = await lexical_search(query, as_of, limit=WIDE)
        except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
            failures += _report(f"{query!r}", False, f"raised {type(e).__name__}: {e}")
            continue
        fused = await fetch_candidates(store, query, as_of, limit=10)
        dense_only = await store.asimilarity_search(
            query, k=10, filter=currency_filter(as_of)
        )
        same = [d.id for d in fused] == [d.id for d in dense_only]
        failures += _report(
            f"{query!r}",
            not lex and same,
            f"lexical={len(lex)} chunks, fused order matches dense-only: {same}",
        )

    print("\nD. the lexical arm finds the exact terms it exists for")
    for query, want in RECALL_CASES:
        docs = await lexical_search(query, as_of, limit=WIDE)
        got = [d.metadata["doc_type"] for d in docs]
        failures += _report(
            f"{query!r}",
            want in got,
            f"want {want}, got {got or '[]'}",
        )

    total = (
        len(SUPERSEDED_TERM_CASES)
        + len(BROAD_CASES) * 3
        + len(EMPTY_QUERY_CASES)
        + len(RECALL_CASES)
    )
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
