"""RESOLVE_INTENT tool layer: claim lookup + hint-based matching + intent
classification (ARCHITECTURE.md §6.2, §11).

Kept deliberately simple/keyword-based for now — same spirit as the
extraction stub: prove the RESOLVE_INTENT control flow works, refine the
matching heuristics later.
"""
from app.fixtures_data import claims_by_party_id, claims_by_case_id

CASE_TYPES = ["healthcare", "dental", "auto"]
STATUS_WORDS = ["denied", "closed", "open", "pending", "approved"]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def list_claims(party_id: str) -> list[dict]:
    return claims_by_party_id().get(party_id, [])


def _created_month(claim: dict) -> int:
    # created_at is "YYYY-MM-DD"
    return int(claim["created_at"].split("-")[1])


def filter_claims(claims: list[dict], hint_text: str) -> list[dict]:
    """Narrows `claims` using keywords found in `hint_text`. Only applies a
    filter if it leaves at least one claim — a hint that doesn't match
    anything (typo, wrong month, etc.) should never zero out the candidate
    list, just get ignored for that filter.
    """
    hint = hint_text.lower()
    candidates = claims

    matched_types = [t for t in CASE_TYPES if t in hint]
    if matched_types:
        narrowed = [c for c in candidates if c["case_type"] in matched_types]
        if narrowed:
            candidates = narrowed

    matched_statuses = [s for s in STATUS_WORDS if s in hint]
    if matched_statuses:
        narrowed = [c for c in candidates if c["status"] in matched_statuses]
        if narrowed:
            candidates = narrowed

    matched_months = [num for name, num in _MONTHS.items() if name in hint]
    if matched_months:
        narrowed = [c for c in candidates if _created_month(c) in matched_months]
        if narrowed:
            candidates = narrowed

    return candidates


def classify_intent(text: str) -> str:
    """Maps free text to the intent categories used by
    required_document_guideline.json's `intent_hints`. TODO: this is a first
    pass — revisit keyword lists once real conversations are tested against
    it, and consider letting the LLM classify instead once extraction is
    swapped from the regex stub.
    """
    t = text.lower()
    if any(w in t for w in ["denied", "denial", "why was", "rejected"]):
        return "denial_question"
    if any(w in t for w in ["document", "upload", "submit", "paperwork", "file"]):
        return "document_submission"
    if any(w in t for w in ["status", "update", "where is", "progress"]):
        return "status_inquiry"
    if any(w in t for w in ["next step", "what do i need to do", "what should i do"]):
        return "next_steps"
    return "general_claim_question"

def get_claim(case_id: str) -> dict | None:
    matches = claims_by_case_id().get(case_id, {})
    return matches[0] if matches else None
