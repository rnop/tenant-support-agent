"""
api.py: FastAPI layer - serves the frontend, runs ingestion, answers questions.

Summary:
- Serves the static HTML/JavaScript frontend.
- /health reports whether the database schema matches the manifest. The compose
  healthcheck calls it, so a container whose database schema has drifted from the
  manifest reports unhealthy instead of failing on the first question.
- /ingest runs ingestion in the background, /ingest/status polls it.
- /ask answers a question against the documents.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .ingest import run_ingest_async
from .hybrid import hybrid_config
from .rag import (
    answer_with_docs_async,
    RERANK_TOP_N,
    RETRIEVAL_FETCH_K,
    RETRIEVAL_K,
    RETRIEVAL_PER_DOC,
)
from .utils import verify_schema, today, PROPERTY_ID, PROPERTY_TZ, METADATA_COLUMNS

app = FastAPI(title="Tenant Assistant - Maple Court")

static_dir = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

_ingest_lock = asyncio.Lock()
_ingest_task: asyncio.Task | None = None
_ingest_last = {
    "status": "idle",      # idle | running | succeeded | failed
    "started_at": None,
    "finished_at": None,
    "stats": None,
    "error": None,
}

class Ask(BaseModel):
    question: str
    category: str | None = Field(
        default=None,
        description="Coarse filter: lease, addenda, policies, rules, disclosures, property",
    )
    doc_type: str | None = Field(default=None, description="Fine filter, e.g. payment_policy")


@app.get("/")
async def root_page():
    return FileResponse(static_dir / "index.html")


@app.get("/health")
async def health():
    """Liveness plus a real schema check.

    Deliberately does not touch OpenAI or Cohere: this runs every 10s from the
    compose healthcheck, and an upstream API hiccup should not mark the container
    unhealthy.
    """
    try:
        schema = await verify_schema()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"database unreachable: {e}"}, status_code=503
        )

    body = {
        "ok": schema["ok"],
        "property_id": PROPERTY_ID,
        "property_date": today().isoformat(),
        "property_tz": str(PROPERTY_TZ),
        "schema": schema,
        "retrieval": {
            "fetch_k": RETRIEVAL_FETCH_K,
            "per_doc": RETRIEVAL_PER_DOC,
            "k": RETRIEVAL_K,
            "rerank_top_n": RERANK_TOP_N,
        },
        "hybrid": hybrid_config(),
        "metadata_columns": METADATA_COLUMNS,
    }
    return JSONResponse(body, status_code=200 if schema["ok"] else 503)


async def _ingest_job():
    _ingest_last.update(
        {"status": "running", "started_at": time.time(),
         "finished_at": None, "stats": None, "error": None}
    )
    try:
        stats = await run_ingest_async()
        _ingest_last.update(
            {"status": "succeeded", "finished_at": time.time(), "stats": stats}
        )
    except Exception as e:
        _ingest_last.update(
            {"status": "failed", "finished_at": time.time(), "error": str(e)}
        )


@app.post("/ingest")
async def kick_off_ingest():
    global _ingest_task
    async with _ingest_lock:
        if _ingest_task and not _ingest_task.done():
            return JSONResponse(
                {"ok": False, "message": "Ingestion already running"}, status_code=409
            )
        _ingest_task = asyncio.create_task(_ingest_job())
    return {"ok": True, "message": "Ingestion started"}


@app.get("/ingest/status")
async def ingest_status():
    return {"ok": True, **_ingest_last}


@app.post("/ask")
async def ask(q: Ask):
    start = time.perf_counter()
    try:
        # No as_of: the API always answers as of today. answer_with_docs_async still
        # takes the argument, which is how evals and the verification one-liner reach
        # a document that is not in force yet.
        result = await answer_with_docs_async(
            q.question, category=q.category, doc_type=q.doc_type
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    elapsed = time.perf_counter() - start
    print(f"/ask [{result['as_of']}] {elapsed:.2f}s - {q.question[:60]!r}")

    return {"ok": True, "elapsed_s": round(elapsed, 3), **result}
