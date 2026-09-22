# LLM_CLIENT_PIPELINE.md

Draft implementations for `app/llm_client.py` and `app/pipeline.py`, per the plan in
`IMPLEMENTATION.md` §1-2.

**Caveat:** the Gemini `google-genai` SDK isn't covered by any bundled reference in this
session (only Anthropic's API is). The code below is from general knowledge, not a verified
live source — treat it as a strong first draft, not a copy-paste-and-trust artifact. Things to
verify against your actual installed `google-genai` version before relying on the fallback
path in anger:

- exact `genai.errors` class names (`ClientError`/`ServerError` vs. a single `APIError` with a
  `.code` attribute)
- whether `response_schema` accepts a plain dict like below, or needs `types.Schema(...)` /
  a Pydantic model / TypedDict instead
- whether `http_options.timeout` is milliseconds or seconds in your version — a units mismatch
  here fails silently as "always times out," so sanity-check with one manual call first

---

## `app/llm_client.py`

```python
"""Thin Gemini transport (IMPLEMENTATION.md §1). No SOP/session knowledge here."""
from __future__ import annotations

import json
import logging
import os

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
_TIMEOUT_MS = 10_000


class LLMUnavailable(Exception):
    """Raised on any transport/quota/parse failure. Callers must catch this
    and fall back to the deterministic path — never let it reach the HTTP layer."""


_client: genai.Client | None = None
_client_init_failed = False


def _get_client() -> genai.Client:
    global _client, _client_init_failed
    if _client is not None:
        return _client
    if _client_init_failed:
        raise LLMUnavailable("GEMINI_API_KEY not configured")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        _client_init_failed = True
        raise LLMUnavailable("GEMINI_API_KEY not configured")
    _client = genai.Client(api_key=api_key)
    return _client


def call_llm(
    system: str,
    user: str,
    schema: dict,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
) -> dict:
    """One structured-output round trip. Returns a dict matching `schema`.
    Raises LLMUnavailable on any failure — never returns a guessed/partial result."""
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
        http_options=types.HttpOptions(timeout=_TIMEOUT_MS),
    )

    try:
        response = client.models.generate_content(model=model, contents=user, config=config)
    except genai_errors.ClientError as e:      # 4xx: rate limit, quota, bad request
        logger.warning("Gemini call failed (client error): %s", e)
        raise LLMUnavailable(str(e)) from e
    except genai_errors.ServerError as e:      # 5xx
        logger.warning("Gemini call failed (server error): %s", e)
        raise LLMUnavailable(str(e)) from e
    except TimeoutError as e:
        logger.warning("Gemini call timed out: %s", e)
        raise LLMUnavailable(str(e)) from e

    if not response.text:
        raise LLMUnavailable("empty response from Gemini")

    try:
        return json.loads(response.text)
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"malformed JSON from Gemini: {e}") from e
```

---

## `app/pipeline.py`

```python
"""Turn orchestration: understand() + respond(), scope/emotion bookkeeping
(IMPLEMENTATION.md §2)."""
from __future__ import annotations

import logging

from app import guardrail
from app.emotion import tone_instruction
from app.extraction import ExtractedTurn, regex_fallback_extract
from app.llm_client import LLMUnavailable, call_llm
from app.session import Phase, SessionState
from app.sop.engine import handle_turn

logger = logging.getLogger(__name__)

OFF_TOPIC_ESCALATION_THRESHOLD = 2

UNDERSTAND_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "full_name": {"type": "STRING", "nullable": True},
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
        "emotion": {
            "type": "STRING",
            "enum": ["neutral", "frustrated", "anxious", "angry", "confused", "refusing"],
        },
    },
    "required": ["wants_different_case", "is_done", "in_scope", "emotion"],
}

UNDERSTAND_SYSTEM_PROMPT = """You are the extraction layer for an insurance claims support \
agent. Given the caller's latest message, extract ONLY what they explicitly stated — never \
guess or infer a PII value (full_name, dob, phone, email, id_last4, policy_number) that \
wasn't actually said. Leave a field null if it wasn't mentioned this turn.

Also classify:
- in_scope: false only if the message is unrelated to this insurance claim call (e.g. general \
knowledge questions). Default true.
- emotion: the caller's emotional tone this turn.
- yes_no: set only if clearly answering a yes/no question.
- wants_different_case / is_done: true only if clearly asking to switch claims, or clearly \
signaling they're finished.

Current SOP phase (context only, do not restrict extraction to it): {phase}"""


def understand(session: SessionState, user_text: str) -> ExtractedTurn:
    system = UNDERSTAND_SYSTEM_PROMPT.format(phase=session.phase.value)
    try:
        data = call_llm(system, user_text, UNDERSTAND_SCHEMA)
        return _extracted_from_llm(data)
    except LLMUnavailable as e:
        logger.warning("understand() falling back to regex extraction: %s", e)
        return regex_fallback_extract(user_text)


def _extracted_from_llm(data: dict) -> ExtractedTurn:
    claimed_fields = {
        k: data[k]
        for k in ("full_name", "dob", "phone", "email", "id_last4", "policy_number")
        if data.get(k)
    }
    return ExtractedTurn(
        claimed_fields=claimed_fields,
        intent_hint=data.get("intent_hint"),
        caller_role=data.get("caller_role"),
        rep_relationship=data.get("rep_relationship"),
        yes_no=data.get("yes_no"),
        mentioned_document=data.get("mentioned_document"),
        wants_different_case=bool(data.get("wants_different_case", False)),
        is_done=bool(data.get("is_done", False)),
        in_scope=bool(data.get("in_scope", True)),
        emotion=data.get("emotion") or "neutral",
    )


RESPOND_SCHEMA = {
    "type": "OBJECT",
    "properties": {"reply": {"type": "STRING"}},
    "required": ["reply"],
}

RESPOND_SYSTEM_PROMPT = """You are a warm, professional insurance claims support agent \
speaking directly to a caller. You may ONLY state facts from the FACTS list — never add, \
infer, or speculate about any claim detail not listed. If FACTS is empty, just deliver \
NEXT_ACTION naturally. Keep it conversational, 2-4 sentences.

{tone_instruction}"""


def respond(
    facts_to_convey: list[str],
    next_action: str,
    tone_hint: str = "neutral",
    *,
    claim: dict | None = None,
) -> str:
    system = RESPOND_SYSTEM_PROMPT.format(tone_instruction=tone_instruction(tone_hint))
    user = (
        "FACTS:\n" + ("\n".join(f"- {f}" for f in facts_to_convey) or "(none)")
        + f"\n\nNEXT_ACTION: {next_action}"
    )

    try:
        data = call_llm(system, user, RESPOND_SCHEMA)
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


def run_turn(session: SessionState, user_text: str) -> str:
    session.turns.append({"role": "user", "text": user_text})

    extracted = understand(session, user_text)

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
    return (
        "It sounds like I'm not the right fit to help with that — let me connect you "
        "with a human representative."
    )
```

---

## Two dependent changes this implies elsewhere

1. **`app/session.py`** — add `off_topic_streak: int = 0` to `SessionState`.
2. **`app/main.py`** — `run_turn()` now does the `session.turns.append(...)` calls itself
   (both user and agent turns), so replace the handler body with:

   ```python
   session = get_session(session_id)
   if session is None:
       raise HTTPException(status_code=404, detail="unknown session_id")
   reply = pipeline.run_turn(session, body.text)
   ```

   and delete the two `session.turns.append(...)` lines and the `extracted = extract(...)` /
   `handle_turn(...)` calls currently there — otherwise turns get double-appended.

## Still to draft

`app/emotion.py::tone_instruction()` and `app/guardrail.py::check()` — `pipeline.py` above
imports both but neither is written yet. See `IMPLEMENTATION.md` §9-10 for their spec.
