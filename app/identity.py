"""Deterministic identity verification against fixtures/policyholders.json
and fixtures/representatives.json. See ARCHITECTURE.md §7.

This is intentionally plain code, not an LLM call: verification math must be
reproducible and auditable, and the phase controller (not the model) decides
when the >=3-field gate is satisfied.
"""
import re
from dataclasses import dataclass, field

from app.fixtures_data import POLICYHOLDERS, REPRESENTATIVES

# Fields that count toward the >=3-of-5 requirement. Policy number is accepted
# as a narrowing hint but does not count (see ARCHITECTURE.md §7).
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
    return _norm(claimed) == _norm(record.get("dob"))


def _id_last4_matches(claimed: str, record: dict) -> bool:
    # accepted regardless of whether record.id_type is ssn_last4 or
    # national_id_last4 — caller shouldn't need to know which one applies.
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
    ambiguous: bool = False  # multiple records tied at >=3 matches


def match_identity(claimed_fields: dict) -> MatchResult:
    """claimed_fields: subset of {full_name, dob, phone, email, id_last4, policy_number}
    accumulated so far this session (see SessionState.identity.claimed_fields).
    """
    scored: list[tuple[str, list[str]]] = []
    for record in POLICYHOLDERS:
        matched = []
        # policy_number narrows candidates but isn't a counted PII field
        if claimed_fields.get("policy_number") and _norm(
            claimed_fields["policy_number"]
        ) != _norm(record.get("policy_number")):
            # explicit mismatch on policy number rules this record out entirely
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
        return MatchResult(
            party_id=None, matched_fields=best_matched, ambiguous=True
        )

    return MatchResult(
        party_id=best_party_id, matched_fields=best_matched, verified=True
    )


@dataclass
class RepresentativeMatch:
    matched: bool = False
    buyer_party_id: str | None = None


def match_representative(rep_name: str, relationship: str | None, buyer_name: str | None) -> RepresentativeMatch:
    """Checks the rep's claimed name/relationship/buyer against
    fixtures/representatives.json. See ARCHITECTURE.md §7.1.
    """
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
