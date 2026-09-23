from app.extraction import ExtractedTurn
from app.pipeline import respond
from app.session import Phase, SessionState
from app.sop import process_case
from app.tools.claims import filter_claims, list_claims


def handle(session: SessionState, user_text: str, extracted: ExtractedTurn) -> str:
    claims = list_claims(session.identity.party_id)
    if not claims:
        return _no_claim_reply()

    new_text = extracted.intent_hint or user_text
    hint_text = " ".join(t for t in (session.memory.intent_hint, new_text) if t)
    if not hint_text:
        return _clarify_reply()

    session.memory.intent_hint = hint_text  # persist so it accumulates next turn

    candidates = filter_claims(claims, hint_text)

    if len(candidates) == 1:
        case = candidates[0]
        session.intent.case_id = case["case_id"]
        session.intent.resolved_intent = (
            extracted.intent_category or "general_claim_question"
        )
        session.phase = Phase.PROCESS_CASE
        return process_case.handle(session, user_text, extracted)

    return _clarify_ambiguous_reply(candidates)


def _no_claim_reply() -> str:
    next_action = "tell the caller that you do not have a claim for them on file and ask whether they want to speak to a representative"
    return respond([], next_action)


def _clarify_reply() -> str:
    next_action = "ask the caller to clarify their intent for calling"
    return respond([], next_action)


def _clarify_ambiguous_reply(candidates: list[dict]) -> str:
    options = "; ".join(
        f"a {c['case_type']} claim, {c['status']}, from {c['created_at']}"
        for c in candidates
    )
    return f"I see a few claims that could match: {options}. Which one are you calling about?"
