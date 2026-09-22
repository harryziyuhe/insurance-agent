import logging

from app import guardrail
from app.emotion import *
from app.extraction import ExtractedTurn, regex_fallback_extract
from app.llm_client import LLMUnavailable, call_llm
from app.session import Phase, SessionState
from app.sop.engine import handle_turn

logger = logging.getLogger(__name__)

OFF_TOPIC_ESCALATION_THRESHOLD = 2

UNDERSTAND_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ful_name": {"type": "STRING", "nullable": True},
        "dob": {"type": "STRING", "nullable": True},
        "phone": {"type": "STRING", "nullable": True},
        "email": {"type": "STRING", "nullable": True},
        "id_last4": {"type": "STRING", "nullable": True},
        "policy_number": {"type": "STRING", "nullable": True},
        "intent_hint": {"type": "STRING", "nullable": True},
        "caller_role": {"type": "STRING", "enum": ["self", "representative"], "nullable": True},
        "rep_relationship": {"type": "STRING", "nullable": True},
        "yes_no": {"type": "STRING", "enum": ["yes", "no"], "nullable": True},
        "mentioned_document": {"type": "STRING", "nullable": True},
        "wants_different_case": {"type": "BOOLEAN"},
        "is_done": {"type": "BOOLEAN"},
        "in_scope": {"type": "BOOLEAN"},
        "emotion": {"type": "STRING", "enum": ["neutral", "frustrated", "anxious", "angry", "confused", "refusing"]}
    },
    "required": ["wants_different_case", "is_done", "in_scope", "emotion"]
}

UNDERSTAND_SYSTEM_PROMPT = """
You are the extraction layer for an insurance claims support agent. \
Given the caller's latest message, extract ONLY what they explicitly \
stated - never guess or infer a PII value (full_name, dob, phone, email, \
id_last4, policy_number) that wasn't actually said. Leave a field null if \
it wasn't mentioned this turn.

Also classify:
- in_scope: false only if the message is unrelated to this insurance claim \
call (e.g. general knowledge questions). Default true.
- emotion: the caller's emotional tone this turn.
- yes_no: set only if clearly answering a yes/no question.
- wants_different case / is_done: true only if clearly asking to switch claims \
or clearly signaling they are finished.

Current SOP phase (context only, do not restrict extraction to it): {phase}
"""

def understand(session: SessionState, user_text: str) -> ExtractedTurn:
    system = UNDERSTAND_SYSTEM_PROMPT.format(phase=session.phase.value)
    try:
        data = call_llm(system, user_text, UNDERSTAND_SCHEMA)
        return _extracted_from_llm(data)
    except LLMUnavailable as e:
        logger.warning("understand() falling back to regex extraction: %s", e)
        return regex_fallback_extract(user_text)

def _extract_from_llm(data: dict) -> ExtractedTurn:
    claimed_fields = {
        k: data[k] for k in ("full_name", "dob", "phone", "email", "id_last4", "policy_number") if data.get(k)
    }
    return ExtractedTurn(
        claimed_fields=claimed_fields,
        intent_hint=data.get("intent_hint")
        caller_role=data.get("caller_role")
        rep_relationship=
    )

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