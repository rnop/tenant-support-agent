"""
rag.py: RAG pipeline that answers tenant questions from the property documents.

Summary:
- Retrieves top-k chunks from pgvector, filtered to the documents actually IN FORCE
  on a given date.
- Hybrid retrieval: a dense vector arm and a lexical tsvector arm run
  concurrently and are fused by Reciprocal Rank Fusion before the per-document cap,
  so diversity and reranking both see the combined set. See app/hybrid.py for why,
  and for the warning about the date filter on the lexical arm.
- Reranker with Cohere and keeps the best few.
- Generates a grounded answer with gpt-4o-mini, citing title and version.
- Flags questions that land on documents marked `escalate` rather than answering.
- Caches LLM responses in Redis by semantic similarity.

REQUIREMENTS: OPENAI_API_KEY, CO_API_KEY and a reachable REDIS_URL at import time.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Any

from pydantic import ConfigDict

from langchain_core.prompts import ChatPromptTemplate, PromptTemplate, format_document
from langchain_core.globals import set_llm_cache
from langchain_core.retrievers import BaseRetriever
from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.chains import create_retrieval_chain
from langchain_classic.docstore.document import Document
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_cohere import CohereRerank
from langchain_redis import RedisSemanticCache

from . import escalation
from .hybrid import (
    HYBRID_DENSE_WEIGHT,
    HYBRID_LEXICAL_WEIGHT,
    HYBRID_RETRIEVAL,
    RRF_K,
    lexical_search,
    rrf_fuse,
)
from .utils import get_vector_store, today, PROPERTY_ID

# Retrieval budget. Sized by tracing the chunk that actually contains an answer
# through the cap and the reranker.
RETRIEVAL_K = int(os.getenv("RETRIEVAL_K", "20"))          # candidates after the cap
RETRIEVAL_FETCH_K = int(os.getenv("RETRIEVAL_FETCH_K", "40"))  # raw pull per arm
RETRIEVAL_PER_DOC = int(os.getenv("RETRIEVAL_PER_DOC", "5"))   # cap per document
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "8"))   # what the model actually sees

REDIS_URL = os.getenv("REDIS_URL")
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

# Redis semantic caching (default is 0.1). A loose value such as 0.98 accepts almost 
# anything and will serve one question's answer to a different question.
CACHE_DISTANCE_THRESHOLD = float(os.getenv("CACHE_DISTANCE_THRESHOLD", "0.05"))

set_llm_cache(
    RedisSemanticCache(
        redis_url=REDIS_URL,
        embeddings=embeddings,
        distance_threshold=CACHE_DISTANCE_THRESHOLD,
    )
)

SYSTEM = """You are a grounded assistant for residents of a rental property.

Answer from the provided context. Never infer, and never fill a gap from general
knowledge about tenancy law.

Quote specific figures, deadlines and notice periods verbatim. Do not round, restate
or summarise a number.

Cite the document you used by title and version, exactly as it appears in the
"Source:" line of each context block.

The context has already been filtered to the documents in force on {as_of}. Treat it
as current: do not hedge about whether a newer version might exist, and do not
speculate about future changes.

Setting out what a document says is your job, including when the document is about
notice periods, protections or obligations - that is reporting, not advising. What
you must not do is apply it to the resident's situation, tell them what they should
do, predict an outcome, or characterise anything as legal or illegal. Describe the
document and let them draw the conclusion.

Only if the context genuinely does not contain the answer, reply with exactly
"I don't know." and nothing else - no apology, no partial answer. The application
detects that phrase and offers the resident a route to a person. It is the last
resort, not a way to avoid a sensitive topic: if the context covers the question,
answer it.
"""

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM),
    ("user", "Question:\n{input}\n\nContext:\n{context}"),
])


def currency_filter(
    as_of: date,
    property_id: str = PROPERTY_ID,
    category: str | None = None,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Documents in force on `as_of`, optionally narrowed further.

    A document is in force when effective_from <= as_of <= effective_to_eff.
    effective_to_eff carries the 9999-12-31 sentinel for open-ended documents, so
    this one range test covers them without a NULL branch.

    Note it is NOT filtering on is_superseded or is_latest_version. Those are
    structural, and they disagree with currency whenever a newer version is signed
    but not yet effective.

    Filter construction is isolated here because the langchain_postgres v2 operator
    syntax is the one part of this file that has to match the library exactly; if it
    needs adjusting, this is the only place to change.

    This has a twin: hybrid.currency_sql() states the same rule in raw SQL for the
    lexical arm, which cannot consume a langchain filter dict. The two must agree.
    Change one and you have to change the other, then run both verify scripts.
    """
    clauses: list[dict[str, Any]] = [
        {"property_id": {"$eq": property_id}},
        {"effective_from": {"$lte": as_of}},
        {"effective_to_eff": {"$gte": as_of}},
    ]
    if category:
        clauses.append({"category": {"$eq": category}})
    if doc_type:
        clauses.append({"doc_type": {"$eq": doc_type}})
    return {"$and": clauses}


