"""OpenAI model registry for lyric translation."""

from __future__ import annotations

import os
from typing import Dict

DEFAULT_TRANSLATION_MODEL = "gpt-4o-mini"

OPENAI_MODELS: Dict[str, str] = {
    "gpt-4o-mini": "OpenAI GPT-4o Mini",
    "gpt-4.1-mini": "OpenAI GPT-4.1 Mini",
    "gpt-4.1": "OpenAI GPT-4.1",
}

LEGACY_MODEL_ALIASES = {
    "gpt-4o": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4o-mini",
    "deepseek-chat": "gpt-4o-mini",
    "gemini-2.0-flash": "gpt-4o-mini",
    "gemini-2.0-flash-lite": "gpt-4o-mini",
    "gemini-1.5-pro": "gpt-4o-mini",
    "gemini-pro": "gpt-4o-mini",
}


def has_openai_api_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def resolve_model(model_id: str | None) -> str:
    if not model_id:
        return DEFAULT_TRANSLATION_MODEL
    if model_id in OPENAI_MODELS:
        return model_id
    return LEGACY_MODEL_ALIASES.get(model_id, DEFAULT_TRANSLATION_MODEL)


def get_available_models() -> Dict[str, str]:
    if not has_openai_api_key():
        return {}
    return OPENAI_MODELS.copy()
