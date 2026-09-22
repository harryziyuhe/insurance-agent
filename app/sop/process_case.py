from app.session import Phase, SessionState
from app.extraction import ExtractedTurn
from app.tools.claims import classify_intent, get_claim
from app.tools.documents import get_document_guideline
from app.tools.status_sim import check_status

def handle(session: SessionState, user_text: str, extracted: ExtractedTurn) -> str:
    claim = get_claim(session.intent.case_id)
    if claim is None or claim["party_id"] != session.identity.party_id:
        return _escalate_reply()

    if _is_done(user_text):
        session.phase = Phase.POST_PROCESS
        return "Sounds good — let me wrap up with a quick summary before we go."

    if _wants_different_case(user_text):
        session.intent.case_id = None
        session.phase = Phase.RESOLVE_INTENT
        return "Sure, let's look into that other claim — what's it regarding?"

    intent = classify_intent(user_text)
    session.case.topics_covered.append(intent)

    if intent == "status_inquiry":
        status = check_status(session, session.intent.case_id)
        return f"Checking now — claim {claim['case_id']} currently shows as {status}."
    elif intent == "denial_question":
        return _denial_reply(claim)
    elif intent == "document_submission":
        return get_document_guideline(claim, user_text, intent)
    elif intent == "next_steps":
        return _next_steps_reply(claim)
    else:
        return _general_reply(claim)


def _denial_reply(claim: dict) -> str:
    denial_reason = claim.get("denial_reason")
    if not denial_reason:
        return (
            f"Claim {claim['case_id']} wasn't denied — it's currently "
            f"{claim['status']}, so there's no denial reason on file."
        )
    reply = f"Claim {claim['case_id']} was denied because {denial_reason}."
    documents_needed = claim.get("documents_needed")
    if documents_needed:
        reply += f" The missing items are: {', '.join(documents_needed)}."
    appeal_deadline = claim.get("appeal_deadline")
    if appeal_deadline:
        reply += f" You have until {appeal_deadline} to appeal."
    return reply


def _next_steps_reply(claim: dict) -> str:
    if claim.get("status") == "denied":
        parts = [f"For claim {claim['case_id']}, the next step is to address the denial."]
        documents_needed = claim.get("documents_needed")
        if documents_needed:
            parts.append(f"You'll need to submit: {', '.join(documents_needed)}.")
        appeal_deadline = claim.get("appeal_deadline")
        if appeal_deadline:
            parts.append(f"The appeal deadline is {appeal_deadline}.")
        return " ".join(parts)
    return (
        f"Claim {claim['case_id']} is currently {claim['status']} — "
        "there's nothing further needed from you right now."
    )


def _general_reply(claim: dict) -> str:
    return (
        f"Claim {claim['case_id']} ({claim['case_type']}) is currently "
        f"{claim['status']}. {claim.get('summary', '')} "
        "What would you like to know more about?"
    )


def _escalate_reply() -> str:
    return (
        "I'm having trouble pulling up that claim on your account. "
        "Let me connect you with a representative who can help."
    )


def _is_done(text: str) -> bool:
    t = text.lower()
    return any(
        phrase in t
        for phrase in [
            "no thanks", "that's all", "thats all", "nothing else",
            "i'm good", "im good", "no other questions", "that's it",
            "thats it", "no more questions",
        ]
    )


def _wants_different_case(text: str) -> bool:
    t = text.lower()
    mentions_another = any(w in t for w in ["another claim", "different claim", "other claim"])
    mentions_case_type = any(w in t for w in ["healthcare", "dental", "auto"])
    return mentions_another or ("another" in t and mentions_case_type)