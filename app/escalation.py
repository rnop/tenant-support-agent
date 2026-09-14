"""
escalation.py: decide when a question should go to a human rather than an answer

Three routes out of the RAG pipeline, in priority order:

  IMMEDIATE_DANGER      -> emergency services, before anything else
  MAINTENANCE_EMERGENCY -> the 24-hour maintenance line
  SAFETY_CONCERN        -> the management office

Two that are decided after retrieval rather than from the question:

  FLAGGED_DOCUMENT      -> the answer rests on a document marked disposition_hint
                           "escalate", which the office handles directly
  NO_ANSWER             -> the documents do not cover it

Why pattern matching rather than an LLM classifier?
    This is a guardrail, and it runs BEFORE retrieval so that someone typing "I smell
    gas" gets the emergency number without waiting on a model call. It has to be fast,
    deterministic and testable. An LLM classifier is better at paraphrase and would be
    a reasonable second layer, but it should not be the only thing between a resident
    and the emergency line.

Emergency list in the Property Fact Sheet ("gas odour, smoke or fire, flooding, no heat, carbon monoxide, no
water, an electrical hazard, a sewage backup, or a unit left unsecured") and the
safety clause in the Building Rules. If those documents change, revisit this list.
"""

from __future__ import annotations

import re

IMMEDIATE_DANGER = "immediate_danger"
MAINTENANCE_EMERGENCY = "maintenance_emergency"
SAFETY_CONCERN = "safety_concern"
FLAGGED_DOCUMENT = "flagged_document"
NO_ANSWER = "no_answer"


def _any(*alternatives: str) -> re.Pattern:
    return re.compile("|".join(alternatives), re.I)


# Checked first: people before property.
_DANGER = _any(
    r"\b911\b", r"\bambulance\b", r"\bparamedic",
    r"\b(someone|somebody|I|we|he|she|they)\s+(is|am|are|was|were)\s+(hurt|injured|bleeding|unconscious|trapped)\b",
    r"\bcan.?t breathe\b", r"\bheart attack\b", r"\boverdose\b",
    r"\bbeing attacked\b", r"\bassaulted?\b",
)

# The Property Fact Sheet's emergency maintenance list.
_MAINTENANCE = _any(
    # gas / fire / CO
    r"\bgas\s*(leak|smell|odou?r)\b", r"\bsmell(s|ing)?\s+(of\s+)?gas\b",
    r"\bcarbon monoxide\b", r"\bco\s+alarm\b",
    r"\bfire\b", r"\bburning smell\b",
    # water
    r"\bflood(ing|ed)?\b", r"\bburst pipe\b", r"\bpipe burst\b",
    r"\bwater (is )?(pouring|gushing|everywhere)\b", r"\bceiling (is )?leaking\b",
    r"\bno (hot )?water\b", r"\bsewage\b", r"\bsewer backup\b", r"\btoilet overflow",
    # heat / power
    r"\bno heat\b", r"\bheat(ing|er)? (is )?(not working|broken|out|dead)\b",
    r"\belectrical (hazard|fire)\b", r"\bsparks?\b", r"\bexposed wir",
    r"\bgot (an )?electric shock\b", r"\bsmoking outlet\b",
    # security
    r"\bbroke?n (lock|door|window)\b", r"\bdoor (won.?t|will not) lock\b",
    r"\bbroke in\b", r"\bbreak.?in\b", r"\bunit .*unsecured\b",
)

# "smoke" is both an emergency ("smoke coming from the oven") and the subject of the
# smoke-free policy ("can I smoke on my balcony?"). Held apart from _MAINTENANCE so
# the policy senses can be excluded: the word routes unless one of them is present.
# Excluding known policy phrasings, rather than listing emergency phrasings, keeps an
# unanticipated way of describing real smoke on the emergency side.
_SMOKE = _any(r"\bsmoke\b")
_SMOKE_NOT_EMERGENCY = _any(
    # smoking as the activity: "can I smoke", "allowed to smoke", "where can guests smoke"
    # The optional word is a subject only: "can I smoke" is policy, "can see smoke" is not.
    r"\b(can|could|may|to)\s+((i|we|you|they|he|she|guests?|residents?|people|visitors?|tenants?)\s+)?smoke\b",
    r"\bsmoke\W*free\b",
    # detector upkeep, not activation
    r"\bsmoke (detector|alarm)s?\W+(\w+\W+)?(batter|replac|install|test|check)",
    # someone else's smoking drifting in: a Smoke-Free Addendum matter for the office
    r"\b(cigarette|cigar|cannabis|weed|marijuana|tobacco|second.?hand)\s+smoke\b",
    r"\bsmoke (drift|odou?r|residue|damage)\b",
)

