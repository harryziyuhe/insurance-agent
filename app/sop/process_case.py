from app.pipeline import respond
from app.session import Phase, SessionState
from app.extraction import ExtractedTurn
from app.tools.claims import classify_intent, get_claim
from app.tools.documents import get_document_guideline
from app.tools.status_sim import check_status


def handle(session: SessionState, user_text: str, extracted: ExtractedTurn) -> str:
    claim = get_claim(session.intent.case_id)
    if claim is None or claim["party_id"] != session.identity.party_id:
        return _escalate_reply(extracted.emotion, session)

    if extracted.is_done or _is_done(user_text):
        session.phase = Phase.POST_PROCESS
        next_action = "tell the caller you'll wrap up with a quick summary before you go"
        return respond([], next_action, extracted.emotion, session=session)

    if extracted.wants_different_case or _wants_different_case(user_text):
        session.intent.case_id = None
        session.phase = Phase.RESOLVE_INTENT
        next_action = "tell the caller sure, let's look into that other claim, and ask what it's regarding"
        return respond([], next_action, extracted.emotion, session=session)

    # LLM-classified this turn's intent when available (understand() already
    # reads the full message semantically); the keyword classifier is only a
    # fallback for when the LLM path is unavailable, same pattern as
    # is_done/wants_different_case above.
    intent = extracted.intent_category or classify_intent(user_text)
    session.case.topics_covered.append(intent)

    if intent == "status_inquiry":
        status = check_status(session, session.intent.case_id)
        facts = [f"claim id: {claim['case_id']}", f"status: {status}"]
        next_action = "tell the caller you checked and this is the claim's current status"
        payment_fact = _payment_fact(claim)
        if payment_fact:
            facts.append(payment_fact)
            next_action += ", and include the amount paid since the claim is settled"
        return respond(facts, next_action, extracted.emotion, claim=claim, session=session)
    elif intent == "denial_question":
        return _denial_reply(claim, extracted.emotion, session)
    # Route on a named document mention too, not just the intent bucket — a
    # caller asking "what is X" or "how to get X" about a specific document is
    # clearly document-related even when intent_category lands on something
    # looser like general_claim_question.
    elif intent == "document_submission" or extracted.mentioned_document:
        return _document_reply(claim, user_text, extracted, session)
    elif intent == "next_steps":
        return _next_steps_reply(claim, extracted.emotion, session)
    else:
        return _general_reply(claim, extracted.emotion, session)


def _denial_reply(claim: dict, emotion: str, session: SessionState) -> str:
    denial_reason = claim.get("denial_reason")
    if not denial_reason:
        facts = [f"claim id: {claim['case_id']}", f"status: {claim['status']}"]
        next_action = (
            "tell the caller this claim wasn't actually denied — it's currently in "
            "that status, so there's no denial reason on file"
        )
        return respond(facts, next_action, emotion, claim=claim, session=session)

    facts = [f"claim id: {claim['case_id']}", f"denial reason: {denial_reason}"]
    documents_needed = claim.get("documents_needed")
    if documents_needed:
        facts.append(f"missing documents: {', '.join(documents_needed)}")
    appeal_deadline = claim.get("appeal_deadline")
    if appeal_deadline:
        facts.append(f"appeal deadline: {appeal_deadline}")
    next_action = "explain why the claim was denied and what's needed to appeal, if anything"
    return respond(facts, next_action, emotion, claim=claim, session=session)


def _document_reply(claim: dict, user_text: str, extracted: ExtractedTurn, session: SessionState) -> str:
    # get_document_guideline() stays deterministic/fixture-grounded (compliance
    # text shouldn't be improvised), but its output is handed to respond() as
    # an authorized fact instead of returned verbatim — the fixture text is
    # written for "how do I submit this" and reads oddly for "what is this
    # document" or "how do I get this", so letting the LLM restate it in
    # direct answer to whatever was actually asked is what makes this
    # "looser" without giving up the compliance-accuracy guarantee.
    guidance = get_document_guideline(claim, user_text, "document_submission", extracted.mentioned_document, extracted.lacks_document)
    facts = [f"claim id: {claim['case_id']}", f"guidance: {guidance}"]
    next_action = (
        "answer the caller's specific question about the document(s), using only the "
        "guidance fact as ground truth — restate it naturally in direct answer to what "
        "they actually asked, rather than reciting it verbatim"
    )
    return respond(facts, next_action, extracted.emotion, claim=claim, session=session)


def _next_steps_reply(claim: dict, emotion: str, session: SessionState) -> str:
    if claim.get("status") == "denied":
        facts = [f"claim id: {claim['case_id']}"]
        documents_needed = claim.get("documents_needed")
        if documents_needed:
            facts.append(f"missing documents: {', '.join(documents_needed)}")
        appeal_deadline = claim.get("appeal_deadline")
        if appeal_deadline:
            facts.append(f"appeal deadline: {appeal_deadline}")
        next_action = "tell the caller the next step is addressing the denial, including anything they need to submit and by when"
        return respond(facts, next_action, emotion, claim=claim, session=session)

    facts = [f"claim id: {claim['case_id']}", f"status: {claim['status']}"]
    next_action = "tell the caller there's nothing further needed from them right now given the claim's current status"
    payment_fact = _payment_fact(claim)
    if payment_fact:
        facts.append(payment_fact)
        next_action += ", and mention the amount they were paid"
    return respond(facts, next_action, emotion, claim=claim, session=session)


def _general_reply(claim: dict, emotion: str, session: SessionState) -> str:
    facts = [f"claim id: {claim['case_id']}", f"claim type: {claim['case_type']}", f"status: {claim['status']}"]
    if claim.get("summary"):
        facts.append(f"summary: {claim['summary']}")
    next_action = "give the caller a quick overview of their claim, then ask what they'd like to know more about"
    payment_fact = _payment_fact(claim)
    if payment_fact:
        facts.append(payment_fact)
        next_action = "give the caller a quick overview of their claim, including the amount paid, then ask what they'd like to know more about"
    return respond(facts, next_action, emotion, claim=claim, session=session)


def _payment_fact(claim: dict) -> str | None:
    # Only ever surface net_pay — the finalized paid amount — and only for a
    # claim that's actually settled in the fixture's own status field (not the
    # simulated live-poll status from check_status(), which tracks a separate,
    # still-in-progress notion of "current state" and shouldn't be conflated
    # with a real closed/approved claim's final payout).
    if claim.get("status") not in ("closed", "approved"):
        return None
    net_pay = claim.get("net_pay")
    if not net_pay:
        return None
    return f"amount paid: ${net_pay}"


def _escalate_reply(emotion: str, session: SessionState) -> str:
    next_action = (
        "tell the caller you're having trouble pulling up that claim on their "
        "account and that you'll connect them with a representative who can help"
    )
    return respond([], next_action, emotion, session=session)


def _is_done(text: str) -> bool:
    t = text.lower()
    return any(
        phrase in t
        for phrase in [
            "no thanks", "that's all", "thats all", "nothing else",
            "nothing more", "i'm good", "im good", "no other questions",
            "that's it", "thats it", "no more questions", "i'm done",
            "im done",
        ]
    )


def _wants_different_case(text: str) -> bool:
    t = text.lower()
    mentions_another = any(w in t for w in ["another claim", "different claim", "other claim"])
    mentions_case_type = any(w in t for w in ["healthcare", "dental", "auto"])
    return mentions_another or ("another" in t and mentions_case_type)
