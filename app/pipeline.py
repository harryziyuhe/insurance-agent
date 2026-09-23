import logging
from dataclasses import asdict

from app import guardrail
from app.emotion import tone_instruction
from app.extraction import ExtractedTurn, regex_fallback_extract
from app.llm_client import LLMUnavailable, call_llm
from app.session import Phase, SessionState

logger = logging.getLogger(__name__)

OFF_TOPIC_ESCALATION_THRESHOLD = 2

UNDERSTAND_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "full_name": {"type": "STRING", "nullable": True, "description": "The caller's full legal name, only if they stated it this turn."},
        "dob": {"type": "STRING", "nullable": True, "description": "The caller's date of birth, only if they stated it this turn, in whatever format they said it."},
        "phone": {"type": "STRING", "nullable": True, "description": "The caller's phone number, only if they stated it this turn."},
        "email": {"type": "STRING", "nullable": True, "description": "The caller's email address, only if they stated it this turn."},
        "id_last4": {"type": "STRING", "nullable": True, "description": "The last 4 digits of the caller's SSN or national ID, only if they stated it this turn."},
        "policy_number": {"type": "STRING", "nullable": True, "description": "The caller's policy number, only if they stated it this turn."},
        "intent_hint": {
            "type": "STRING",
            "nullable": True,
            "description": (
                "A short phrase capturing why the caller says they're reaching out or what "
                "claim/topic they mention — e.g. 'denied healthcare claim', 'auto claim status', "
                "'why my claim was denied'. Extract this even when it's combined in the same "
                "message with the caller's name or other details — a caller very often states "
                "their name and their reason for calling in one breath. Null only if the message "
                "gives no indication at all of why they're calling."
            ),
        },
        "caller_role": {
            "type": "STRING",
            "enum": ["self", "representative"],
            "nullable": True,
            "description": (
                "'self' if the caller says they are the policyholder themselves. "
                "'representative' if they say they're calling on someone else's behalf "
                "(e.g. a family member, POA, or authorized rep). Null if unstated."
            ),
        },
        "rep_relationship": {
            "type": "STRING",
            "nullable": True,
            "description": "If caller_role is 'representative', their stated relationship to the policyholder (e.g. 'son', 'spouse', 'POA'). Null otherwise.",
        },
        "yes_no": {"type": "STRING", "enum": ["yes", "no"], "nullable": True, "description": "Set only if this message is clearly answering a yes/no question the agent just asked."},
        "mentioned_document": {"type": "STRING", "nullable": True, "description": "The specific document name the caller mentions (e.g. 'pathology report'), only if one was named this turn."},
        "lacks_document": {"type": "BOOLEAN", "description": "True only if the caller is clearly saying they don't have, can't find, or lost a document being discussed."},
        "intent_category": {
            "type": "STRING",
            "nullable": True,
            "enum": ["denial_question", "document_submission", "status_inquiry", "next_steps", "general_claim_question"],
            "description": (
                "What this message is mainly about, once the caller is already discussing a "
                "specific claim: 'denial_question' (asking why it was denied, or about the "
                "denial), 'document_submission' (ANY question about a specific document — what "
                "it is, what it needs to contain, how to obtain/get one, how/where to submit it, "
                "or saying they don't have one — not just submission logistics), 'status_inquiry' "
                "(asking for a status update), 'next_steps' (asking what to do next), "
                "'general_claim_question' (anything else about the claim, including just "
                "acknowledging, thanking, or a comment with no new question). Null if this "
                "message isn't about a specific claim at all."
            ),
        },
        "wants_different_case": {"type": "BOOLEAN", "description": "True only if the caller is clearly asking to switch to discussing a different claim."},
        "is_done": {
            "type": "BOOLEAN",
            "description": (
                "True ONLY if the caller explicitly states or unmistakably implies they have no "
                "more questions and are ready to end the call — e.g. 'that's all', 'I'm done', "
                "'nothing more', 'no other questions', 'no that's all the questions I have'. "
                "A bare acknowledgment like 'ok', 'thanks', 'got it', 'sounds good', or 'all good' "
                "is NOT is_done by itself — these overwhelmingly mean 'understood, go on' or "
                "'acknowledged', not 'end the call', and most callers say several of these during "
                "a conversation before they're actually finished. When genuinely unsure, prefer "
                "false — a false negative just means one more turn; a false positive ends the "
                "conversation on the caller."
            ),
        },
        "in_scope": {"type": "BOOLEAN", "description": "False only if the message is unrelated to this insurance claim call (e.g. a general knowledge question). Default true."},
        "emotion": {
            "type": "STRING",
            "enum": ["neutral", "frustrated", "anxious", "angry", "confused", "refusing"],
            "description": (
                "The caller's emotional tone, read from this message in light of the "
                "conversation so far — emotion typically escalates rather than resetting each "
                "turn, so weigh the trend: a mildly annoyed message on top of prior frustration "
                "is more than just 'frustrated' again, and a short reply like 'fine' or 'whatever' "
                "reads very differently after several unresolved turns than as an opener. Pick the "
                "strongest tone actually supported by the message, don't default to neutral just "
                "because this message alone is calm-sounding if the conversation clearly is not."
            ),
        }
    },
    "required": ["wants_different_case", "is_done", "in_scope", "emotion"]
}

