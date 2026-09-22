"""VERIFY_ID phase controller (ARCHITECTURE.md §6.1 + §7).

Strict phase: this module is the only place allowed to decide verification,
and it never reads memory.intent_hint to act on it — only the pipeline (which
runs extraction before dispatch) writes to memory. That separation is what
makes "remembers the hint but doesn't act on it here" structural rather than
a prompting hope.
"""
from app.extraction import ExtractedTurn
from app.identity import match_identity, match_representative
from app.session import Phase, SessionState

REQUIRED_MATCHES = 3
ALL_FIELDS_PROMPT = (
    "full name, date of birth, phone number, email, or the last 4 digits of "
    "your SSN/ID"
)


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
            return _verified_reply(session)
        return _in_progress_reply(identity, result)

    # --- standard self-verification path ---
    result = match_identity(identity.claimed_fields)
    identity.matched_fields = result.matched_fields

    if result.verified:
        identity.verified = True
        identity.party_id = result.party_id
        session.phase = Phase.RESOLVE_INTENT
        return _verified_reply(session)

    if result.ambiguous:
        return (
            "I have a couple of possible matches with what you've given me so far. "
            f"Could you also confirm your {ALL_FIELDS_PROMPT}?"
        )

    return _in_progress_reply(identity, result)


def _verified_reply(session: SessionState) -> str:
    if session.memory.intent_hint:
        return (
            "Thanks, you're verified. I have a note that you're calling about "
            f"{session.memory.intent_hint} — let's pick up there."
        )
    return "Thanks, you're verified. What can I help you with today?"


def _in_progress_reply(identity, result) -> str:
    have = len(result.matched_fields)

    if not identity.claimed_fields:
        return (
            "Before I can discuss any claim details, I need to verify your identity. "
            f"Could you provide your {ALL_FIELDS_PROMPT}? I'll need at least "
            f"{REQUIRED_MATCHES} of those."
        )

    if have == 0:
        return (
            "I wasn't able to match what you provided to an account on file. "
            f"Could you double-check and provide your {ALL_FIELDS_PROMPT}?"
        )

    return (
        f"Thanks — I have {have} of the {REQUIRED_MATCHES} pieces of "
        "identification I need. Could you provide one more, such as your "
        f"{ALL_FIELDS_PROMPT}?"
    )
