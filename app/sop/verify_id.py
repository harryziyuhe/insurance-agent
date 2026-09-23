from app.extraction import ExtractedTurn
from app.identity import PII_FIELDS, match_identity, match_representative
from app.pipeline import respond
from app.session import Phase, SessionState

REQUIRED_MATCHES = 3
ALL_FIELDS_PROMPT = (
    "full name, date of birth, phone number, email, or the last 4 digits of your SSN/ID"
)

_FIELD_LABELS = {
    "full_name": "full name",
    "dob": "date of birth",
    "phone": "phone number",
    "email": "email",
    "id_last4": "last 4 digits of your SSN/ID",
}


def _labels(fields) -> str:
    return ", ".join(_FIELD_LABELS.get(f, f) for f in fields)


MAX_VERIFICATION_ATTEMPTS = 10


def handle(session: SessionState, extracted: ExtractedTurn) -> str:
    identity = session.identity

    # --- merge extracted signals into session state (never overwrite) ---
    for k, v in extracted.claimed_fields.items():
        identity.claimed_fields.setdefault(k, v)
        identity.claimed_fields[k] = v  # last-stated value wins per field

    if extracted.caller_role and not identity.caller_role:
        identity.caller_role = extracted.caller_role
    if extracted.rep_relationship and not identity.rep_relationship:
        identity.rep_relationship = extracted.rep_relationship

    if extracted.intent_hint and not session.memory.intent_hint:
        session.memory.intent_hint = extracted.intent_hint

    identity.verification_attempts += 1
    first_turn = identity.verification_attempts == 1

    # --- representative path (ARCHITECTURE.md §7.1) ---
    if identity.caller_role == "representative":
        rep_name = identity.claimed_fields.get("full_name")
        if rep_name and not identity.rep_name:
            identity.rep_name = rep_name
        # Even on the rep path, verification still runs against the
        # buyer/policyholder's PII fields the rep supplies.
        rep_match = None
        if identity.rep_name:
            rep_match = match_representative(
                identity.rep_name, identity.rep_relationship, buyer_name=None
            )
        result = match_identity(identity.claimed_fields)
        if result.verified and rep_match and rep_match.matched:
            identity.verified = True
            identity.party_id = rep_match.buyer_party_id or result.party_id
            identity.matched_fields = result.matched_fields
            session.phase = Phase.RESOLVE_INTENT
            return _verified_reply(session, first_turn)
        if identity.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
            return _escalate_reply(session, extracted.emotion)
        return _in_progress_reply(identity, result, extracted.emotion, first_turn)

    result = match_identity(identity.claimed_fields)
    identity.matched_fields = result.matched_fields

    if result.verified:
        identity.verified = True
        identity.party_id = result.party_id
        session.phase = Phase.RESOLVE_INTENT
        return _verified_reply(session, first_turn)

    if identity.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
        return _escalate_reply(session, extracted.emotion)

    if result.ambiguous:
        unprovided_fields = [f for f in PII_FIELDS if f not in identity.claimed_fields]
        greeting = _greeting_prefix(first_turn)
        next_action = (
            f"{greeting}tell the caller you have a couple of possible matches with what "
            f"they've given so far and ask them to also confirm their "
            f"{_labels(unprovided_fields) if unprovided_fields else ALL_FIELDS_PROMPT}"
        )
        return respond([], next_action, extracted.emotion)

    return _in_progress_reply(identity, result, extracted.emotion, first_turn)


def _greeting_prefix(first_turn: bool) -> str:
    return "Start with a warm, friendly greeting, then " if first_turn else ""


def _verified_reply(session: SessionState, first_turn: bool = False) -> str:
    facts_to_convey = []
    if session.memory.intent_hint:
        next_action = f"tell the caller they're verified and that you'll pick up their earlier mention of {session.memory.intent_hint}"
    else:
        next_action = (
            "tell the caller they're verified and ask for what they need help with"
        )

    return respond(facts_to_convey, _greeting_prefix(first_turn) + next_action)


def _in_progress_reply(identity, result, emotion, first_turn: bool = False) -> str:
    facts_to_convey = []

    have = len(result.matched_fields)
    wrong_fields = [
        f
        for f in PII_FIELDS
        if identity.claimed_fields.get(f) and f not in result.matched_fields
    ]

    unprovided_fields = [f for f in PII_FIELDS if f not in identity.claimed_fields]

    greeting = _greeting_prefix(first_turn)

    if not identity.claimed_fields:
        next_action = f"{greeting}tell the caller they need to have their identity verified. They need to provide at least {REQUIRED_MATCHES} of {ALL_FIELDS_PROMPT} information"

    elif wrong_fields:
        if unprovided_fields:
            alternative = f"provide a different piece of ID they haven't given yet, such as {_labels(unprovided_fields)}"
        else:
            alternative = "double-check that value, since they've already given every other accepted form of ID"
        next_action = (
            f"{greeting}tell the caller the {_labels(wrong_fields)} they gave doesn't match what's on file "
            f"({have} of {REQUIRED_MATCHES} pieces confirmed so far). Ask them to double-check that "
            f"value, or {alternative}"
        )

    else:
        next_action = (
            f"{greeting}tell the caller that you have {have} of the {REQUIRED_MATCHES} pieces of "
            f"identification needed ({_labels(result.matched_fields)} confirmed). Ask them to provide "
            f"one more, such as their {_labels(unprovided_fields)}"
        )

    return respond(facts_to_convey, next_action, emotion)


def _escalate_reply(session: SessionState, emotion: str) -> str:
    session.phase = Phase.HUMAN_HANDOFF
    next_action = (
        "apologize that verifying their identity is taking longer than it should, "
        "and let them know you're connecting them with a human representative who "
        "can verify them another way"
    )
    return respond([], next_action, emotion)
