"""
verify_currency.py: prove the retrieval filter returns the version in force

The whole design rests on currency_filter() actually reaching SQL and excluding
superseded documents. A wrong-but-plausible answer from the full chain would not
reveal a broken filter - the model would just answer from whatever it was given.
So this checks retrieval directly, with no LLM in the loop: same query, several
dates, and asserts which version comes back.

Run inside the app container, after ingest:
    docker compose exec app python scripts/verify_currency.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

sys.path.insert(0, "/app")

from app.rag import currency_filter          # noqa: E402
from app.utils import get_vector_store       # noqa: E402

# (as_of, query, doc_type, expected version in force on that date)
CASES = [
    ("2024-03-01", "late rent fee and grace period", "payment_policy", 1),
    ("2024-09-01", "late rent fee and grace period", "payment_policy", 2),
    ("2026-09-11", "late rent fee and grace period", "payment_policy", 3),
    ("2024-06-01", "quiet hours and noise in shared areas", "building_rules", 1),
    ("2026-09-11", "quiet hours and noise in shared areas", "building_rules", 2),
    ("2026-01-15", "how much can the rent be increased", "rent_increase_policy", 1),
    ("2026-09-11", "how much can the rent be increased", "rent_increase_policy", 2),
]


async def main() -> int:
    store = await get_vector_store()
    failures = 0

    for as_of_s, query, doc_type, expected in CASES:
        as_of = date.fromisoformat(as_of_s)
        docs = await store.asimilarity_search(
            query, k=20, filter=currency_filter(as_of)
        )

        versions = sorted({
            d.metadata["version"] for d in docs if d.metadata["doc_type"] == doc_type
        })
        # Anything returned must be in force; more than one version of the same
        # doc_type means the filter is not doing its job.
        ok = versions == [expected]
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {as_of_s}  {doc_type:22} got v{versions or '-'}  want v[{expected}]")

        if not ok:
            for d in docs:
                if d.metadata["doc_type"] == doc_type:
                    print(f"         v{d.metadata['version']} "
                          f"{d.metadata['effective_from']} -> {d.metadata['effective_to_eff']}")

    total = len(CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
