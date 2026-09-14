"""
build_manifest.py: normalize data/metadata.json into data/manifest.json

The raw metadata.json is an export from a document management system. It is
accurate but not ingest-ready:

  - supersession links are one-sided and incomplete: a document often records the
    version it replaced, or the one that replaced it, but not both,
  - nothing ties a file to its version chain except the doc_type/version pair,
  - `effective_to: null` means "open ended", which is awkward to express in a
    vector-store range filter.

It also enforces the corpus policy that documents are held only once they are
officially in force - see check_coverage().

This script repairs those into a flat, ingest-ready manifest keyed by file path,
and fails loudly on anything it cannot reconcile. It derives; it never invents:
every field traces back to metadata.json or to the version chain itself.

Run:  python scripts/build_manifest.py [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW = DATA_DIR / "metadata.json"
OUT = DATA_DIR / "manifest.json"

# Sentinel for "still in force". Lets a retrieval filter use a plain range test
# instead of special-casing NULL on every query.
OPEN_ENDED = "9999-12-31"

# Files that live in data/ but are not part of the corpus. Anything else without a
# metadata record is an orphan and fails the build.
NOT_DOCUMENTS = {"metadata.json", "manifest.json", "README.md", ".DS_Store"}

# doc_type -> category. This mapping is the single definition of the corpus taxonomy:
# documents live in data/<category>/, and the build verifies that every file sits in
# the folder its doc_type maps to. The folder layout is therefore a projection of this
# dict, not an independent source of truth that could drift away from it.
#
# To re-file a doc_type, change it here, move its files to the new folder and update
# their paths in metadata.json.
CATEGORIES = {
    # the contract itself
    "lease_standard": "lease",
    # attachments to the lease - all templates, describing terms in the abstract
    "addendum_bed_bug": "addenda",
    "addendum_mold": "addenda",
    "addendum_pet": "addenda",
    "addendum_smoke_free": "addenda",
    "addendum_utility_allocation": "addenda",
    # how the tenancy is run day to day
    "deposit_policy": "policies",
    "maintenance_policy": "policies",
    "movein_moveout": "policies",
    "payment_policy": "policies",
    "pet_policy": "policies",
    "rent_increase_policy": "policies",
    # conduct in the building and shared areas
    "building_rules": "rules",
    # legally required notices handed to the tenant
    "disclosure_lead": "disclosures",
    "disclosures": "disclosures",
    "tenant_protections_notice": "disclosures",
    # reference facts about the building
    "property_fact_sheet": "property",
}

# Fields promoted to real vector-store columns because retrieval filters on them.
# Everything else rides along in the JSONB metadata blob.
#
# `is_superseded` is deliberately NOT here. It is structural (does a higher version
# exist?), not temporal, and the two come apart whenever a newer version is issued
# before it takes effect: that document is superseded on paper while still being the
# one in force. Filtering on is_superseded would silently drop the correct answer.
# Currency is decided by the effective_from/effective_to_eff range against the as-of
# date, and by nothing else. check_coverage() keeps future-dated documents out of the
# corpus, which makes that case rare - not impossible.
FILTER_COLUMNS = [
    "category",
    "doc_id",
    "property_id",
    "doc_type",
    "version",
    "effective_from",
    "effective_to_eff",
    "is_template",
    "disposition_hint",
]


class BuildError(Exception):
    pass


# Duplicated from app/utils.py rather than imported: this script is deliberately
# standalone so it runs on a host without the app's dependencies installed. Both
# read the same PROPERTY_TZ, so "is this document in the future?" is answered by the
# property's clock here too, not by whichever machine runs the build.
PROPERTY_TZ = ZoneInfo(os.getenv("PROPERTY_TZ", "America/Los_Angeles"))


def _today() -> date:
    return datetime.now(PROPERTY_TZ).date()


def _d(value: str) -> date:
    return date.fromisoformat(value)


def load_raw() -> list[dict]:
    if not RAW.exists():
        raise BuildError("missing data/metadata.json")
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    docs = raw.get("documents")
    if not docs:
        raise BuildError("metadata.json has no documents array")
    return docs


def check_files(docs: list[dict], problems: list[str]) -> None:
    """Every record points at a real file, every file has a record, and every file
    sits in the one folder its doc_type maps to."""
    declared = {d["path"] for d in docs}

    for d in sorted(docs, key=lambda x: x["path"]):
        path = d["path"]
        if not (DATA_DIR / path).is_file():
            problems.append(f"metadata references a file that does not exist: {path}")
            continue

        expected = CATEGORIES.get(d["doc_type"])
        if expected is None:
            problems.append(
                f"doc_type {d['doc_type']} has no category - add it to CATEGORIES"
            )
            continue

        parts = PurePosixPath(path).parts
        if len(parts) != 2:
            problems.append(
                f"{path}: expected data/<category>/<file>, got {len(parts) - 1} "
                "folder level(s)"
            )
        elif parts[0] != expected:
            problems.append(
                f"{path}: filed under {parts[0]}/ but doc_type {d['doc_type']} "
                f"maps to {expected}/"
            )

    on_disk = {
        p.relative_to(DATA_DIR).as_posix()
        for p in DATA_DIR.rglob("*")
        if p.is_file() and p.name not in NOT_DOCUMENTS
    }
    for orphan in sorted(on_disk - declared):
        problems.append(f"file on disk has no metadata record: {orphan}")


def build_chains(docs: list[dict]) -> dict:
    """Group documents into version chains keyed by (property_id, doc_type)."""
    chains = defaultdict(list)
    for d in docs:
        chains[(d["property_id"], d["doc_type"])].append(d)
    for key in chains:
        chains[key].sort(key=lambda d: d["version"])
    return chains


def validate_chain(key, chain: list[dict], problems: list[str]) -> None:
    """Versions must be 1..n with no gaps, and effective ranges must not overlap."""
    prop, doc_type = key
    versions = [d["version"] for d in chain]
    if versions != list(range(1, len(chain) + 1)):
        problems.append(f"{prop}/{doc_type}: version numbers are not contiguous: {versions}")

    for older, newer in zip(chain, chain[1:]):
        older_end = older.get("effective_to")
        if older_end is None:
            problems.append(
                f"{prop}/{doc_type}: v{older['version']} is open-ended but "
                f"v{newer['version']} supersedes it"
            )
            continue
        if _d(older_end) >= _d(newer["effective_from"]):
            problems.append(
                f"{prop}/{doc_type}: v{older['version']} (to {older_end}) overlaps "
                f"v{newer['version']} (from {newer['effective_from']})"
            )


def reconcile_links(key, chain: list[dict], notes: list[str], problems: list[str]) -> None:
    """Derive supersession from the version chain; cross-check declared links.

    The chain order is the authority. A declared link that contradicts it is an
    error worth stopping for; a link the export simply omitted is a repair.
    """
    by_version = {d["version"]: d for d in chain}

    for d in chain:
        prev = by_version.get(d["version"] - 1)
        nxt = by_version.get(d["version"] + 1)
        derived_supersedes = prev["doc_id"] if prev else None
        derived_superseded_by = nxt["doc_id"] if nxt else None

        for field, derived in (
            ("supersedes", derived_supersedes),
            ("superseded_by", derived_superseded_by),
        ):
            declared = d.get(field)
            if declared and declared != derived:
                problems.append(
                    f"{d['doc_id']}: declared {field}={declared} contradicts "
                    f"version chain ({derived})"
                )
            elif not declared and derived:
                notes.append(f"repaired {d['doc_id']}.{field} -> {derived}")

        d["_supersedes"] = derived_supersedes
        d["_superseded_by"] = derived_superseded_by


def check_coverage(docs: list[dict], as_of: date, allow_pending: bool,
                   problems: list[str], warnings: list[str]) -> None:
    """Corpus policy: it holds documents that are in force or expired, never ones
    that take effect in the future.

    A document is added when it is officially issued, and ingest is re-run. Keeping
    a future document in the corpus means it is embedded and sitting in the store
    ahead of time, relying entirely on the retrieval filter to stay hidden.

    The second check is the mirror image and the more dangerous one: a chain whose
    newest document has an end date will simply stop answering when that date
    passes, with nothing to replace it and no error anywhere.
    """
    for d in sorted(docs, key=lambda x: x["path"]):
        if _d(d["effective_from"]) > as_of:
            msg = (f"{d['path']}: effective_from {d['effective_from']} is in the "
                   f"future (as of {as_of}) - remove it until it is officially issued")
            (warnings if allow_pending else problems).append(msg)

    newest: dict[tuple[str, str], dict] = {}
    for d in docs:
        key = (d["property_id"], d["doc_type"])
        if key not in newest or d["version"] > newest[key]["version"]:
            newest[key] = d

    for (prop, doc_type), d in sorted(newest.items()):
        end = d.get("effective_to")
        if end is None:
            continue
        end_date = _d(end)
        if end_date < as_of:
            problems.append(
                f"{prop}/{doc_type}: nothing is in force - newest document "
                f"(v{d['version']}) expired on {end}"
            )
        else:
            warnings.append(
                f"{prop}/{doc_type}: v{d['version']} is the newest document and "
                f"expires in {(end_date - as_of).days} days ({end}) with no "
                "successor - after that, nothing answers for this doc_type"
            )


def to_record(d: dict, chain_len: int) -> dict:
    """One flat, ingest-ready record. Nothing date-relative is frozen in here."""
    effective_to = d.get("effective_to")
    return {
        # identity
        "path": d["path"],
        "category": CATEGORIES[d["doc_type"]],
        "format": d["format"],
        "doc_id": d["doc_id"],
        "property_id": d["property_id"],
        "doc_type": d["doc_type"],
        "title": d["title"],
        # version / time
        "version": d["version"],
        "effective_from": d["effective_from"],
        "effective_to": effective_to,                     # null = open ended, for display
        "effective_to_eff": effective_to or OPEN_ENDED,   # sentinel, for range filters
        "supersedes": d["_supersedes"],
        "superseded_by": d["_superseded_by"],
        "is_superseded": d["_superseded_by"] is not None,
        "is_latest_version": d["version"] == chain_len,
        "chain_length": chain_len,
        # provenance / routing
        "jurisdiction": d["jurisdiction"],
        "issuing_office": d["issuing_office"],
        "authority_refs": d.get("authority_refs", []),
        "disposition_hint": d["disposition_hint"],
        "is_template": bool(d.get("is_template", False)),
        "sections": d.get("sections", []),
    }


def in_force(record: dict, as_of: date) -> bool:
    return _d(record["effective_from"]) <= as_of <= _d(record["effective_to_eff"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=_today().isoformat(),
                    help="date used for the in-force report (default: today at the property)")
    ap.add_argument("--allow-pending", action="store_true",
                    help="downgrade future-dated documents from an error to a warning, "
                         "for staging a document shortly before it takes effect")
    args = ap.parse_args()
    as_of = _d(args.as_of)

    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    docs = load_raw()
    check_files(docs, problems)
    check_coverage(docs, as_of, args.allow_pending, problems, warnings)

    chains = build_chains(docs)
    for key, chain in sorted(chains.items()):
        validate_chain(key, chain, problems)
        reconcile_links(key, chain, notes, problems)

    if problems:
        print("MANIFEST BUILD FAILED\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    records = []
    for key, chain in sorted(chains.items()):
        for d in chain:
            records.append(to_record(d, len(chain)))
    records.sort(key=lambda r: (r["doc_type"], r["version"]))

    manifest = {
        "generated_by": "scripts/build_manifest.py",
        "source": "data/metadata.json",
        "note": (
            "Ingest-ready document manifest. Supersession links are derived from the "
            "version chain and are symmetric. effective_to is null for open-ended "
            f"documents; effective_to_eff carries the sentinel {OPEN_ENDED} so range "
            "filters need no NULL handling. Nothing here is relative to the current "
            "date - whether a document is in force is decided at query time by the "
            "effective_from/effective_to_eff range, NOT by is_superseded or "
            "is_latest_version. Those two are structural and can disagree with "
            "currency: a version can be superseded by a document that has not taken "
            "effect yet, and is then still the one in force."
        ),
        "open_ended_sentinel": OPEN_ENDED,
        "filter_columns": FILTER_COLUMNS,
        "documents": records,
    }
    OUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---- report ----
    print(f"wrote data/manifest.json: {len(records)} documents, {len(chains)} chains")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  ! {w}")
    if notes:
        print(f"\nrepaired {len(notes)} missing supersession link(s):")
        for n in notes:
            print(f"  - {n}")

    by_category: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_category[r["category"]].append(r)
    print(f"\ncategories ({len(by_category)}):")
    for cat in sorted(by_category):
        rows = by_category[cat]
        types = sorted({r["doc_type"] for r in rows})
        print(f"  {cat:12} {len(rows):>2} file(s), {len(types)} doc_type(s)")

    multi = {k: c for k, c in chains.items() if len(c) > 1}
    print(f"\nversioned doc_types ({len(multi)}):")
    for (prop, doc_type), chain in sorted(multi.items()):
        print(f"  {doc_type}: v1..v{len(chain)}")

    print(f"\nin force as of {as_of}:")
    current = [r for r in records if in_force(r, as_of)]
    for r in sorted(current, key=lambda r: r["doc_type"]):
        tail = "" if r["is_latest_version"] else "   <- NOT the highest version"
        print(f"  {r['doc_type']:28} v{r['version']}{tail}")

    pending = [r for r in records if _d(r["effective_from"]) > as_of]
    if pending:
        print(f"\nnot yet in force as of {as_of}:")
        for r in sorted(pending, key=lambda r: r["effective_from"]):
            print(f"  {r['doc_type']:28} v{r['version']}  from {r['effective_from']}")

    # Where "latest version" and "in force" disagree. Every entry here is a case
    # that a naive max(version) retriever gets wrong.
    divergent = [r for r in current if not r["is_latest_version"]]
    if divergent:
        print(f"\nstructural/temporal divergence as of {as_of}"
              f" - max(version) would answer these wrongly:")
        for r in divergent:
            print(f"  {r['doc_type']}: v{r['version']} is in force, but v{r['chain_length']} "
                  f"exists (superseded_by {r['superseded_by']})")

    expired = [r for r in records if _d(r["effective_to_eff"]) < as_of]
    print(f"\nexpired: {len(expired)}  |  in force: {len(current)}  |  pending: {len(pending)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
