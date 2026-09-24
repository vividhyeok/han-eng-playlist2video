"""OpenAI model choices for lyric translation."""
from __future__ import annotations

import os
from typing import Dict

DEFAULT_TRANSLATION_MODEL = "gpt-5.6-terra"
REVIEW_TRANSLATION_MODEL = "gpt-5.6-sol"

OPENAI_MODELS: Dict[str, str] = {
    "gpt-5.6-terra": "GPT-5.6 Terra · 권장 (품질/속도 균형)",
    "gpt-5.6-luna": "GPT-5.6 Luna · 빠른 대량 처리",
    "gpt-5.6-sol": "GPT-5.6 Sol · 최고 품질",
}

LEGACY_MODEL_ALIASES = {
    model: DEFAULT_TRANSLATION_MODEL
    for model in (
        "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4o", "gpt-4o-mini",
        "gpt-4-turbo", "gpt-4.1", "gpt-4.1-mini", "deepseek-chat",
        "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro",
        "gemini-pro",
    )
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
