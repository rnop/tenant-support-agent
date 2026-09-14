"""
eval_ragas.py: score the full pipeline against the golden dataset

Every row of seed/qna_test.jsonl is sent through answer_with_docs_async() at the
row's own as_of date, then judged two ways. The two are kept apart because they
answer different questions:

- Deterministic checks (pass/fail). 
    - Did the question take the expected route, did retrieval surface the document the answer rests on, 
    and was everything retrieved in force on as_of? A failure is a bug, and the script exits 1.
- RAGAS metrics scores from an LLM judge, 0 to 1. 
    - How good is the answer? Reported, never failed: a threshold is a decision to make once there 
    is a baseline.

What each row gets, by `expect` (a string, or a list of acceptable routes):

  answer, flagged_document   route, primary source, currency; RAGAS metrics
  no_answer                  route only - there is no reference answer to score
  maintenance_emergency,     route, and every phone number in the reference answer
  immediate_danger,          (and 911, if it names it) must appear in the contact
  safety_concern             block - the guardrail quotes the fact sheet verbatim

The primary source is sources[0] in the dataset. Later sources are supporting: the
same fact is often stated in more than one document (the lease repeats the entry
notice rule, for one), so requiring every one of them would fail correct answers.

RAGAS Metrics
- **faithfulness**: share of the answer's claims supported by the retrieved contexts
- **answer_relevancy**: how directly the answer addresses the question, judged by generating questions 
back from the answer and comparing them with the original
- **context_precision**: whether the contexts that are relevant to the reference answer are ranked above 
the ones that aren't
- **context_recall**: share of the reference answer's claims supported by the retrieved contexts
- **factual_correctness**: share of the reference answer's claims that the answer states

"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
import warnings
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_core.globals import set_llm_cache   # noqa: E402

from app.rag import answer_with_docs_async          # noqa: E402
from app.escalation import URGENT, NO_ANSWER        # noqa: E402

DATASET = ROOT / "seed" / "qna_test.jsonl"
MANIFEST = ROOT / "data" / "manifest.json"
RESULTS_DIR = ROOT / "eval_results"

SCORED_ROUTES = {"answer", "flagged_document"}
METRICS = ["faithfulness", "answer_relevancy", "context_precision",
           "context_recall", "factual_correctness"]
CHECKS = ["route", "source", "currency", "contact"]

PHONE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")

MAX_RETRIES = 8   # rate-limit retries per row; backoff caps at 60s

# --------------------------------------------------------------------------- data

def load_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_manifest() -> tuple[dict[str, tuple[str, int]], dict[tuple[str, int], tuple[date, date]]]:
    """doc_id -> (doc_type, version), and (doc_type, version) -> in-force range.

    Response sources carry doc_type and version but not doc_id, so both lookups are
    keyed to meet them there.
    """
    docs = json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]
    by_id = {d["doc_id"]: (d["doc_type"], d["version"]) for d in docs}
    in_force = {
        (d["doc_type"], d["version"]): (
            date.fromisoformat(d["effective_from"]),
            date.fromisoformat(d["effective_to_eff"]),
        )
        for d in docs
    }
    return by_id, in_force


def row_categories(rows: list[dict]) -> dict[str, str | None]:
    """Row id -> category of the row's primary source document."""
    docs = json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]
    category = {d["doc_id"]: d["category"] for d in docs}
    return {r["id"]: category[r["sources"][0]["doc_id"]] if r["sources"] else None for r in rows}


def expected_routes(row: dict) -> list[str]:
    e = row["expect"]
    return e if isinstance(e, list) else [e]


def actual_route(result: dict) -> str:
    esc = result.get("escalation")
    return esc["reason"] if esc else "answer"


# ------------------------------------------------------------------------- checks

