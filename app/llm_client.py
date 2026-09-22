from google import genai

client = genai.Client()



class LLMUnavailable(Exception):


def call_llm(system, user, schema) -> dict:
    return