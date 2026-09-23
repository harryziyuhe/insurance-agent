from app.extraction import ExtractedTurn
from app.session import Phase, SessionState
from app.sop import post_process, process_case, resolve_intent, verify_id


def handle_turn(session: SessionState, user_text: str, extracted: ExtractedTurn) -> str:
    if session.phase == Phase.VERIFY_ID:
        return verify_id.handle(session, extracted)
    if session.phase == Phase.RESOLVE_INTENT:
        return resolve_intent.handle(session, user_text, extracted)
    if session.phase == Phase.PROCESS_CASE:
        return process_case.handle(session, user_text, extracted)
    if session.phase == Phase.POST_PROCESS:
        return post_process.handle(session, user_text, extracted)
    # HUMAN_HANDOFF has no controller yet (ARCHITECTURE.md: "not yet wired to
    # anything") — this only fires on a turn *after* the handoff message
    # itself, since that message is returned directly by whichever phase
    # triggered the escalation.
    return (
        "You've been connected to a human representative queue — "
        "someone will be with you shortly."
    )
