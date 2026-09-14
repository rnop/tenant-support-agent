"""
verify_escalation.py: prove escalation routes questions correctly

classify_question() decides, before any retrieval or model call, whether a question
goes straight to a contact banner. Both failure directions matter and neither shows
up in an ordinary answer-quality eval:

  - a false negative leaves someone reporting smoke waiting on retrieval
  - a false positive shows the emergency line to someone asking a policy question

The word "smoke" is the sharpest case - it is both an emergency and the subject of
the smoke-free policy - so it has the most rows.

After retrieval, cited_flagged() decides whether an answer rests on a document the
office handles directly. Those cases check that retrieving such a document alongside
an unrelated answer does not escalate, and that a missing citation does not drop an
escalation.

Pure functions, no database, no keys:

    python scripts/verify_escalation.py
    docker compose exec app python scripts/verify_escalation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.escalation import (                 # noqa: E402
    cited_flagged,
    classify_question,
    IMMEDIATE_DANGER as DANGER,
    MAINTENANCE_EMERGENCY as MAINT,
    SAFETY_CONCERN as SAFETY,
)

# (question, expected route or None for an ordinary question)
CASES = [
    # smoke as policy - must not route
    ("Can I smoke on my balcony?", None),
    ("Am I allowed to smoke in the courtyard?", None),
    ("Is it okay to smoke weed inside my unit?", None),
    ("Where can guests smoke?", None),
    ("Could residents smoke on the patio before 2025?", None),
    ("Is Maple Court smoke-free?", None),
    ("What does the smoke free addendum cover?", None),
    ("What is the smoking policy?", None),
    ("Who replaces the smoke detector batteries?", None),
    ("My neighbour's cigarette smoke drifts into my unit", None),
    ("Do I pay for smoke odour remediation when I move out?", None),

    # smoke as emergency - must route
    ("There's smoke coming out of my oven", MAINT),
    ("I smell smoke in the hallway", MAINT),
    ("I can see smoke under the door", MAINT),
    ("You can smell smoke from the stairwell", MAINT),
    ("My apartment is filling with smoke", MAINT),
    ("smoke everywhere in the laundry room", MAINT),
    ("The smoke alarm is going off and I can see smoke", MAINT),

    # the rest of the maintenance list
    ("There is a fire in the trash enclosure", MAINT),
    ("burning smell from the outlet", MAINT),
    ("smoking outlet in the kitchen", MAINT),
    ("I smell gas in my kitchen", MAINT),
    ("Water is pouring through my ceiling from upstairs", MAINT),
    ("We have no heat and it's freezing", MAINT),

    # other routes
    ("Someone is hurt and bleeding in the stairwell", DANGER),
    ("My neighbour keeps threatening me in the hallway", SAFETY),

    # ordinary questions
    ("What are the quiet hours?", None),
    ("Can the landlord take back approval for my pet?", None),
]

# cited_flagged(): which flagged documents an answer actually rests on.
_JCRN = {"title": "Just Cause & Relocation Notice — Maple Court",
         "citation": "Just Cause & Relocation Notice — Maple Court (v1, effective 1 January 2025)",
         "disposition_hint": "escalate"}
_MOVE = {"title": "Move-In & Move-Out Procedures — Maple Court",
         "citation": "Move-In & Move-Out Procedures — Maple Court (v1, effective 1 January 2026)",
         "disposition_hint": "answer"}
_PET = {"title": "Pet Addendum (Template) — Maple Court",
        "citation": "Pet Addendum (Template) — Maple Court (v1, effective 1 April 2025)",
        "disposition_hint": "answer"}

# (label, answer, retrieved docs, expected flagged citations)
CITATION_CASES = [
    ("cites only an unflagged doc",
     "Moving is permitted 8:00 a.m. to 8:00 p.m.\n\nSource: Move-In & Move-Out Procedures — Maple Court (v1)",
     [_MOVE, _JCRN], []),
    ("cites the flagged doc",
     "Relocation assistance is one month's rent.\n\nSource: Just Cause & Relocation Notice — Maple Court (v1)",
     [_JCRN, _MOVE], [_JCRN["citation"]]),
    ("cites both",
     "Per the Move-In & Move-Out Procedures and the Just Cause & Relocation Notice, ...",
     [_MOVE, _JCRN], [_JCRN["citation"]]),
    ("'and' for '&', inline, trailing punctuation",
     "Under the just cause and relocation notice, protections apply after twelve months.",
     [_JCRN], [_JCRN["citation"]]),
    ("(Template) dropped by the model",
     "The Pet Addendum says approval may be withdrawn.",
     [_PET, _JCRN], []),
    ("no citation at all - unknown basis, keep escalation",
     "Relocation assistance is one month's rent.",
     [_MOVE, _JCRN], [_JCRN["citation"]]),
    ("nothing flagged retrieved",
     "Moving is permitted 8:00 a.m. to 8:00 p.m.",
     [_MOVE], []),
]

# Wrong today, recorded so they are not rediscovered. Reported, never failed; when one
# starts passing, move it into CASES.
KNOWN_GAPS = [
    ("My outlet is smoking", MAINT),            # only "smoking outlet" is matched
    ("Is there a fire extinguisher in the laundry room?", None),  # bare \bfire\b
]


def main() -> int:
    failures = 0
    for question, want in CASES:
        got = classify_question(question)
        ok = got == want
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} want {str(want):22} got {str(got):22} {question}")

    print("\ncited_flagged:")
    for label, answer, docs, want in CITATION_CASES:
        got = cited_flagged(answer, docs)
        ok = got == want
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {'escalate' if want else 'answer':8} {label}"
              + ("" if ok else f"  (got {got})"))

    print("\nknown gaps (not failing):")
    for question, want in KNOWN_GAPS:
        got = classify_question(question)
        state = "now passes - move to CASES" if got == want else f"still {got}"
        print(f"  gap  want {str(want):22} {state:26} {question}")

    total = len(CASES) + len(CITATION_CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
