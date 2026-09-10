"""OpenAI model registry for lyric translation."""
from __future__ import annotations

import os
from typing import Dict

DEFAULT_TRANSLATION_MODEL = "gpt-5.6-luna"
REVIEW_TRANSLATION_MODEL = "gpt-5.6-terra"

OPENAI_MODELS: Dict[str, str] = {
    "gpt-5.6-luna": "GPT-5.6 Luna · recommended / high-volume",
    "gpt-5.6-terra": "GPT-5.6 Terra · higher quality",
    "gpt-5.6-sol": "GPT-5.6 Sol · maximum quality",
}

LEGACY_MODEL_ALIASES = {
    "gpt-5.4-mini": DEFAULT_TRANSLATION_MODEL,
    "gpt-5.4-nano": DEFAULT_TRANSLATION_MODEL,
    "gpt-4o": DEFAULT_TRANSLATION_MODEL,
    "gpt-4o-mini": DEFAULT_TRANSLATION_MODEL,
    "gpt-4-turbo": DEFAULT_TRANSLATION_MODEL,
    "gpt-4.1": DEFAULT_TRANSLATION_MODEL,
    "gpt-4.1-mini": DEFAULT_TRANSLATION_MODEL,
    "deepseek-chat": DEFAULT_TRANSLATION_MODEL,
    "gemini-2.0-flash": DEFAULT_TRANSLATION_MODEL,
    "gemini-2.0-flash-lite": DEFAULT_TRANSLATION_MODEL,
    "gemini-1.5-pro": DEFAULT_TRANSLATION_MODEL,
    "gemini-pro": DEFAULT_TRANSLATION_MODEL,
}


def has_openai_api_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def resolve_model(model_id: str | None) -> str:
    if not model_id:
        return DEFAULT_TRANSLATION_MODEL
    if model_id in OPENAI_MODELS:
        return model_id
    return LEGACY_MODEL_ALIASES.get(model_id, DEFAULT_TRANSLATION_MODEL)


def get_available_models() -> Dict[str, str]:
    return OPENAI_MODELS.copy()
