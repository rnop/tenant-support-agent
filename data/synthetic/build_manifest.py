"""Build and validate data/synthetic/manifest.json from document front matter.

The manifest is the single input to ingestion, and the source of the doc_id sets that
evals/leak asserts against. It is generated, never hand-edited.

Validation runs first and is fatal. Fifty-odd hand-written front matter blocks are exactly
where transcription errors hide, and a corpus that silently disagrees with itself makes
every downstream eval untrustworthy.

Usage:
    python data/synthetic/build_manifest.py            # validate and write manifest
    python data/synthetic/build_manifest.py --check    # validate only, no write
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "_fixtures"

# The date the corpus is authored against. Documents are checked for a coherent
# as-of resolution at this date.
AS_OF = date(2026, 8, 1)

PROPERTIES = {
    "maple-court": {
        "name": "Maple Court",
        "address": "3140 Roswell Avenue, Long Beach, CA 90814",
        "city": "Long Beach",
        "type": "4-unit apartment building",
        "built": 1962,
    },
    "harborview": {
        "name": "Harborview Apartments",
        "address": "3145 Roswell Avenue, Long Beach, CA 90814",
        "city": "Long Beach",
        "type": "4-unit apartment building",
        "built": 2004,
    },
    "cedar-ridge": {
        "name": "Cedar Ridge House",
        "address": "11482 Cypress Canyon Road, San Diego, CA 92131",
        "city": "San Diego",
        "type": "single-family home, 3 bd / 2.5 ba",
        "built": 1998,
    },
}

REQUIRED_CORPUS_FIELDS = [
    "doc_id",
    "property_id",
    "title",
    "doc_type",
    "version",
    "effective_from",
    "jurisdiction",
    "issuing_office",
]


def split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("no front matter")
    end = text.index("\n---", 3)
    meta = yaml.safe_load(text[3:end])
    body = text[end + 4 :]
    return meta or {}, body


def as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def overlaps(a_from, a_to, b_from, b_to) -> bool:
    a_to = a_to or date.max
    b_to = b_to or date.max
    return a_from <= b_to and b_from <= a_to


def collect() -> tuple[list[dict], list[dict], list[str]]:
    corpus: list[dict] = []
    fixtures: list[dict] = []
    errors: list[str] = []

    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        if path.name == "README.md":
            continue
        try:
            meta, body = split_front_matter(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rel}: could not parse front matter ({exc})")
            continue

        record = {
            "path": rel,
            "doc_id": meta.get("doc_id"),
            "words": len(body.split()),
            "sections": len(meta.get("sections") or []),
        }

        if FIXTURE_DIR in path.parents:
            record.update(
                {
                    "fixture": meta.get("fixture"),
                    "must_not_ingest": bool(meta.get("must_not_ingest")),
                    "title": meta.get("title"),
                    "attack": meta.get("attack"),
                    "based_on": meta.get("based_on"),
                    "reason": meta.get("reason"),
                }
            )
            if record["fixture"] not in {"rejected", "injection"}:
                errors.append(f"{rel}: fixture must be 'rejected' or 'injection'")
            fixtures.append(record)
            continue

        for field in REQUIRED_CORPUS_FIELDS:
            if meta.get(field) in (None, ""):
                errors.append(f"{rel}: missing required field '{field}'")

        prop_dir = path.parent.name
        if meta.get("property_id") != prop_dir:
            errors.append(
                f"{rel}: property_id '{meta.get('property_id')}' does not match "
                f"directory '{prop_dir}'"
            )
        if "is_current" in meta:
            errors.append(f"{rel}: forbidden field 'is_current' (invariant 9)")

        try:
            eff_from = as_date(meta.get("effective_from"))
            eff_to = as_date(meta.get("effective_to"))
        except ValueError as exc:
            errors.append(f"{rel}: bad date ({exc})")
            continue

        if eff_from and eff_to and eff_from > eff_to:
            errors.append(f"{rel}: effective_from {eff_from} is after effective_to {eff_to}")

        record.update(
            {
                "property_id": meta.get("property_id"),
                "title": meta.get("title"),
                "doc_type": meta.get("doc_type"),
                "version": meta.get("version"),
                "effective_from": eff_from.isoformat() if eff_from else None,
                "effective_to": eff_to.isoformat() if eff_to else None,
                "jurisdiction": meta.get("jurisdiction"),
                "authority_refs": meta.get("authority_refs") or [],
                "disposition_hint": meta.get("disposition_hint"),
                "is_template": bool(meta.get("is_template")),
                "superseded_by": meta.get("superseded_by"),
                "todo_verify": [
                    ref
                    for ref in (meta.get("authority_refs") or [])
                    if isinstance(ref, str) and "TODO(verify)" in ref
                ],
            }
        )
        corpus.append(record)

    return corpus, fixtures, errors


def validate(corpus: list[dict], fixtures: list[dict], errors: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for rec in corpus + fixtures:
        doc_id = rec["doc_id"]
        if doc_id in seen:
            errors.append(f"duplicate doc_id '{doc_id}' in {rec['path']} and {seen[doc_id]}")
        seen[doc_id] = rec["path"]

    # Two documents of the same type at the same property must not be in force at once.
    by_key = defaultdict(list)
    for rec in corpus:
        by_key[(rec["property_id"], rec["doc_type"])].append(rec)

    for (prop, doc_type), recs in sorted(by_key.items()):
        for i, a in enumerate(recs):
            for b in recs[i + 1 :]:
                if overlaps(
                    date.fromisoformat(a["effective_from"]),
                    date.fromisoformat(a["effective_to"]) if a["effective_to"] else None,
                    date.fromisoformat(b["effective_from"]),
                    date.fromisoformat(b["effective_to"]) if b["effective_to"] else None,
                ):
                    errors.append(
                        f"{prop}/{doc_type}: overlapping effective ranges — "
                        f"v{a['version']} and v{b['version']}"
                    )

    # Every superseded_by must point at a real doc_id.
    for rec in corpus:
        target = rec.get("superseded_by")
        if target and target not in seen:
            errors.append(f"{rec['path']}: superseded_by '{target}' does not exist")

    # A property with no document in force today is a corpus bug.
    for prop in PROPERTIES:
        live = [
            r
            for r in corpus
            if r["property_id"] == prop
            and date.fromisoformat(r["effective_from"]) <= AS_OF
            and (r["effective_to"] is None or date.fromisoformat(r["effective_to"]) >= AS_OF)
        ]
        if not live:
            errors.append(f"{prop}: no document in force as of {AS_OF}")

    return errors


def current_as_of(corpus: list[dict], as_of: date) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for rec in corpus:
        if rec["is_template"]:
            continue
        start = date.fromisoformat(rec["effective_from"])
        end = date.fromisoformat(rec["effective_to"]) if rec["effective_to"] else None
        if start <= as_of and (end is None or end >= as_of):
            out[rec["property_id"]][rec["doc_type"]] = rec["doc_id"]
    return dict(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate only, do not write")
    args = ap.parse_args()

    corpus, fixtures, errors = collect()
    errors = validate(corpus, fixtures, errors)

    if errors:
        print(f"FAILED — {len(errors)} problem(s):\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    by_property = defaultdict(list)
    for rec in corpus:
        by_property[rec["property_id"]].append(rec)

    manifest = {
        "as_of": AS_OF.isoformat(),
        "organization": "Alder Grove Property Management",
        "counts": {
            "properties": len(PROPERTIES),
            "corpus_documents": len(corpus),
            "templates": sum(1 for r in corpus if r["is_template"]),
            "fixtures_rejected": sum(1 for r in fixtures if r["fixture"] == "rejected"),
            "fixtures_injection": sum(1 for r in fixtures if r["fixture"] == "injection"),
            "total_words": sum(r["words"] for r in corpus),
        },
        "properties": {
            pid: {
                **meta,
                "document_count": len(by_property[pid]),
                "doc_ids": sorted(r["doc_id"] for r in by_property[pid]),
            }
            for pid, meta in PROPERTIES.items()
        },
        "current_as_of": current_as_of(corpus, AS_OF),
        "escalate_only": sorted(
            r["doc_id"] for r in corpus if r["disposition_hint"] == "escalate"
        ),
        "todo_verify": sorted(
            {r["doc_id"] for r in corpus if r["todo_verify"]}
        ),
        "documents": sorted(corpus, key=lambda r: (r["property_id"], r["doc_type"], r["version"])),
        "fixtures": {
            "rejected": sorted(
                (r for r in fixtures if r["fixture"] == "rejected"),
                key=lambda r: r["doc_id"],
            ),
            "injection": sorted(
                (r for r in fixtures if r["fixture"] == "injection"),
                key=lambda r: r["doc_id"],
            ),
        },
    }

    print("OK — corpus validates.")
    print(f"  corpus documents : {manifest['counts']['corpus_documents']}")
    print(f"  of which templates: {manifest['counts']['templates']}")
    print(f"  rejected fixtures: {manifest['counts']['fixtures_rejected']}")
    print(f"  injection fixtures: {manifest['counts']['fixtures_injection']}")
    print(f"  total words      : {manifest['counts']['total_words']:,}")
    for pid in PROPERTIES:
        print(f"  {pid:<13}: {len(by_property[pid])} documents")

    if args.check:
        return 0

    out = ROOT / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out.relative_to(ROOT.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
