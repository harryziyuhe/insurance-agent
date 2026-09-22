from app.extraction import ExtractedTurn
from app.session import SessionState
from app.tools.claims import get_claim
from app.tools.email import send_email_summary

def handle(session: SessionState, user_text: str, extracted: ExtractedTurn) -> str:
    if session.post_process.summary is None:
        claim = get_claim(session.intent.case_id)
        summary = _draft_summary(session, claim)
        session.post_process.summary = summary
        session.post_process.awaiting_consent = True
        return summary + "Would you like me to email you this summary?"

    if session.post_process.awaiting_consent:
        return _handle_consent_answer(session, user_text)

    return _closing_reply(session)

def _draft_summary(session, claim) -> str:
    name = session.identity.claimed_fields.get("full_name")
    topics = list(set(session.case.topics_covered))
    outcome_line = f"currently {claim['status']}"
    if claim.get("denial_reason"):
        outcome_line += f" ({claim['denial_reason']})"

    followups = []
    if claim.get("documents_needed"):
        followups.append(f"submit: {','.join(claim['documents_needed'])}")
    if claim.get("appeal_deadline"):
        followups.append(f"appeal by {claim['appeal_deadline']}")

    return 

def _handle_consent_answer(session, user_text) -> str:
    answer = _parse_yes_no(user_text)
    if answer is None:
        return "Sorry, just to confirm - yes or no, would you like the summary emailed?"

    session.post_process.awaiting_consent = False
    session.post_process.email_consent = answer

    if answer == "yes":
        send_email_summary(session.identity.party_id, session.post_process.summary)
        session.post_process.email_sent = True
        return "Done - I've sent that summary to your email on file. Anything else?"
    return "No problem. I won't send anything. Thanks for calling - have a great day."

def _parse_yes_no(user_text):
    t = user_text.lower()
    if any(phrase in t for phrase in ["yes", "sure", "please do"]):
        return "yes"
    if any(phrase in t for phrase in ["no", "skip"]):
        return "no"
    return None

def _closing_reply(session):
    return "We've already wrapped up, is there something new I can help with?"