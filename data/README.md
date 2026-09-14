# Corpus — Maple Court

21 documents for one property (`maple-court`), in four formats (txt, md, pdf, docx).
The files themselves carry only a title, a version / effective-date line, and a page
footer. Everything else lives in metadata.

## Layout

```
data/
  addenda/       5 files   lease attachments, all templates
  disclosures/   3 files   legally required notices
  lease/         1 file    the standard lease
  policies/      9 files   deposit, maintenance, move-in/out, payment, pet, rent increase
  property/      1 file    building fact sheet
  rules/         2 files   building rules & shared areas
  metadata.json            raw DMS export — source of truth, edit this
  manifest.json            generated, ingest-ready — do not edit
```

**The folder is not the source of the category.** `category` is a function of
`doc_type`, defined once in `CATEGORIES` in `scripts/build_manifest.py`. The folder
layout is a projection of that dict, and the build *verifies* the two agree — a file
sitting in the wrong folder fails the build rather than silently retagging itself.
Inferring `category` from the folder name alone would have no way to detect a
misfiled document.

To re-file a doc_type: change `CATEGORIES`, move its files into the new folder, update
their paths in `metadata.json`, then run `python scripts/build_manifest.py` to revalidate.

## Regenerating

```bash
python scripts/build_manifest.py
```

The build fails rather than guessing if a record points at a missing file, a file has no
record, a file is in the wrong category folder, a doc_type has no category mapping,
versions are non-contiguous, effective ranges overlap, or a declared supersession link
contradicts the version chain.

## What the build normalizes

1. **Supersession links.** The raw export is one-sided — a document often records
   the version it replaced, or the one that replaced it, but not both. The manifest
   derives both directions from the version chain and cross-checks every declared
   link against it. 4 links are repaired on the current corpus.
2. **Open-ended validity.** `effective_to: null` is kept for display, and
   `effective_to_eff` carries the sentinel `9999-12-31` so a retrieval filter can use a
   plain range test with no NULL handling.
3. **Chain position.** `is_latest_version` and `chain_length` are computed per
   `(property_id, doc_type)` chain.

## Corpus policy: in force or expired, never future

The corpus holds documents that are in force now or were in force once. A document
that takes effect in the future is **not** added until it is officially issued; then
it is added and ingest is re-run.

`scripts/build_manifest.py` enforces this. A future-dated document fails the build
(`--allow-pending` downgrades it to a warning if you need to stage one). The build
also warns when the newest document in a chain has an end date and no successor,
because that chain will quietly stop answering when the date passes.

## Currency is a date question, not a version question

Three doc_types are versioned: `building_rules` (v1–v2), `payment_policy` (v1–v3),
`rent_increase_policy` (v1–v2). Superseded versions stay in the corpus — 4 documents
are expired — because they are the record of what applied at the time.

**A document is in force when `effective_from <= as_of <= effective_to_eff`, and by no
other test.** That matters even with no future documents around: without the range
test, a question about the late fee can be answered from `payment_policy.v1`, which
governed in 2022.

`is_superseded` and `is_latest_version` are *structural*, not temporal. Under the
policy above they usually agree with currency, but they come apart the moment a
document is issued ahead of its effective date — it is superseded on paper while
still being the one in force. The date range stays correct in both cases and costs
nothing extra, so it is the only currency test used.

## Schema

Promoted to real vector-store columns (retrieval filters on these):

`category` · `doc_id` · `property_id` · `doc_type` · `version` · `effective_from` ·
`effective_to_eff` · `is_template` · `disposition_hint`

`category` is the coarse filter (6 values), `doc_type` the fine one (17 values).

Carried in the JSONB metadata blob:

`path` · `format` · `title` · `effective_to` · `supersedes` · `superseded_by` ·
`is_superseded` · `is_latest_version` · `chain_length` · `jurisdiction` ·
`issuing_office` · `authority_refs` · `sections`

`disposition_hint` is `answer` for every document except `tenant_protections_notice.v1`,
which is `escalate` — the routing signal for questions the assistant should hand to a
human rather than answer.