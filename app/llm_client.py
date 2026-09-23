import json
import logging
import os

import groq
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from groq import Groq

load_dotenv()

logger = logging.getLogger(__name__)

GROQ_MODEL = "openai/gpt-oss-20b"
GEMINI_MODEL = "gemini-2.5-flash"
_TIMEOUT_S = 10


class LLMUnavailable(Exception):
    pass


_groq_client: Groq | None = None
_groq_init_failed = False

_gemini_client: genai.Client | None = None
_gemini_init_failed = False


def _get_groq_client() -> Groq:
    global _groq_client, _groq_init_failed
    if _groq_client is not None:
        return _groq_client
    if _groq_init_failed:
        raise LLMUnavailable("GROQ_API_KEY not configured")
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        _groq_init_failed = True
        raise LLMUnavailable("GROQ_API_KEY not configured")
    _groq_client = Groq(api_key=api_key, timeout=_TIMEOUT_S)
    return _groq_client


def _get_gemini_client() -> genai.Client:
    global _gemini_client, _gemini_init_failed
    if _gemini_client is not None:
        return _gemini_client
    if _gemini_init_failed:
        raise LLMUnavailable("GEMINI_API_KEY not configured")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        _gemini_init_failed = True
        raise LLMUnavailable("GEMINI_API_KEY not configured")
    _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


_GEMINI_TO_JSON_TYPE = {
    "OBJECT": "object",
    "STRING": "string",
    "BOOLEAN": "boolean",
    "NUMBER": "number",
    "INTEGER": "integer",
}


def _to_strict_json_schema(node: dict) -> dict:
    json_type = _GEMINI_TO_JSON_TYPE.get(node.get("type"), node.get("type"))
    out: dict = {}

    if node.get("nullable"):
        out["type"] = [json_type, "null"]
    else:
        out["type"] = json_type

    if "description" in node:
        out["description"] = node["description"]

    if "enum" in node:
        out["enum"] = list(node["enum"]) + ([None] if node.get("nullable") else [])

    if json_type == "object" and "properties" in node:
        out["properties"] = {
            k: _to_strict_json_schema(v) for k, v in node["properties"].items()
        }
        out["required"] = list(node["properties"].keys())
        out["additionalProperties"] = False

    return out


def _call_groq(system: str, user: str, schema: dict, temperature: float) -> dict:
    client = _get_groq_client()
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": _to_strict_json_schema(schema),
                },
            },
        )
    except groq.APIError as e:
        logger.warning("Groq call failed: %s", e)
        raise LLMUnavailable(str(e)) from e

    content = completion.choices[0].message.content
    if not content:
        raise LLMUnavailable("empty response from Groq")
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"malformed JSON from Groq: {e}") from e


def _call_gemini(system: str, user: str, schema: dict, temperature: float) -> dict:
    client = _get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
        http_options=types.HttpOptions(timeout=_TIMEOUT_S * 1000),
    )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=user, config=config
        )
    except genai_errors.ClientError as e:
        logger.warning("Gemini call failed (client error): %s", e)
        raise LLMUnavailable(str(e)) from e
    except genai_errors.ServerError as e:
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


_PROVIDERS = (
    ("Groq", _call_groq),
    ("Gemini", _call_gemini),
)


def call_llm(system: str, user: str, schema: dict, temperature: float = 0.0) -> dict:
    errors = []
    for name, fn in _PROVIDERS:
        try:
            return fn(system, user, schema, temperature)
        except LLMUnavailable as e:
            logger.warning("%s unavailable, trying next provider: %s", name, e)
            errors.append(f"{name}: {e}")
    raise LLMUnavailable(f"all LLM providers unavailable ({'; '.join(errors)})")