UNDERSTAND_SYSTEM_PROMPT = """
You are the extraction layer for an insurance claims support agent. \
Given the caller's latest message, extract ONLY what they explicitly \
stated - never guess or infer a PII value (full_name, dob, phone, email, \
id_last4, policy_number) that wasn't actually said. Leave a field null if \
it wasn't mentioned this turn.

Pay close attention to intent_hint in particular: callers frequently state \
their name, their identity, AND their reason for calling all in the same \
message (e.g. "Hi, my name is X, I'm calling about my denied claim..."). \
Extract intent_hint whenever it's present, even if it's mixed in with other \
information — do not let PII extraction crowd it out.

Judgment calls like emotion, is_done, and intent_category depend on the \
conversation so far, not just the words in this one message — read the \
recent-conversation context below (if any) before deciding on those fields.

Current SOP phase (context only, do not restrict extraction to it): {phase}
"""

UNDERSTAND_HISTORY_TURNS = 6  # recent turns of context, not counting this one


def _recent_history(session: SessionState) -> str:
    # session.turns already has this turn's user message appended (pipeline
    # run_turn appends before calling understand()), so exclude the last entry
    # to avoid duplicating it against the explicit "latest message" below.
    prior = session.turns[:-1][-UNDERSTAND_HISTORY_TURNS:]
    if not prior:
        return ""
    lines = [f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['text']}" for t in prior]
    return "Recent conversation, for context only — do not extract fields from these lines:\n" + "\n".join(lines) + "\n\n"


def understand(session: SessionState, user_text: str) -> ExtractedTurn:
    system = UNDERSTAND_SYSTEM_PROMPT.format(phase=session.phase.value)
    # A short reply like "I'm done" or "that's all" is only unambiguous in
    # light of what was just asked — without recent history, is_done/emotion/
    # intent_category judgments are guesses made on a single line in isolation.
    payload = f"{_recent_history(session)}Caller's latest message (extract/classify ONLY this):\n{user_text}"
    try:
        data = call_llm(system, payload, UNDERSTAND_SCHEMA)
        return _extracted_from_llm(data)
    except LLMUnavailable as e:
        logger.warning("understand() falling back to regex extraction: %s", e)
        return regex_fallback_extract(user_text)

def _extracted_from_llm(data: dict) -> ExtractedTurn:
    claimed_fields = {
        k: data[k] for k in ("full_name", "dob", "phone", "email", "id_last4", "policy_number") if data.get(k)
    }
    return ExtractedTurn(
        claimed_fields=claimed_fields,
        intent_hint=data.get("intent_hint"),
        caller_role=data.get("caller_role"),
        rep_relationship=data.get("rep_relationship"),
        yes_no=data.get("yes_no"),
        mentioned_document=data.get("mentioned_document"),
        intent_category=data.get("intent_category"),
        lacks_document=bool(data.get("lacks_document", False)),
        wants_different_case=bool(data.get("wants_different_case", False)),
        is_done=bool(data.get("is_done", False)),
        in_scope=bool(data.get("in_scope", True)),
        emotion=data.get("emotion") or "neutral"
    )

