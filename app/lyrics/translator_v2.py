"""Whole-song, index-safe Korean lyric translation and LRC parsing.

The previous translator chunked lyrics into small windows. This module sends one compact
indexed song payload, validates returned indices, and enforces identical translations for
identical repeated lines.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from app.config.config_manager import get_config
from app.config.paths import TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import has_openai_api_key, resolve_model
from app.lyrics.lyric_text_utils import prepare_lyric_text_for_subtitles

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover
    AsyncOpenAI = None

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None
    Field = None

HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}:\d{2}(?:[.:]\d{1,3})?)\]")
METADATA_PATTERN = re.compile(r"^\[(ar|ti|al|by|offset|length):.*\]$", re.IGNORECASE)
PROMPT_VERSION = "whole-song-kr-rap-v4"
CACHE_VERSION = 4

if BaseModel is not None:
    class TranslationLine(BaseModel):
        index: int
        translated: str

    class TranslationPayload(BaseModel):
        lines: List[TranslationLine] = Field(default_factory=list)


def convert_timestamp(timestamp: str) -> float:
    value = timestamp.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except ValueError:
        return 0.0
    return 0.0


def format_lrc_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds % 60:05.2f}"


def _parse_lrc_content(content: str, duration: float = 0.0) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    plain_lines: List[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or METADATA_PATTERN.match(line):
            continue
        stamps = TIMESTAMP_PATTERN.findall(line)
        text = TIMESTAMP_PATTERN.sub("", line).strip()
        if stamps and text:
            for stamp in stamps:
                entries.append({"start_time": convert_timestamp(stamp), "original": text})
        elif text:
            plain_lines.append(text)

    if entries:
        entries.sort(key=lambda item: float(item["start_time"]))
        return entries
    if not plain_lines:
        return []

    prepared = prepare_lyric_text_for_subtitles("\n".join(plain_lines))
    plain_lines = [line.strip() for line in prepared.splitlines() if line.strip()]
    start_offset = 5.0 if duration > 20 else 0.0
    usable = max(duration - start_offset, float(len(plain_lines)))
    interval = usable / max(1, len(plain_lines))
    return [
        {"start_time": start_offset + index * interval, "original": line}
        for index, line in enumerate(plain_lines)
    ]


def _normalize_repeat_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _repeat_groups(lines: List[str]) -> List[List[int]]:
    positions: Dict[str, List[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        key = _normalize_repeat_key(line)
        if key:
            positions[key].append(index)
    return [indexes for indexes in positions.values() if len(indexes) > 1]


def _instructions() -> str:
    return """You translate Korean song lyrics into polished, natural English subtitle lines.
The material is frequently Korean rap, hip-hop, R&B, indie, and internet-influenced writing.
Before translating, silently infer the song-level speaker, addressee, recurring motifs,
repeated hooks, slang, wordplay, named entities, brands, places, cultural references, and
sentence fragments that spill across line breaks. Use the entire indexed song as context.

Translation rules:
1. Return exactly one translation for every non-empty indexed input line. Never merge,
   split, omit, reorder, or renumber lines.
2. Keep identical repeated lyric lines translated identically unless the input text itself
   changes. Hooks must remain terminologically consistent.
3. Preserve attitude, register, profanity, intimacy, irony, flexing, melancholy, and
   deliberate ambiguity. Do not sanitize Korean rap slang.
4. Translate Korean slang by intended pragmatic meaning, not by a dictionary gloss. For
   culturally specific slang with no clean equivalent, use concise natural English that
   preserves the effect.
5. Treat proper nouns carefully. Keep established English spellings when clear; otherwise
   romanize names rather than inventing an English meaning. Never output Hangul inside the
   English translation.
6. Infer omitted Korean subjects/objects and sentence continuation from surrounding lines,
   while keeping each subtitle aligned to its own source line.