def run_checks(row: dict, result: dict, by_id: dict, in_force: dict) -> dict[str, tuple[bool, str]]:
    """{check name: (passed, detail)}. Checks that do not apply to a row are absent."""
    want = expected_routes(row)
    got = actual_route(result)
    checks = {"route": (got in want, f"got {got}, want {' or '.join(want)}")}

    if any(w in URGENT for w in want):
        needed = PHONE.findall(row["answer"]) + (["911"] if "911" in row["answer"] else [])
        missing = [n for n in needed if n not in result["answer"]]
        checks["contact"] = (not missing, f"missing {missing}" if missing else "numbers present")
        return checks

    # No reference to check against: either the row has none, or the pipeline
    # declined and the row accepts that (my-balance: redirect or decline).
    if not row["sources"] or (got == NO_ANSWER and NO_ANSWER in want):
        return checks

    retrieved = {
        (s["doc_type"], s["version"]) for s in result["sources"] if s.get("doc_type")
    }

    primary = by_id[row["sources"][0]["doc_id"]]
    cited = {by_id[s["doc_id"]] for s in row["sources"]}
    hits = len(cited & retrieved)
    checks["source"] = (
        primary in retrieved,
        f"{primary[0]} v{primary[1]} {'retrieved' if primary in retrieved else 'NOT retrieved'}; "
        f"{hits}/{len(cited)} cited docs retrieved",
    )

    as_of = date.fromisoformat(row["as_of"])
    stale = sorted(
        f"{dt} v{v}" for dt, v in retrieved
        if not in_force[(dt, v)][0] <= as_of <= in_force[(dt, v)][1]
    )
    checks["currency"] = (not stale, f"not in force on {as_of}: {stale}" if stale else "all in force")
    return checks


# ------------------------------------------------------------------------ metrics

def build_metrics(model: str) -> dict:
    from openai import AsyncOpenAI
    from ragas.embeddings import OpenAIEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (
        AnswerRelevancy, ContextPrecisionWithReference, ContextRecall,
        FactualCorrectness, Faithfulness,
    )

    client = AsyncOpenAI()
    llm = llm_factory(model, client=client)
    # Same embedding model as the pipeline
    emb = OpenAIEmbeddings(client=client, model="text-embedding-3-small")

    faith = Faithfulness(llm=llm)
    relevancy = AnswerRelevancy(llm=llm, embeddings=emb)
    precision = ContextPrecisionWithReference(llm=llm)
    recall = ContextRecall(llm=llm)
    correctness = FactualCorrectness(llm=llm, mode="recall")

    return {
        "faithfulness": lambda s: faith.ascore(
            user_input=s["question"], response=s["response"], retrieved_contexts=s["contexts"]),
        "answer_relevancy": lambda s: relevancy.ascore(
            user_input=s["question"], response=s["response"]),
        "context_precision": lambda s: precision.ascore(
            user_input=s["question"], reference=s["reference"], retrieved_contexts=s["contexts"]),
        "context_recall": lambda s: recall.ascore(
            user_input=s["question"], retrieved_contexts=s["contexts"], reference=s["reference"]),
        "factual_correctness": lambda s: correctness.ascore(
            response=s["response"], reference=s["reference"]),
    }


def should_score(row: dict, result: dict) -> bool:
    """Only rows with a reference answer and something retrieved to judge it by.

    A row expected to answer is still scored when the model declined - a low score
    there is the finding - unless declining is one of the row's accepted routes. Nor
    when the guardrail intercepted it, because then the contexts are the contact
    block and every metric would be noise.
    """
    want, got = expected_routes(row), actual_route(result)
    return (
        bool(SCORED_ROUTES & set(want))
        and bool(row["sources"])
        and got not in URGENT
        and not (got == NO_ANSWER and NO_ANSWER in want)
        and bool(result["contexts"])
    )


async def score_row(row: dict, result: dict, metrics: dict, sem: asyncio.Semaphore) -> dict:
    sample = {
        "question": row["question"],
        "response": result["answer"],
        "contexts": result["contexts"],
        "reference": row["answer"],
    }

    async def one(name):
        async with sem:
            try:
                r = await metrics[name](sample)
                return name, {"value": r.value, "reason": getattr(r, "reason", None)}
            except Exception as e:  # one metric failing should not lose the others
                return name, {"value": None, "reason": f"error: {e!r}"}

    return dict(await asyncio.gather(*(one(n) for n in METRICS)))


# -------------------------------------------------------------------------- run

