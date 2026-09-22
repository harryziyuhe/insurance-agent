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
    Exception

_client: genai.Client | None = None
_client_init_failed = False

def _get_client() -> genai.Client:
    global _client, _client_init_failed
    if _client is not None:
        return _client
    if _client_init_failed:
        raise LLMUnavailable("GENMINI_API_KEY not configured")
    api_key = os.environ.get("GEMNINI_API_KEY")
    if not api_key:
        raise LLMUnavailable("GENMINI_API_KEY not configured")
    _client = genai.Client(api_key=api_key)
    return _client

def call_llm(system: str, user: str, schema: dict, model: str = DEFAULT_MODEL, temperature: float = 0.0) -> dict:
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
        http_options=types.HttpOptions(timeout=_TIMEOUT_MS)
    )

    try:
        response = client.models.generate_content(model=model, contents=user, config=config)
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