7. Keep subtitle wording concise and readable. Do not add explanations or footnotes.
8. Lines that are already fully English must be copied unchanged.
9. Output only the structured `lines` payload requested by the schema.
"""


def _cache_key(model: str, artist: str, title: str, lyrics: List[str]) -> str:
    digest = hashlib.sha256("\n".join(lyrics).encode("utf-8")).hexdigest()
    return f"{CACHE_VERSION}:{PROMPT_VERSION}:{model}:{artist.casefold()}:{title.casefold()}:{digest}"


def _load_cache() -> Dict[str, Any]:
    ensure_data_dirs()
    try:
        with open(TRANSLATION_CACHE_PATH, "r", encoding="utf-8") as file:
            cache = json.load(file)
        if isinstance(cache, dict) and isinstance(cache.get("songs"), dict):
            return cache
    except Exception:
        pass
    return {"version": CACHE_VERSION, "songs": {}}


def _save_cache(cache: Dict[str, Any]) -> None:
    ensure_data_dirs()
    with open(TRANSLATION_CACHE_PATH, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, indent=2)


def _clean(text: str) -> str:
    text = re.sub(r"^(translation:|english:)\s*", "", text.strip(), flags=re.IGNORECASE)
    return text.strip("`\"'").strip()


async def translate_lyrics(
    lyrics: List[str], artist: str = "Unknown Artist", title: str = "Unknown Title"
) -> List[str]:
    if not lyrics or not any(HANGUL_PATTERN.search(line or "") for line in lyrics):
        return [line.strip() for line in lyrics]
    if AsyncOpenAI is None or BaseModel is None or not has_openai_api_key():
        raise RuntimeError("OPENAI_API_KEY is not configured or OpenAI dependencies are missing.")

    model = resolve_model(get_config().get_translation_model())
    cache = _load_cache()
    key = _cache_key(model, artist, title, lyrics)
    cached = cache.get("songs", {}).get(key)
    if isinstance(cached, list) and len(cached) == len(lyrics):
        return [str(item) for item in cached]

    indexed = [
        {"index": index, "text": line.strip()}
        for index, line in enumerate(lyrics)
        if line.strip()
    ]
    expected = {item["index"] for item in indexed}
    payload = {
        "artist": artist,
        "title": title,
        "repeated_line_groups": _repeat_groups(lyrics),
        "lines": indexed,
    }
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    last_error: Optional[Exception] = None

    for attempt in range(3):
        try:
            response = await client.responses.parse(
                model=model,
                instructions=_instructions(),
                input=json.dumps(payload, ensure_ascii=False),
                text_format=TranslationPayload,
                max_output_tokens=8192,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("OpenAI returned no structured translation payload.")
            translated = {item.index: _clean(item.translated) for item in parsed.lines}
            if set(translated) != expected:
                missing = sorted(expected - set(translated))
                extra = sorted(set(translated) - expected)
                raise ValueError(f"Translation index mismatch. missing={missing}, extra={extra}")
            bad = [index for index, text in translated.items() if not text or HANGUL_PATTERN.search(text)]
            if bad:
                raise ValueError(f"Invalid English output at indexes: {bad}")

            output = [translated.get(index, line.strip()) if line.strip() else "" for index, line in enumerate(lyrics)]
            for group in _repeat_groups(lyrics):
                canonical = next((output[index] for index in group if output[index]), "")
                if canonical:
                    for index in group:
                        output[index] = canonical

            cache.setdefault("songs", {})[key] = output
            cache["version"] = CACHE_VERSION
            _save_cache(cache)
            return output
        except Exception as exc:
            last_error = exc
            payload["retry_note"] = (
                f"Previous attempt failed validation: {exc}. Return every required index exactly once, "
                "English only, and preserve line alignment."
            )
            print(f"[WARN] Whole-song translation attempt {attempt + 1} failed: {exc}")

    raise RuntimeError(f"Translation failed after validation retries: {last_error}")


async def parse_lrc_and_translate(
    lrc_filepath: str,
    json_filepath: str,
    duration: float = 0.0,
    *,
    artist: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    with open(lrc_filepath, "r", encoding="utf-8") as file:
        content = file.read()
    entries = _parse_lrc_content(content, duration=duration)
    if not entries:
        raise ValueError(f"No lyric lines could be parsed from {lrc_filepath}")

    originals = [str(item["original"]) for item in entries]
    translations = await translate_lyrics(
        originals,
        artist=artist or os.getenv("CURRENT_ARTIST", "Unknown Artist"),
        title=title or os.getenv("CURRENT_TITLE", "Unknown Title"),
    )
    if len(translations) != len(entries):
        raise RuntimeError("Translation line count changed unexpectedly.")

    for entry, translated in zip(entries, translations):
        entry["english"] = translated

    os.makedirs(os.path.dirname(json_filepath), exist_ok=True)
    with open(json_filepath, "w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)
    return json_filepath


def parse_lyrics_for_review(content: str, duration: float = 0.0) -> List[Dict[str, Any]]:
    return _parse_lrc_content(content, duration=duration)
