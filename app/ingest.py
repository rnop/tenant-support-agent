"""
ingest.py: load documents, join their metadata, chunk on section boundaries, store

Summary:
- Step 1: Iterate data/manifest.json, not the filesystem. The manifest is the list
  of documents that exist; a file it does not name is not ingested, and a file it
  names but that is missing is an error.
- Step 2: Join every loader unit into one text per document and strip the headers
  and footers that repeat across pages.
- Step 3: Split on the section headings the manifest already records, then split
  further only where a section is too large. Each chunk knows which section it came
  from.
- Step 4: Attach the manifest record to every chunk, embed, store, index.
"""

from __future__ import annotations

import os
import re
import traceback
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from langchain_classic.docstore.document import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import (
    UnstructuredMarkdownLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    TextLoader,
)
from langchain_postgres.v2.indexes import HNSWIndex, DistanceStrategy

from .hybrid import FTS_INDEX_DDL, FTS_INDEX_NAME
from .utils import (
    get_vector_store, load_manifest, reset_property, _connect,
    METADATA_COLUMNS, DATA_DIR, PROPERTY_ID,
)

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "900"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))

# Catches "Page 1 of 2" variations per page
PAGE_MARKER = re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$", re.M)

# Catches boilerplate lines repeating on this share of a document's pages
BOILERPLATE_PAGE_SHARE = 0.5
BOILERPLATE_MAX_LEN = 150


def _loader_for(path: Path):
    """Explicit UTF-8: the corpus uses em dashes and middle dots, and TextLoader
    otherwise falls back to the platform encoding."""
    ext = path.suffix.lower()
    if ext == ".md":
        return UnstructuredMarkdownLoader(str(path))
    if ext == ".pdf":
        return PyMuPDFLoader(str(path))
    if ext == ".docx":
        return UnstructuredWordDocumentLoader(str(path))
    if ext == ".txt":
        return TextLoader(str(path), encoding="utf-8")
    raise ValueError(f"no loader for extension {ext!r} ({path})")


def _strip_boilerplate(pages: list[str]) -> str:
    """Join a document's pages, dropping the header/footer lines that repeat.

    Every PDF page here carries a footer like
        Lead-Based Paint Disclosure - Maple Court · Version 1 · Effective ... · Page 2 of 2
    which is identical across pages, duplicates metadata already held in columns,
    and dilutes every embedding it lands in.

    Detected rather than pattern-matched, so it works for docx and any future
    format without knowing what the footer says. Single-page documents have nothing
    to compare against, so only the page marker is removed.
    """
    pages = [PAGE_MARKER.sub("", p) for p in pages]
    if len(pages) < 2:
        return "\n".join(pages)

    counts: Counter[str] = Counter()
    for page in pages:
        # set(): a line repeated within one page is not evidence of boilerplate.
        counts.update({ln.strip() for ln in page.splitlines() if ln.strip()})

    threshold = max(2, round(len(pages) * BOILERPLATE_PAGE_SHARE))
    boilerplate = {
        line for line, n in counts.items()
        if n >= threshold and len(line) <= BOILERPLATE_MAX_LEN
    }

    kept = []
    for page in pages:
        kept.extend(
            ln for ln in page.splitlines() if ln.strip() not in boilerplate
        )
    return "\n".join(kept)


def _find_sections(text: str, sections: list[dict]) -> list[tuple[dict | None, str]]:
    """Slice text at the section headings the manifest records.

    Searches for known headings rather than guessing at heading shape, which is what
    makes this work across txt, md, pdf and docx: the four formats render a heading
    differently, but all of them contain the line "4. Laundry room" somewhere.

    Returns [(section_or_None, body)]. The leading title block, before any heading,
    comes back with section None.
    """
    hits: list[tuple[int, dict]] = []
    for s in sections:
        pattern = re.compile(
            rf"^[ \t]*#*[ \t]*{re.escape(s['number'])}\.[ \t]*{re.escape(s['title'])}[ \t]*$",
            re.M | re.I,
        )
        m = pattern.search(text)
        if m:
            hits.append((m.start(), s))

    if not hits:
        return [(None, text)]

    hits.sort(key=lambda h: h[0])
    parts: list[tuple[dict | None, str]] = []

    preamble = text[: hits[0][0]].strip()
    if preamble:
        parts.append((None, preamble))

    for i, (start, section) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        body = text[start:end].strip()
        if body:
            parts.append((section, body))

    return parts


