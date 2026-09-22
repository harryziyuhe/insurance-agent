"""Document-guidance lookup for PROCESS_CASE (ARCHITECTURE.md §6.3, §11),
backed by fixtures/required_document_guideline.json.

Lookup order, most specific to least specific:
  1. named document mentioned + "I don't have it" -> document_alternative_guidance
  2. named document mentioned, no such signal      -> document_guidance
  3. claim_followup_guidance's match_any patterns  (priority = fixture list order)
  4. claim_followup_guidance's intent_hints + requires_documents fallback
  5. claim_followup_fallback

Whatever text wins gets {case_id}/{documents}/{average_processing_time_after_submission}
filled in.
"""
from app.fixtures_data import DOCUMENT_GUIDELINE

_NO_DOCUMENT_PHRASES = [
    "don't have", "do not have", "dont have", "missing", "lost",
    "can't find", "cannot find", "haven't got", "have not got",
]

# Fixture keys are wordier than what a caller will actually say, or than
# claim["documents_needed"] entries ("original pathology report" vs.
# "pathology report") — map each fixture key to the shorter phrases that
# should trigger it.
_DOC_CORE_TERMS = {
    "original pathology report": ["pathology report", "pathology"],
    "treating provider office note": ["office note"],
    "repair estimate": ["repair estimate", "estimate"],
    "supplemental accident scene photos": [
        "accident scene photos", "scene photos", "accident photos",
    ],
}


def _find_mentioned_document(user_text: str) -> str | None:
    text = user_text.lower()
    for doc_key, core_terms in _DOC_CORE_TERMS.items():
        if any(term in text for term in core_terms):
            return doc_key
    return None


def _mentions_not_having_it(user_text: str) -> bool:
    text = user_text.lower()
    return any(phrase in text for phrase in _NO_DOCUMENT_PHRASES)


def _fill_template(text: str, claim: dict) -> str:
    documents_needed = claim.get("documents_needed") or []
    settings = DOCUMENT_GUIDELINE["claim_followup_settings"]
    return text.format(
        case_id=claim["case_id"],
        documents=", ".join(documents_needed) if documents_needed else "the requested items",
        average_processing_time_after_submission=(
            settings["average_processing_time_after_submission"]["en"]
        ),
    )


def get_document_guideline(claim: dict, user_text: str, intent: str) -> str:
    doc_guidance = DOCUMENT_GUIDELINE["document_guidance"]
    doc_alt_guidance = DOCUMENT_GUIDELINE["document_alternative_guidance"]
    followup_guidance = DOCUMENT_GUIDELINE["claim_followup_guidance"]
    followup_fallback = DOCUMENT_GUIDELINE["claim_followup_fallback"]["en"]

    requires_documents = bool(claim.get("documents_needed"))

    # 1/2. Named-document check first — most specific signal available.
    doc_key = _find_mentioned_document(user_text)
    if doc_key:
        if _mentions_not_having_it(user_text):
            alt = doc_alt_guidance.get(doc_key, doc_alt_guidance["default"])
            return _fill_template(alt["en"], claim)
        if doc_key in doc_guidance:
            parts = []
            case_type_guide = DOCUMENT_GUIDELINE["case_type_guidance"].get(claim["case_type"])
            if case_type_guide:
                parts.append(case_type_guide["en"])
            parts.append(doc_guidance[doc_key]["en"])
            return _fill_template(" ".join(parts), claim)
        # matched a core term but the fixture has no guidance entry for it —
        # fall through to the general router below

    # 3. match_any router — priority order matches the fixture's list order.
    text = user_text.lower()
    for entry in followup_guidance:
        if any(pattern in text for pattern in entry.get("match_any", [])):
            return _fill_template(entry["en"], claim)

    # 4. intent_hints + requires_documents fallback (entries with no
    #    match_any, e.g. "missing_required_material_alternatives").
    for entry in followup_guidance:
        if entry.get("match_any"):
            continue
        if intent in entry.get("intent_hints", []) and entry.get("requires_documents") == requires_documents:
            return _fill_template(entry["en"], claim)

    # 5. Global fallback.
    return _fill_template(followup_fallback, claim)