async def run_row(row, by_id, in_force, categories, metrics, pipeline_sem, metric_sem) -> dict:
    out = {"id": row["id"], "kind": row["kind"], "category": categories[row["id"]],
           "question": row["question"],
           "as_of": row["as_of"], "expect": row["expect"], "reference": row["answer"]}
    start = time.perf_counter()
    retries = 0
    while True:
        try:
            async with pipeline_sem:
                result = await answer_with_docs_async(
                    row["question"], as_of=date.fromisoformat(row["as_of"])
                )
            break
        except Exception as e:
            # A rate limit (Cohere trial keys allow ~10 rerank calls a minute)
            if getattr(e, "status_code", None) == 429 and retries < MAX_RETRIES:
                retries += 1
                await asyncio.sleep(min(60, 5 * 2 ** retries))
                continue
            out.update(error=repr(e), retries=retries, metrics={},
                       checks={"route": (False, f"pipeline error: {e!r}")})
            return out

    out.update(
        elapsed_s=round(time.perf_counter() - start, 2),
        retries=retries,
        route=actual_route(result),
        answer=result["answer"],
        sources=[s.get("citation") for s in result["sources"]],
        contexts=result["contexts"],
        checks=run_checks(row, result, by_id, in_force),
        metrics={},
    )
    if metrics and should_score(row, result):
        out["metrics"] = await score_row(row, result, metrics, metric_sem)
    return out


def _fmt(v) -> str:
    return "  -  " if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:5.2f}"


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return sum(vals) / len(vals) if vals else None


def summarize(results: list[dict]) -> dict:
    """Aggregate results (as saved to JSONL) into check totals and metric means.

    Works on a results file after the fact as well as on a fresh run - see
    --summarize - so a summary can always be regenerated from the rows.
    """
    def means(group):
        out = {"n": len(group)}
        out.update({m: _mean((r["metrics"].get(m) or {}).get("value") for r in group) for m in METRICS})
        return out

    scored = [r for r in results if r["metrics"]]
    return {
        "rows": len(results),
        "checks": {
            k: {"passed": sum(r["checks"][k]["ok"] for r in results if k in r["checks"]),
                "total": sum(k in r["checks"] for r in results)}
            for k in CHECKS if any(k in r["checks"] for r in results)
        },
        "failures": [
            {"id": r["id"], "check": k, "detail": c["detail"]}
            for r in results for k, c in r["checks"].items() if not c["ok"]
        ],
        "metrics": {
            "all": means(scored),
            "by_category": {c: means([r for r in scored if r["category"] == c])
                            for c in sorted({r["category"] for r in scored})},
        } if scored else None,
        "metric_errors": sum(
            1 for r in scored for m in METRICS
            if ((r["metrics"].get(m) or {}).get("reason") or "").startswith("error:")
        ),
        "rate_limit_retries": sum(r.get("retries", 0) for r in results),
    }


def report(results: list[dict], summary: dict) -> int:
    abbrev = {"faithfulness": "faith", "answer_relevancy": "relev", "context_precision": "c_pre",
              "context_recall": "c_rec", "factual_correctness": "fact"}
    has_metrics = summary["metrics"] is not None
    head = f"  {'id':30} {'kind':12} {'route':5} {'src':4} {'cur':4} {'cont':4}"
    if has_metrics:
        head += " " + " ".join(f"{abbrev[m]:>5}" for m in METRICS)
    print(head)

    for r in results:
        c = r["checks"]
        cells = " ".join(
            f"{('ok' if c[k]['ok'] else 'FAIL') if k in c else '-':{w}}"
            for k, w in zip(CHECKS, (5, 4, 4, 4))
        )
        line = f"  {r['id']:30} {r['kind']:12} {cells}"
        if has_metrics:
            line += " " + " ".join(_fmt((r["metrics"].get(m) or {}).get("value")) for m in METRICS)
        print(line)

    # --- check failures, with the detail needed to act on them
    answers = {r["id"]: r.get("answer") for r in results}
    if summary["failures"]:
        print("\ncheck failures:")
        for f in summary["failures"]:
            print(f"  {f['id']:30} {f['check']:9} {f['detail']}")
            if f["check"] == "route" and answers[f["id"]]:
                print(f"  {'':30} {'':9} answer: {answers[f['id']].strip()[:160]!r}")

    print("\nchecks:")
    for k, c in summary["checks"].items():
        print(f"  {k:9} {c['passed']}/{c['total']}")

    if has_metrics:
        m = summary["metrics"]
        print(f"\nmetrics (mean over {m['all']['n']} scored rows):")
        print(f"  {'':12} {'n':>3} " + " ".join(f"{abbrev[x]:>5}" for x in METRICS))
        for label, group in [("all", m["all"])] + list(m["by_category"].items()):
            print(f"  {label:12} {group['n']:3} " + " ".join(_fmt(group[x]) for x in METRICS))
        if summary["metric_errors"]:
            print(f"  ({summary['metric_errors']} metric call(s) errored - see the results file)")

    if summary["rate_limit_retries"]:
        print(f"\n{summary['rate_limit_retries']} rate-limit retries")

    return 1 if summary["failures"] else 0


