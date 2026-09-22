from app.session import SessionState
from app.extraction import ExtractedTurn

def understand(session: SessionState, user_text: str) -> ExtractedTurn:
    return

def respond(facts_to_convey: list[str], next_action: str, tone_hint: str = "neutral") -> str:
    return

def run_turn(session: SessionState, user_text):
    extracted = understand(session, user_text)
    if not extracted.in_scope:
        session.off_topic_streak += 1
    else:
        session.off_topic_streak = 0

    if _off_topic_escalation_triggered(session, extracted):
        return _handoff_reply(session)

    reply = handle_turn(session, user_text, extracted)

    return reply