# Building Rules: safety, harassment, threatening behaviour.
_SAFETY = _any(
    r"\bharass(ed|ing|ment)?\b", r"\bthreat(en(ed|ing))?\b",
    r"\bstalk(ed|ing)\b", r"\bintimidat", r"\bunsafe\b",
    r"\bdomestic (violence|abuse)\b", r"\bafraid (of|for)\b",
    r"\bneighbou?r .*(threat|harass|aggressive)",
)


def classify_question(question: str) -> str | None:
    """Urgency route for a question, or None for an ordinary one.

    Order matters: danger to people outranks a maintenance emergency, which
    outranks a safety concern.
    """
    if _DANGER.search(question):
        return IMMEDIATE_DANGER
    if _MAINTENANCE.search(question) or (
        _SMOKE.search(question) and not _SMOKE_NOT_EMERGENCY.search(question)
    ):
        return MAINTENANCE_EMERGENCY
    if _SAFETY.search(question):
        return SAFETY_CONCERN
    return None


# What to retrieve from the corpus for each route. The contact details are never
# hardcoded here - they are quoted from the Property Fact Sheet, so they cannot go
# stale against the documents, and the resident sees the wording the office wrote.
CONTACT_QUERY = {
    IMMEDIATE_DANGER: "emergency maintenance 24-hour line immediate danger 911",
    MAINTENANCE_EMERGENCY: "emergency maintenance 24-hour line gas flooding no heat",
    SAFETY_CONCERN: "management office hours telephone email contact",
    FLAGGED_DOCUMENT: "management office hours telephone email contact",
    NO_ANSWER: "management office hours telephone email contact",
}

# Shown above the answer. Urgent routes instruct; the rest offer, because the
# resident may simply want the information.
MESSAGE = {
    IMMEDIATE_DANGER:
        "If anyone is in immediate danger, call 911 first. "
        "Do you want to escalate this to the 24-hour maintenance line?",
    MAINTENANCE_EMERGENCY:
        "This sounds like an emergency the 24-hour maintenance line handles, "
        "not something to wait on. Do you want to escalate this to maintenance?",
    SAFETY_CONCERN:
        "A concern about safety or another resident's behaviour goes to the "
        "management office directly. Do you want to escalate this to the landlord?",
    FLAGGED_DOCUMENT:
        "This touches a notice the office handles directly, and the documents alone "
        "may not settle it. Do you want to escalate this to the landlord?",
    NO_ANSWER:
        "The property documents do not cover this. Do you want to escalate this to "
        "maintenance or the landlord?",
}

# Routes that must not wait on the model: answered straight from the fact sheet.
URGENT = {IMMEDIATE_DANGER, MAINTENANCE_EMERGENCY, SAFETY_CONCERN}

# Phrases the model is told to use when the context does not answer the question.
_NO_ANSWER_MARKERS = (
    "i don't know",
    "i do not know",
    "i dont know",
)


def looks_unanswered(answer: str) -> bool:
    """Whether the model declined to answer.

    Detected from the text because the chain returns prose, not a structured
    verdict. The system prompt pins the exact wording to keep this reliable; if the
    prompt's wording changes, change these markers with it.
    """
    head = answer.strip().lower()[:120]
    return any(marker in head for marker in _NO_ANSWER_MARKERS)


def _words(text: str) -> str:
    """Lowercase, '&' as 'and', every run of non-alphanumerics a single space, padded
    so a containment test only matches whole words."""
    text = re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and "))
    return f" {text.strip()} "


def cites(answer: str, title: str) -> bool:
    """Whether the answer names the document with this title.

    Matched on the document's name - the part of "Pet Addendum (Template) — Maple
    Court" a model repeats when citing it - rather than the full citation string,
    which the model shortens and reformats. Punctuation, '&' vs 'and' and the
    (Template) marker are ignored.
    """
    name = re.sub(r"\(template\)", " ", title.split(" — ")[0], flags=re.I)
    name = _words(name)
    return name.strip() != "" and name in _words(answer)


def cited_flagged(answer: str, docs: list[dict]) -> list[str]:
    """Citations of the escalate-flagged documents this answer rests on.

    `docs` are retrieved chunk metadata dicts (title, citation, disposition_hint).

    Retrieval brings back more than the answer uses: "notice", "move out" and
    "office" are enough to pull the Just Cause & Relocation Notice into a question
    about moving hours. Escalating on retrieval alone put the landlord banner on
    answers that never touched that notice, so a flagged document counts only when
    the answer cites it - which the system prompt requires it to do.

    If the answer cites no retrieved document at all, what it rests on is unknown,
    and every flagged document retrieved counts: a missing citation must not be the
    thing that drops an escalation.
    """
    flagged = [d for d in docs if d.get("disposition_hint") == "escalate" and d.get("citation")]
    if not flagged:
        return []
    if any(cites(answer, d.get("title") or "") for d in docs):
        flagged = [d for d in flagged if cites(answer, d.get("title") or "")]
    return sorted({d["citation"] for d in flagged})