def write_summary(results_path: Path, summary: dict, **run_info) -> Path:
    path = results_path.with_name(results_path.stem + ".summary.json")
    body = {"results": str(results_path.relative_to(ROOT)), **run_info, **summary}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--kind", action="append", help="only rows of this kind (repeatable)")
    ap.add_argument("--id", action="append", help="only this row id (repeatable)")
    ap.add_argument("--checks-only", action="store_true", help="skip RAGAS; no judge calls")
    ap.add_argument("--use-cache", action="store_true", help="keep the Redis LLM cache on")
    ap.add_argument("--judge-model", default="gpt-4o-mini")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="pipeline calls at once (low by default: the reranker is rate limited)")
    ap.add_argument("--out", type=Path, help="results JSONL (default: eval_results/<timestamp>.jsonl)")
    ap.add_argument("--summarize", type=Path, metavar="RESULTS_JSONL",
                    help="don't run anything: rebuild the summary for an existing results file")
    args = ap.parse_args()

    if args.summarize:
        path = args.summarize.resolve()
        results = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        # Files written before rows carried a category: derive it from the dataset.
        categories = row_categories(load_rows(args.dataset))
        for r in results:
            r.setdefault("category", categories.get(r["id"]))
        summary = summarize(results)
        report(results, summary)
        print(f"\nsummary: {write_summary(path, summary).relative_to(ROOT)}")
        return 0

    if not args.use_cache:
        set_llm_cache(None)

    rows = load_rows(args.dataset)
    if args.kind:
        rows = [r for r in rows if r["kind"] in args.kind]
    if args.id:
        rows = [r for r in rows if r["id"] in args.id]
    if not rows:
        print("no rows match the filters")
        return 2

    by_id, in_force = load_manifest()
    categories = row_categories(rows)
    metrics = None if args.checks_only else build_metrics(args.judge_model)
    pipeline_sem = asyncio.Semaphore(args.concurrency)
    metric_sem = asyncio.Semaphore(args.concurrency * 3)

    print(f"{len(rows)} rows from {args.dataset.relative_to(ROOT)}  |  "
          f"cache {'on' if args.use_cache else 'off'}  |  "
          f"{'checks only' if args.checks_only else f'judge {args.judge_model}'}\n")

    started = datetime.now()
    start = time.perf_counter()
    results = await asyncio.gather(*(
        run_row(r, by_id, in_force, categories, metrics, pipeline_sem, metric_sem) for r in rows
    ))
    # The saved results shape - checks as {"ok", "detail"}
    results = [
        {**r, "checks": {k: {"ok": ok, "detail": d} for k, (ok, d) in r["checks"].items()}}
        for r in results
    ]
    summary = summarize(results)
    exit_code = report(results, summary)
    elapsed = time.perf_counter() - start

    out = (args.out or RESULTS_DIR / f"{started:%Y%m%d-%H%M%S}.jsonl").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    summary_path = write_summary(
        out, summary,
        started=started.isoformat(timespec="seconds"),
        elapsed_s=round(elapsed),
        dataset=str(args.dataset.resolve().relative_to(ROOT)),
        filters={"kind": args.kind, "id": args.id},
        judge_model=None if args.checks_only else args.judge_model,
        cache=args.use_cache,
    )
    print(f"\n{elapsed:.0f}s  |  results: {out.relative_to(ROOT)}  |  summary: {summary_path.relative_to(ROOT)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
