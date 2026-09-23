import re
from dataclasses import dataclass, field


@dataclass
class ExtractedTurn:
    claimed_fields: dict = field(default_factory=dict)
    intent_hint: str | None = None
    caller_role: str | None = None  # "self" | "representative" | None
    rep_relationship: str | None = None
    yes_no: str | None = None
    mentioned_document: str | None = None
    intent_category: str | None = (
        None  # denial_question | document_submission | status_inquiry | next_steps | general_claim_question
    )
    lacks_document: bool = False
    wants_different_case: bool = False
    is_done: bool = False
    in_scope: bool = True
    emotion: str = "neutral"


_NAME_RE = re.compile(
    r"(?:my name is|this is)\s+([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)+)",
    re.IGNORECASE,
)
_DOB_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_POLICY_RE = re.compile(r"\b(POL-\d+)\b", re.IGNORECASE)
_SSN4_RE = re.compile(r"(?:ssn|social security|id)[^\d]{0,20}(\d{4})\b", re.IGNORECASE)
_LAST_FOUR_RE = re.compile(r"last\s*(?:four|4)\D{0,10}(\d{4})", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(\+\d[\d\-\s]{8,14}\d)")
_INTENT_RE = re.compile(
    r"calling(?:\s+\w+){0,3}\s+(?:about|regarding)\s+(.+?)(?:\.\s|\.$|,\s*(?:dob|my dob)|$)",
    re.IGNORECASE,
)
_REP_RE = re.compile(
    r"\b(on behalf of|calling for|i'?m (?:her|his|their) (son|daughter|spouse|husband|wife))\b",
    re.IGNORECASE,
)


def regex_fallback_extract(user_text: str) -> ExtractedTurn:
    out = ExtractedTurn()

    if m := _NAME_RE.search(user_text):
        out.claimed_fields["full_name"] = m.group(1)

    if m := _DOB_RE.search(user_text):
        out.claimed_fields["dob"] = m.group(1)

    if m := _POLICY_RE.search(user_text):
        out.claimed_fields["policy_number"] = m.group(1).upper()

    if (m := _LAST_FOUR_RE.search(user_text)) or (m := _SSN4_RE.search(user_text)):
        out.claimed_fields["id_last4"] = m.group(1)

    if m := _EMAIL_RE.search(user_text):
        out.claimed_fields["email"] = m.group(0)

    if m := _PHONE_RE.search(user_text):
        out.claimed_fields["phone"] = m.group(1)

    if m := _INTENT_RE.search(user_text):
        out.intent_hint = m.group(1).strip().rstrip(".")

    if re.search(r"\bi'?m the policyholder\b", user_text, re.IGNORECASE):
        out.caller_role = "self"
    elif m := _REP_RE.search(user_text):
        out.caller_role = "representative"
        out.rep_relationship = m.group(2) if m.group(2) else None

    return out