# Each context block is prefixed with its own citation so the model can attribute
# per fact, rather than guessing which of several sources a number came from.
#
# Only keys present on EVERY chunk may appear here - a missing key raises at format
# time, which is why these are always-present strings.
DOCUMENT_PROMPT = PromptTemplate.from_template(
    "Source: {citation}{section_ref}{template_note}\n{page_content}"
)


async def fetch_candidates(
    store: Any,
    query: str,
    as_of: date,
    *,
    limit: int = RETRIEVAL_FETCH_K,
    category: str | None = None,
    doc_type: str | None = None,
) -> list[Document]:
    """The raw candidate set for a question: both retrieval arms, fused.

    Dense and lexical run concurrently - they are two independent round trips to
    two different connections, so the pair costs about what the slower one costs
    alone, and the dense arm has an embedding API call in front of it anyway.

    Both arms are restricted to the documents in force on `as_of`, by two spellings
    of the same predicate: currency_filter() below for the dense arm, and
    hybrid.currency_sql() for the lexical one. Adding recall without carrying the
    date filter across would reintroduce exactly the failure this pipeline exists to
    prevent - see the warning on currency_sql().

    With HYBRID_RETRIEVAL false this is the dense arm alone, byte for byte the
    pipeline as it was before fusion existed. That is what makes the comparison a
    config change rather than a revert.

    Exposed rather than inlined into DiverseRetriever so scripts/verify_hybrid_currency.py
    checks the candidate set the pipeline actually builds.
    """
    dense_filter = currency_filter(as_of, category=category, doc_type=doc_type)
    if not HYBRID_RETRIEVAL:
        return await store.asimilarity_search(query, k=limit, filter=dense_filter)

    dense, lexical = await asyncio.gather(
        store.asimilarity_search(query, k=limit, filter=dense_filter),
        lexical_search(
            query, as_of, limit=limit, category=category, doc_type=doc_type
        ),
    )
    return rrf_fuse(
        [(dense, HYBRID_DENSE_WEIGHT), (lexical, HYBRID_LEXICAL_WEIGHT)],
        rrf_k=RRF_K,
    )


class DiverseRetriever(BaseRetriever):
    """Hybrid search with a cap on how many chunks any one document may contribute.

    Plain similarity search floods the candidate set from whichever document is
    closest overall: measured on this corpus, no query returned more than 4 distinct
    doc_types in its top 10, and "emergency maintenance phone number" spent five of
    its top six slots on maintenance_policy while the document that actually holds
    the number ranked sixth.

    So over-fetch, then keep at most `per_doc` chunks per document. The reranker
    downstream is good at ordering a candidate set but cannot recover a document
    that never made it into one.

    The cap runs after fusion rather than per arm, so a document the lexical arm
    found is subject to the same budget as one the dense arm found, and the reranker
    sees a single set rather than two it has to reconcile.

    It takes as_of/category/doc_type rather than a prepared filter because the two
    arms need the same restriction expressed two different ways; handing it one of
    the two spellings would leave the other to be built somewhere else.
    """

    store: Any
    as_of: date
    category: str | None
    doc_type: str | None
    k: int
    fetch_k: int
    per_doc: int

    model_config = ConfigDict(arbitrary_types_allowed=True)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        candidates = await fetch_candidates(
            self.store,
            query,
            self.as_of,
            limit=self.fetch_k,
            category=self.category,
            doc_type=self.doc_type,
        )

        taken: dict[str, int] = {}
        kept: list[Document] = []
        for doc in candidates:
            doc_id = doc.metadata.get("doc_id")
            if taken.get(doc_id, 0) >= self.per_doc:
                continue
            taken[doc_id] = taken.get(doc_id, 0) + 1
            kept.append(doc)
            if len(kept) >= self.k:
                break
        return kept

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        raise NotImplementedError("DiverseRetriever is async-only; use ainvoke")


