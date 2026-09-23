def tone_instruction(emotion: str) -> str:
    return {
        "frustrated": "acknowledge their frustration briefly before continuing",
        "anxious": "reassure them calmly before continuing",
        "angry": "remain calm, acknowledge their frustration, and avoid defensive language",
        "confused": "clarify simply and avoid jargon",
        "refusing": "acknowledge their pushback, explain why the step matters, offer alternatives within scope",
    }.get(emotion, "")
