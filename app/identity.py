import re
from dataclasses import dataclass, field
from datetime import datetime

from app.fixtures_data import POLICYHOLDERS, REPRESENTATIVES

_DOB_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y%m%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%d %B %Y",
    "%Y %B %d",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %b %Y",
    "%Y %b %d",
]


_DATE_SUBSTRING_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\d{8}\b|"
    r"[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]+\.?\s+\d{4}|\d{4}\s+[A-Za-z]+\.?\s+\d{1,2}"
)


def _parse_date(s: str | None):
    if not s:
        return None
    text = re.sub(r"\s+", " ", s.strip())
    for candidate in (text, *(m.group(0) for m in _DATE_SUBSTRING_RE.finditer(text))):
        for fmt in _DOB_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


PII_FIELDS = ["full_name", "dob", "phone", "email", "id_last4"]


def _norm(s: str | None) -> str | None:
    if s is None:
        return None
    return re.sub(r"\s+", " ", s.strip().lower())


def _digits(s: str | None) -> str | None:
    if s is None:
        return None
    return re.sub(r"\D", "", s)


def _name_matches(claimed: str, record: dict) -> bool:
    claimed_n = _norm(claimed)
    candidates = [record.get("name")] + record.get("name_aliases", [])
    return any(claimed_n == _norm(c) for c in candidates if c)


def _phone_matches(claimed: str, record: dict) -> bool:
    claimed_d = _digits(claimed)
    if not claimed_d:
        return False
    candidates = [record.get("phone")] + record.get("phone_aliases", [])
    return any(claimed_d and claimed_d in _digits(c) for c in candidates if c)


def _email_matches(claimed: str, record: dict) -> bool:
    claimed_n = _norm(claimed)
    candidates = [record.get("email")] + record.get("email_aliases", [])
    return any(claimed_n == _norm(c) for c in candidates if c)


def _dob_matches(claimed: str, record: dict) -> bool:
    claimed_date = _parse_date(claimed)
    record_date = _parse_date(record.get("dob"))
    if claimed_date and record_date:
        return claimed_date == record_date
    return _norm(claimed) == _norm(record.get("dob"))


def _id_last4_matches(claimed: str, record: dict) -> bool:
    return _digits(claimed) == _digits(record.get("id_last4"))


_FIELD_MATCHERS = {
    "full_name": _name_matches,
    "phone": _phone_matches,
    "email": _email_matches,
    "dob": _dob_matches,
    "id_last4": _id_last4_matches,
}


@dataclass
class MatchResult:
    party_id: str | None = None
    matched_fields: list[str] = field(default_factory=list)
    verified: bool = False
    ambiguous: bool = False


def match_identity(claimed_fields: dict) -> MatchResult:
    """claimed_fields: subset of {full_name, dob, phone, email, id_last4, policy_number}
    accumulated so far this session (see SessionState.identity.claimed_fields).
    """
    scored: list[tuple[str, list[str]]] = []
    for record in POLICYHOLDERS:
        matched = []
        if claimed_fields.get("policy_number") and _norm(
            claimed_fields["policy_number"]
        ) != _norm(record.get("policy_number")):
            continue
        for field_name in PII_FIELDS:
            claimed_value = claimed_fields.get(field_name)
            if not claimed_value:
                continue
            if _FIELD_MATCHERS[field_name](claimed_value, record):
                matched.append(field_name)
        if matched:
            scored.append((record["party_id"], matched))

    if not scored:
        return MatchResult()

    scored.sort(key=lambda x: len(x[1]), reverse=True)
    best_party_id, best_matched = scored[0]
    best_count = len(best_matched)

    if best_count < 3:
        return MatchResult(party_id=best_party_id, matched_fields=best_matched)

    tied = [pid for pid, m in scored if len(m) == best_count]
    if len(tied) > 1:
        return MatchResult(party_id=None, matched_fields=best_matched, ambiguous=True)

    return MatchResult(
        party_id=best_party_id, matched_fields=best_matched, verified=True
    )


@dataclass
class RepresentativeMatch:
    matched: bool = False
    buyer_party_id: str | None = None


def match_representative(
    rep_name: str, relationship: str | None, buyer_name: str | None
) -> RepresentativeMatch:
    rep_name_n = _norm(rep_name)
    for row in REPRESENTATIVES:
        if _norm(row["rep_name"]) != rep_name_n:
            continue
        if relationship and _norm(relationship) != _norm(row["relationship"]):
            continue
        if buyer_name and _norm(buyer_name) != _norm(row["buyer_name"]):
            continue
        return RepresentativeMatch(matched=True, buyer_party_id=row["buyer_party_id"])
    return RepresentativeMatch()