RESPOND_SCHEMA = {
    "type": "OBJECT",
    "properties": {"reply": {"type": "STRING"}},
    "required": ["reply"]
}

RESPOND_SYSTEM_PROMPT = """
You are a professional insurance claims support agent speaking directly \
to a caller. You may only state facts from the FACTS list. Never add, infer \
or speculate about any claim detail not listed. If FACTS is empty, just deliver \
NEXT_ACTION naturally. Keep it conversational, 2-4 sentences. Vary your \
sentence structure and word choice turn to turn — don't fall into a fixed \
template, especially across turns about the same claim. {tone_instruction}
"""

# Higher than understand()'s 0.0 on purpose: understand() is extracting facts
# and needs the same input to reliably produce the same output, but respond()
# is generating conversational phrasing, where some variation across turns
# is exactly what makes it not sound like a scripted bot repeating itself.
RESPOND_TEMPERATURE = 0.8


def _recent_agent_replies(session: SessionState | None, max_replies: int = 3) -> str:
    if not session:
        return ""
    replies = [t["text"] for t in session.turns if t["role"] == "agent"][-max_replies:]
    if not replies:
        return ""
    lines = "\n".join(f"- {r}" for r in replies)
    return f"\n\nYour own recent replies (say this differently — don't repeat the same phrasing/structure):\n{lines}"


def respond(
    facts_to_convey: list[str],
    next_action: str,
    tone_hint: str = "neutral",
    claim: dict | None = None,
    session: SessionState | None = None,
) -> str:
    system = RESPOND_SYSTEM_PROMPT.format(tone_instruction=tone_instruction(tone_hint))
    user = (
        "FACTS:\n" + ("\n".join(f"- {f}" for f in facts_to_convey) or "(none)")
        + f"\n\nNEXT_ACTION: {next_action}"
        + _recent_agent_replies(session)
    )

    try:
        data = call_llm(system, user, RESPOND_SCHEMA, temperature=RESPOND_TEMPERATURE)
        reply = data["reply"]
    except LLMUnavailable as e:
        logger.warning("respond() falling back to templated reply: %s", e)
        reply = _fallback_reply(facts_to_convey, next_action)

    if not guardrail.check(reply, facts_to_convey, claim=claim):
        logger.error("guardrail rejected a reply, using fallback instead: %r", reply)
        reply = _fallback_reply(facts_to_convey, next_action)

    return reply

def _fallback_reply(facts_to_convey: list[str], next_action: str) -> str:
    parts = [p.rstrip(".") for p in (*facts_to_convey, next_action) if p]
    return ". ".join(parts) + "."

def run_turn(session: SessionState, user_text):
    # Deferred import: app.sop.engine's phase controllers import respond() from
    # this module at load time, so importing handle_turn up top would deadlock
    # on a circular import.
    from app.sop.engine import handle_turn

    session.turns.append({"role": "user", "text": user_text})

    extracted = understand(session, user_text)
    session.last_extracted = asdict(extracted)
    if _register_scope(session, extracted):
        reply = _handoff_reply(session)
    else:
        reply = handle_turn(session, user_text, extracted)

    session.turns.append({"role": "agent", "text": reply})

    return reply

def _register_scope(session: SessionState, extracted: ExtractedTurn) -> bool:
    if extracted.in_scope:
        session.off_topic_streak = 0
        return False
    session.off_topic_streak += 1
    return session.off_topic_streak >= OFF_TOPIC_ESCALATION_THRESHOLD

def _handoff_reply(session: SessionState) -> str:
    session.phase = Phase.HUMAN_HANDOFF
    session.off_topic_streak = 0
    return ("It sounds like I'm not the right fit to help with that - let me connect you with a human representative")