async def _build_chain(
    as_of: date,
    category: str | None = None,
    doc_type: str | None = None,
):
    store = await get_vector_store()
    base_retriever = DiverseRetriever(
        store=store,
        as_of=as_of,
        category=category,
        doc_type=doc_type,
        k=RETRIEVAL_K,
        fetch_k=RETRIEVAL_FETCH_K,
        per_doc=RETRIEVAL_PER_DOC,
    )
    retriever = ContextualCompressionRetriever(
        base_retriever=base_retriever,
        base_compressor=CohereRerank(
            top_n=RERANK_TOP_N,
            model="rerank-multilingual-v3.0",
        ),
    )
    llm = ChatOpenAI(model="gpt-4o-mini")
    doc_chain = create_stuff_documents_chain(
        llm,
        PROMPT,
        document_prompt=DOCUMENT_PROMPT,
        document_separator="\n\n---\n\n",
    )
    return create_retrieval_chain(retriever, doc_chain)


async def _contact_block(as_of: date, route: str) -> dict | None:
    """Pull the relevant contact details out of the Property Fact Sheet, verbatim.

    Quoted rather than paraphrased and never hardcoded: an emergency number that has
    drifted from the documents is worse than no number, and a model rewording a
    phone number is a risk with no upside.
    """
    store = await get_vector_store()
    docs = await store.asimilarity_search(
        escalation.CONTACT_QUERY[route],
        k=1,
        filter=currency_filter(as_of, doc_type="property_fact_sheet"),
    )
    if not docs:
        return None
    d = docs[0]
    return {
        "text": d.page_content.strip(),
        "source": f"{d.metadata.get('citation')}{d.metadata.get('section_ref') or ''}",
    }


async def _escalation(route: str, as_of: date, sources: list[dict] | None = None) -> dict:
    block = await _contact_block(as_of, route)
    return {
        "reason": route,
        "message": escalation.MESSAGE[route],
        "contact": block,
        "flagged_sources": sources or [],
    }


async def answer_with_docs_async(
    question: str,
    as_of: date | None = None,
    category: str | None = None,
    doc_type: str | None = None,
) -> dict:
    """Answer a question against the documents in force on `as_of` (default today)."""
    as_of = as_of or today()

    # Guardrail ahead of retrieval: an urgent question gets the contact details
    # straight from the fact sheet, without waiting on retrieval and generation.
    # Ex: Answering "what does the policy say about flooding" to someone whose unit
    # is flooding is the wrong response even when it is factually correct.
    route = escalation.classify_question(question)
    if route in escalation.URGENT:
        esc = await _escalation(route, as_of)
        contact = esc["contact"]
        return {
            "answer": contact["text"] if contact else
                      "Contact the management office immediately.",
            "sources": [{"citation": contact["source"]}] if contact else [],
            "contexts": [contact["text"]] if contact else [],
            "as_of": as_of.isoformat(),
            "disposition": "escalate",
            "escalation": esc,
        }

    chain = await _build_chain(as_of, category=category, doc_type=doc_type)
    result = await chain.ainvoke({"input": question, "as_of": as_of.isoformat()})

    docs: list[Document] = result["context"]

    # Sources are structured rather than bare paths: a resident needs to know which
    # version of a policy an answer came from, not which file it lived in.
    seen: set[str] = set()
    sources = []
    for d in docs:
        doc_id = d.metadata.get("doc_id")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        sources.append({
            "citation": d.metadata.get("citation"),
            "section": (d.metadata.get("section_ref") or "").strip() or None,
            "doc_type": d.metadata.get("doc_type"),
            "category": d.metadata.get("category"),
            "version": d.metadata.get("version"),
            "effective_from": d.metadata.get("effective_from_iso"),
            "effective_to": d.metadata.get("effective_to_iso"),
            "path": d.metadata.get("source"),
            "is_template": d.metadata.get("is_template"),
        })

    answer: str = result["answer"]

    # Post-retrieval routes, in priority order.
    #
    # "I don't know" - Nothing retrieved at all, or the model declining to answer, 
    # both mean the documents do not cover the question.
    #
    # "Escalate" - A document marked disposition_hint "escalate" is one management handles
    # directly: the answer is still shown, because it is accurate, but it is not
    # presented as the last word. Only when the answer cites it - being retrieved
    # alongside an unrelated answer is not enough. See `escalation.cited_flagged`.
    flagged = escalation.cited_flagged(answer, [d.metadata for d in docs])

    route: str | None = None
    if not docs or escalation.looks_unanswered(answer):
        route = escalation.NO_ANSWER
    elif flagged:
        route = escalation.FLAGGED_DOCUMENT

    payload = {
        "answer": answer,
        "sources": sources,
        "contexts": [format_document(d, DOCUMENT_PROMPT) for d in docs],
        "as_of": as_of.isoformat(),
        "disposition": "escalate" if route else "answer",
        "escalation": await _escalation(route, as_of, flagged) if route else None,
    }
    return payload