def _metadata_for(record: dict, section: dict | None) -> dict:
    """Build chunk metadata from a manifest record.

    The nine keys in METADATA_COLUMNS are pulled into real SQL columns by the vector
    store; everything else is JSON-serialised into langchain_metadata. That split
    drives the two type rules here:

      - effective_from / effective_to_eff become datetime.date, because the columns
        are DATE and asyncpg will not coerce a string on insert.
      - the same two fields stay ISO strings under different keys in the JSON part,
        which has to remain serialisable.

    Build a fresh dict rather than updating the loader's metadata: PyMuPDFLoader
    sets its own `title` and `format` from the PDF headers, which would collide with
    the manifest's values for the same keys.
    """
    section_ref = f" §{section['number']} {section['title']}" if section else ""

    meta: dict[str, Any] = {
        # --- promoted to SQL columns (filterable) ---
        "category": record["category"],
        "doc_id": record["doc_id"],
        "property_id": record["property_id"],
        "doc_type": record["doc_type"],
        "version": record["version"],
        "effective_from": date.fromisoformat(record["effective_from"]),
        "effective_to_eff": date.fromisoformat(record["effective_to_eff"]),
        "is_template": record["is_template"],
        "disposition_hint": record["disposition_hint"],
        # --- carried as JSON (display, citation, provenance) ---
        "source": record["path"],
        "citation": _citation(record),
        "section_number": section["number"] if section else None,
        "section_title": section["title"] if section else None,
        "section_ref": section_ref,

        # Rendered here rather than in the prompt template so the prompt never has
        # to render a raw boolean. Five addenda are templates: they describe the
        # standard terms of an agreement, not what any particular resident signed,
        # and the model must not present them as binding.
        "template_note": (
            " [TEMPLATE - describes standard terms, not a signed agreement]"
            if record["is_template"] else ""
        ),
        "title": record["title"],
        "format": record["format"],
        "effective_from_iso": record["effective_from"],
        "effective_to_iso": record["effective_to"],
        "supersedes": record["supersedes"],
        "superseded_by": record["superseded_by"],
        "is_superseded": record["is_superseded"],
        "is_latest_version": record["is_latest_version"],
        "chain_length": record["chain_length"],
        "jurisdiction": record["jurisdiction"],
        "issuing_office": record["issuing_office"],
        "authority_refs": record["authority_refs"],
    }
    return meta


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _human_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _citation(record: dict) -> str:
    """What a resident should see next to an answer."""
    return (
        f"{record['title']} "
        f"(v{record['version']}, effective {_human_date(record['effective_from'])})"
    )


def build_chunks() -> list[Document]:
    """One Document per chunk, section-aware, metadata attached."""
    manifest = load_manifest()
    base = Path(DATA_DIR)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    chunks: list[Document] = []

    for record in manifest["documents"]:
        path = base / record["path"]
        if not path.is_file():
            raise FileNotFoundError(
                f"manifest lists {record['path']} but the file is missing - "
                "re-run scripts/build_manifest.py"
            )
        try:
            loaded = _loader_for(path).load()
        except Exception:
            print(f"INGEST ERROR: failed to load {record['path']}")
            traceback.print_exc()
            raise

        text = _strip_boilerplate([d.page_content for d in loaded])

        for section, body in _find_sections(text, record.get("sections", [])):
            pieces = [body] if len(body) <= CHUNK_SIZE else splitter.split_text(body)
            for i, piece in enumerate(pieces):
                if i and section:
                    piece = f"[{section['number']}. {section['title']} (continued)]\n{piece}"
                chunks.append(
                    Document(page_content=piece, metadata=_metadata_for(record, section))
                )

    return chunks


async def _create_index(store) -> None:
    """Build the two retrieval indexes over the finished data.

    HNSW is dropped by reset_property() and rebuilt here, because an HNSW graph
    built incrementally as rows arrive is measurably worse than one built over the
    complete set.

    The GIN full-text index is not dropped and is created IF NOT EXISTS, so on a
    re-run this is a no-op - see reset_property() for why that asymmetry is
    deliberate. It is issued here as well as in init-db/init.sql because init.sql
    only runs against an empty data directory: this is what gets the index onto a
    database that already exists.
    """
    index = HNSWIndex(
        name="hnsw_idx",
        distance_strategy=DistanceStrategy.COSINE_DISTANCE,
        m=16,
        ef_construction=64,
    )
    await store.aapply_vector_index(index, concurrently=True)
    print("INGEST: HNSW index created")

    conn = await _connect()
    try:
        await conn.execute(FTS_INDEX_DDL)
    finally:
        await conn.close()
    print(f"INGEST: {FTS_INDEX_NAME} full-text index present")


async def run_ingest_async() -> dict:
    chunks = build_chunks()

    # Replace rather than append: see reset_property().
    removed = await reset_property(PROPERTY_ID)
    if removed:
        print(f"INGEST: cleared {removed} existing chunks for {PROPERTY_ID}")

    store = await get_vector_store()
    await store.aadd_documents(chunks)

    by_category: dict[str, int] = {}
    for c in chunks:
        cat = c.metadata["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    with_section = sum(1 for c in chunks if c.metadata["section_number"])
    manifest = load_manifest()

    print(f"INGEST: {len(manifest['documents'])} docs, {len(chunks)} chunks, "
          f"{with_section} mapped to a section")
    for cat in sorted(by_category):
        print(f"  {cat:12} {by_category[cat]:>4} chunks")

    await _create_index(store)

    return {
        "replaced_chunks": removed,
        "documents": len(manifest["documents"]),
        "chunks": len(chunks),
        "chunks_with_section": with_section,
        "by_category": by_category,
        "metadata_columns": METADATA_COLUMNS,
    }
