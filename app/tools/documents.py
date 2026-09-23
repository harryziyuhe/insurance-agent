from app.fixtures_data import DOCUMENT_GUIDELINE

# Fixture keys are wordier than what a caller will actually say, or than
# claim["documents_needed"] entries ("original pathology report" vs.
# "pathology report") — map each fixture key to the shorter phrases that
# should trigger it.
_DOC_CORE_TERMS = {
    "original pathology report": ["pathology report", "pathology"],
    "treating provider office note": ["office note"],
    "repair estimate": ["repair estimate", "estimate"],
    "supplemental accident scene photos": [
        "accident scene photos",
        "scene photos",
        "accident photos",
    ],
}


def _match_doc_key(text: str) -> str | None:
    text = text.lower()
    for doc_key, core_terms in _DOC_CORE_TERMS.items():
        if any(term in text for term in core_terms):
            return doc_key
    return None


def _find_mentioned_document(
    user_text: str, mentioned_document: str | None = None
) -> str | None:
    if mentioned_document and (matched := _match_doc_key(mentioned_document)):
        return matched
    return _match_doc_key(user_text)


def _fill_template(text: str, claim: dict) -> str:
    documents_needed = claim.get("documents_needed") or []
    settings = DOCUMENT_GUIDELINE["claim_followup_settings"]
    return text.format(
        case_id=claim["case_id"],
        documents=", ".join(documents_needed)
        if documents_needed
        else "the requested items",
        average_processing_time_after_submission=(
            settings["average_processing_time_after_submission"]["en"]
        ),
    )


def get_document_guideline(
    claim: dict,
    user_text: str,
    intent: str,
    mentioned_document: str | None = None,
    lacks_document: bool = False,
) -> str:
    doc_guidance = DOCUMENT_GUIDELINE["document_guidance"]
    doc_alt_guidance = DOCUMENT_GUIDELINE["document_alternative_guidance"]
    followup_guidance = DOCUMENT_GUIDELINE["claim_followup_guidance"]
    followup_fallback = DOCUMENT_GUIDELINE["claim_followup_fallback"]["en"]

    requires_documents = bool(claim.get("documents_needed"))

    doc_key = _find_mentioned_document(user_text, mentioned_document)
    if doc_key:
        if lacks_document:
            alt = doc_alt_guidance.get(doc_key, doc_alt_guidance["default"])
            return _fill_template(alt["en"], claim)
        if doc_key in doc_guidance:
            parts = []
            case_type_guide = DOCUMENT_GUIDELINE["case_type_guidance"].get(
                claim["case_type"]
            )
            if case_type_guide:
                parts.append(case_type_guide["en"])
            parts.append(doc_guidance[doc_key]["en"])
            return _fill_template(" ".join(parts), claim)

    text = user_text.lower()
    for entry in followup_guidance:
        if any(pattern in text for pattern in entry.get("match_any", [])):
            return _fill_template(entry["en"], claim)

    for entry in followup_guidance:
        if entry.get("match_any"):
            continue
        if (
            intent in entry.get("intent_hints", [])
            and entry.get("requires_documents") == requires_documents
        ):
            return _fill_template(entry["en"], claim)

    return _fill_template(followup_fallback, claim)
