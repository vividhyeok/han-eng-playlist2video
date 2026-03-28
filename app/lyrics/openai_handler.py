"""OpenAI-powered lyric translation and LRC parsing helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from itertools import zip_longest
from typing import Any, Dict, List, Optional

from app.config.config_manager import get_config
from app.config.paths import TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import has_openai_api_key, resolve_model
from app.lyrics.lyric_text_utils import prepare_lyric_text_for_subtitles

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = lambda: None

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover
    AsyncOpenAI = None

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None
    Field = None

try:
    from pydub import AudioSegment
except ImportError:  # pragma: no cover
    AudioSegment = None

load_dotenv()

HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}:\d{2}(?:[.:]\d{1,3})?)\]")
METADATA_PATTERN = re.compile(r"^\[(ar|ti|al|by|offset|length):.*\]$", re.IGNORECASE)

TRANSLATION_CACHE_VERSION = 3
TRANSLATION_PROMPT_VERSION = "kr-rap-indie-v2"
TRANSLATION_CHUNK_SIZE = 32
TRANSLATION_CONTEXT_RADIUS = 2
KOREAN_CONTINUATION_ENDINGS = (
    "\uace0",
    "\uc11c",
    "\uc9c0\ub9cc",
    "\ub294\ub370",
    "\uba70",
    "\uba74",
    "\ub2c8\uae4c",
    "\ub77c\uace0",
    "\uc774\ub77c",
    "\uc778\ub370",
    "\ud574\uc11c",
    "\ud558\uba70",
    "\ud558\ub2e4\uac00",
    "\uac70\ub4e0",
    "\ucc98\ub7fc",
    "\ub4ef",
    "\ub4ef\uc774",
    "\ub9c8\ub0e5",
    "\ubcf4\ub2e4",
)
KOREAN_CONTINUATION_STARTERS = (
    "\uadf8\ub9ac\uace0",
    "\uadfc\ub370",
    "\ud558\uc9c0\ub9cc",
    "\uadf8\ub798\uc11c",
    "\ub610",
    "\ub9c8\uce58",
    "\ucc98\ub7fc",
    "\ubcf4\ub2e4",
    "\ud558\uba74\uc11c",
    "\ud558\uba70",
)

_translation_cache: Optional[Dict[str, Any]] = None


if BaseModel is not None:
    class TranslationLine(BaseModel):
        index: int
        translated: str


    class TranslationPayload(BaseModel):
        lines: List[TranslationLine] = Field(default_factory=list)


class OpenAILyricsTranslator:
    """Translate lyric lines with the OpenAI Responses API."""

    def __init__(self, model_name: str):
        self.model_name = resolve_model(model_name)
        self.client = None
        if AsyncOpenAI and has_openai_api_key():
            self.client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def is_available(self) -> bool:
        return self.client is not None and BaseModel is not None

    async def translate(
        self,
        lyrics: List[str],
        artist: str,
        title: str,
    ) -> List[str]:
        if not lyrics:
            return []

        if not self.is_available():
            return lyrics

        indexed_lines = [
            {"index": index, "text": line.strip()}
            for index, line in enumerate(lyrics)
            if line.strip()
        ]
        if not indexed_lines:
            return lyrics

        if not any(HANGUL_PATTERN.search(item["text"]) for item in indexed_lines):
            return [line.strip() for line in lyrics]

        last_error: Optional[Exception] = None
        translated_by_index: Dict[int, str] = {}

        for chunk in _chunk_lines(indexed_lines, TRANSLATION_CHUNK_SIZE):
            try:
                translated_by_index.update(
                    await self._translate_chunk(lyrics, artist, title, chunk)
                )
            except Exception as exc:
                last_error = exc
                print(f"[WARN] OpenAI chunk translation failed: {exc}")
                for item in chunk:
                    translated_by_index[item["index"]] = item["text"]

        translated_lines: List[str] = []
        for index, original in enumerate(lyrics):
            stripped = original.strip()
            if not stripped:
                translated_lines.append("")
                continue
            if is_english(stripped):
                translated_lines.append(stripped)
                continue

            translated_text = translated_by_index.get(index, stripped).strip()
            if not translated_text or translated_text == "Translation Error":
                translated_text = stripped
            translated_lines.append(translated_text)

        if translated_lines:
            return translated_lines

        if last_error is not None:
            traceback.print_exception(last_error)
        return lyrics

    async def _translate_chunk(
        self,
        lyrics: List[str],
        artist: str,
        title: str,
        chunk: List[Dict[str, Any]],
    ) -> Dict[int, str]:
        payload = _build_translation_payload(lyrics, artist, title, chunk)
        chunk_indexes = [item["index"] for item in chunk]
        last_error: Optional[Exception] = None

        for attempt in range(3):
            try:
                response = await self.client.responses.parse(
                    model=self.model_name,
                    instructions=_build_translation_instructions(),
                    input=json.dumps(payload, ensure_ascii=False),
                    text_format=TranslationPayload,
                    temperature=0.2,
                    max_output_tokens=3072,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError("OpenAI returned no parsed content.")

                translated_by_index = {
                    item.index: clean_translation(item.translated)
                    for item in parsed.lines
                    if item.index in chunk_indexes
                }
                missing_indexes = [
                    item["index"]
                    for item in chunk
                    if item["index"] not in translated_by_index
                ]
                if missing_indexes:
                    raise ValueError(f"Missing translated lines: {missing_indexes}")
                return translated_by_index
            except Exception as exc:
                last_error = exc
                print(
                    f"[WARN] OpenAI translation attempt {attempt + 1} failed: {exc}"
                )

        if last_error is not None:
            raise last_error
        raise RuntimeError("Translation chunk failed without an explicit error.")


def _build_translation_instructions() -> str:
    return (
        "You translate Korean song lyrics into natural English subtitle lines.\n"
        "These lyrics are often Korean rap, alternative, or indie writing.\n"
        "Infer omitted subjects, objects, particles, endings, and inverted word order "
        "from nearby lines instead of translating fragments literally.\n"
        "Preserve tone, emotional intent, attitude, and punch. Favor the intended "
        "meaning over stiff word-for-word phrasing.\n"
        "If a Korean line is fragmentary because the sentence spills across lines, "
        "write the most natural English line for that fragment while keeping it aligned "
        "to only that line.\n"
        "Keep subtitle lines concise. Do not over-explain slang or metaphors.\n"
        "Return a JSON object with one field named 'lines'.\n"
        "The 'lines' field must contain a list of objects with keys "
        "'index' and 'translated'.\n"
        "Rules:\n"
        "1. Keep exactly one output line for every input line.\n"
        "2. Never merge or split lines.\n"
        "3. Preserve explicit profanity, swagger, irony, melancholy, and intimacy when present.\n"
        "4. Use the nearby line context fields when a sentence is split across lines.\n"
        "5. If an input line is already fully English, copy it unchanged.\n"
        "6. Output natural English only in the 'translated' field.\n"
        "7. Do not add explanations, markdown, or extra keys.\n"
    )


def _new_cache() -> Dict[str, Any]:
    return {"version": TRANSLATION_CACHE_VERSION, "songs": {}}


def _ensure_cache_loaded() -> None:
    global _translation_cache
    if _translation_cache is not None:
        return

    ensure_data_dirs()
    if not os.path.exists(TRANSLATION_CACHE_PATH):
        _translation_cache = _new_cache()
        return

    try:
        with open(TRANSLATION_CACHE_PATH, "r", encoding="utf-8") as cache_file:
            raw_cache = json.load(cache_file)
        if isinstance(raw_cache, dict) and isinstance(raw_cache.get("songs"), dict):
            _translation_cache = raw_cache
        else:
            _translation_cache = _new_cache()
    except Exception:
        _translation_cache = _new_cache()


def _save_cache() -> None:
    if _translation_cache is None:
        return

    ensure_data_dirs()
    os.makedirs(os.path.dirname(TRANSLATION_CACHE_PATH), exist_ok=True)
    with open(TRANSLATION_CACHE_PATH, "w", encoding="utf-8") as cache_file:
        json.dump(_translation_cache, cache_file, ensure_ascii=False, indent=2)


def _build_song_cache_key(
    model_id: str,
    artist: str,
    title: str,
    lyrics: List[str],
) -> str:
    normalized_lyrics = "\n".join(line.strip() for line in lyrics)
    digest = hashlib.sha256(normalized_lyrics.encode("utf-8")).hexdigest()
    return (
        f"{resolve_model(model_id)}::{TRANSLATION_PROMPT_VERSION}::"
        f"{artist.strip().lower()}::{title.strip().lower()}::{digest}"
    )


def _get_cached_song_translation(cache_key: str) -> Optional[List[str]]:
    _ensure_cache_loaded()
    assert _translation_cache is not None
    cached_value = _translation_cache["songs"].get(cache_key)
    if isinstance(cached_value, list) and all(
        isinstance(item, str) for item in cached_value
    ):
        return cached_value
    return None


def _update_song_cache(cache_key: str, translations: List[str]) -> None:
    _ensure_cache_loaded()
    assert _translation_cache is not None
    _translation_cache["songs"][cache_key] = translations


async def translate_lyrics(
    lyrics: List[str],
    artist: Optional[str] = None,
    title: Optional[str] = None,
) -> List[str]:
    """Translate a full lyric block with context-aware OpenAI output."""

    if not lyrics:
        return []

    artist_name = artist or os.getenv("CURRENT_ARTIST", "Unknown Artist")
    title_name = title or os.getenv("CURRENT_TITLE", "Unknown Title")
    model_id = resolve_model(get_config().get_translation_model())
    cache_key = _build_song_cache_key(model_id, artist_name, title_name, lyrics)

    cached_translation = _get_cached_song_translation(cache_key)
    if cached_translation and len(cached_translation) == len(lyrics):
        return cached_translation

    translator = OpenAILyricsTranslator(model_name=model_id)
    translated_lines = await translator.translate(lyrics, artist_name, title_name)
    if len(translated_lines) != len(lyrics):
        print(
            "[WARN] Translation line count mismatch. Falling back to original lyrics."
        )
        translated_lines = lyrics

    _update_song_cache(cache_key, translated_lines)
    _save_cache()
    return translated_lines


def is_english(text: str) -> bool:
    if not text or not text.strip():
        return True
    return not bool(HANGUL_PATTERN.search(text))


def clean_translation(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^(translation:|english:)\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip("`\"'")

    if HANGUL_PATTERN.search(cleaned):
        cleaned = re.sub(r"\([^)]*[\uac00-\ud7a3][^)]*\)", "", cleaned)
        cleaned = re.sub(r"[\uac00-\ud7a3]+", "", cleaned).strip()
        if not cleaned:
            return "Translation Error"

    return cleaned.strip() or "Translation Error"


def _chunk_lines(
    indexed_lines: List[Dict[str, Any]],
    chunk_size: int,
) -> List[List[Dict[str, Any]]]:
    return [
        indexed_lines[start:start + chunk_size]
        for start in range(0, len(indexed_lines), chunk_size)
    ]


def _build_translation_payload(
    lyrics: List[str],
    artist: str,
    title: str,
    chunk: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "artist": artist,
        "title": title,
        "translation_profile": "Korean rap and indie lyric subtitle translation",
        "chunk_indexes": [item["index"] for item in chunk],
        "lines": [
            {
                "index": item["index"],
                "text": item["text"],
                "previous": _nearest_nonempty_line(lyrics, item["index"], -1),
                "next": _nearest_nonempty_line(lyrics, item["index"], 1),
                "before_context": _context_window(
                    lyrics,
                    item["index"],
                    direction=-1,
                    radius=TRANSLATION_CONTEXT_RADIUS,
                ),
                "after_context": _context_window(
                    lyrics,
                    item["index"],
                    direction=1,
                    radius=TRANSLATION_CONTEXT_RADIUS,
                ),
                "notes": _build_line_notes(lyrics, item["index"], item["text"]),
            }
            for item in chunk
        ],
    }


def _nearest_nonempty_line(
    lyrics: List[str],
    index: int,
    direction: int,
) -> str:
    current = index + direction
    while 0 <= current < len(lyrics):
        candidate = lyrics[current].strip()
        if candidate:
            return candidate
        current += direction
    return ""


def _context_window(
    lyrics: List[str],
    index: int,
    direction: int,
    radius: int,
) -> List[str]:
    context: List[str] = []
    current = index + direction
    while 0 <= current < len(lyrics) and len(context) < radius:
        candidate = lyrics[current].strip()
        if candidate:
            context.append(candidate)
        current += direction
    if direction < 0:
        context.reverse()
    return context


def _build_line_notes(
    lyrics: List[str],
    index: int,
    text: str,
) -> List[str]:
    previous_line = _nearest_nonempty_line(lyrics, index, -1)
    next_line = _nearest_nonempty_line(lyrics, index, 1)
    notes: List[str] = []

    if _looks_like_fragment(text):
        notes.append("likely fragment with omitted grammar")
    if _continues_previous_line(text, previous_line) or _continues_next_line(text, next_line):
        notes.append("likely part of a sentence spanning adjacent lines")
    if len(text.split()) <= 3 or len(text) <= 12:
        notes.append("keep the English concise and punchy")

    return notes


def _looks_like_fragment(text: str) -> bool:
    stripped = text.strip().strip("~.")
    if not stripped:
        return False
    if len(stripped) <= 12:
        return True
    return any(stripped.endswith(ending) for ending in KOREAN_CONTINUATION_ENDINGS)


def _continues_previous_line(text: str, previous_line: str) -> bool:
    stripped = text.strip()
    if not stripped or not previous_line:
        return False
    return any(stripped.startswith(starter) for starter in KOREAN_CONTINUATION_STARTERS)


def _continues_next_line(text: str, next_line: str) -> bool:
    stripped = text.strip()
    if not stripped or not next_line:
        return False
    return any(stripped.endswith(ending) for ending in KOREAN_CONTINUATION_ENDINGS)


def convert_timestamp(timestamp: str) -> float:
    value = timestamp.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
    except ValueError:
        return 0.0
    return 0.0


def format_lrc_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    return f"{minutes:02d}:{remaining_seconds:05.2f}"


def seconds_to_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours = total_milliseconds // 3_600_000
    remainder = total_milliseconds % 3_600_000
    minutes = remainder // 60_000
    remainder %= 60_000
    secs = remainder // 1_000
    milliseconds = remainder % 1_000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def _parse_lrc_content(content: str, duration: float = 0.0) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    plain_lines: List[str] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or METADATA_PATTERN.match(line):
            continue

        timestamps = TIMESTAMP_PATTERN.findall(line)
        text = TIMESTAMP_PATTERN.sub("", line).strip()

        if timestamps and text:
            for timestamp in timestamps:
                entries.append(
                    {
                        "start_time": convert_timestamp(timestamp),
                        "original": text,
                    }
                )
        elif not timestamps and text:
            plain_lines.append(text)

    if entries:
        entries.sort(key=lambda item: float(item["start_time"]))
        return entries

    if not plain_lines:
        return []

    prepared_plain_text = prepare_lyric_text_for_subtitles("\n".join(plain_lines))
    plain_lines = [line for line in prepared_plain_text.splitlines() if line.strip()]

    start_offset = 5.0 if duration > 20 else 0.0
    usable_duration = max(duration - start_offset, float(len(plain_lines)))
    interval = usable_duration / max(len(plain_lines), 1)

    distributed_entries: List[Dict[str, Any]] = []
    for index, line in enumerate(plain_lines):
        distributed_entries.append(
            {
                "start_time": start_offset + index * interval,
                "original": line,
            }
        )
    return distributed_entries


async def parse_lrc_and_translate(
    lrc_filepath: str,
    json_filepath: str,
    duration: float = 0.0,
) -> str:
    if not os.path.exists(lrc_filepath):
        raise FileNotFoundError(f"LRC file not found: {lrc_filepath}")

    with open(lrc_filepath, "r", encoding="utf-8") as lrc_file:
        lrc_content = lrc_file.read()

    lyrics_data = _parse_lrc_content(lrc_content, duration=duration)
    if not lyrics_data:
        raise ValueError(f"No lyric lines could be parsed from {lrc_filepath}")

    originals = [item["original"] for item in lyrics_data]
    artist = os.getenv("CURRENT_ARTIST", "Unknown Artist")
    title = os.getenv("CURRENT_TITLE", "Unknown Title")
    translations = await translate_lyrics(originals, artist=artist, title=title)

    for entry, translated_text in zip_longest(lyrics_data, translations, fillvalue=""):
        if entry is None:
            continue
        entry["english"] = translated_text or entry["original"]

    os.makedirs(os.path.dirname(json_filepath), exist_ok=True)
    with open(json_filepath, "w", encoding="utf-8") as json_file:
        json.dump(lyrics_data, json_file, ensure_ascii=False, indent=2)

    print(f"[INFO] Lyrics JSON saved to {json_filepath}")
    return json_filepath


def save_lyrics_json(lyrics_data: List[Dict[str, Any]], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(lyrics_data, output_file, ensure_ascii=False, indent=2)


async def generate_srt_from_lrc(
    lrc_filepath: str,
    srt_filepath: str,
    audio_filepath: Optional[str] = None,
    default_duration: float = 3.0,
) -> str:
    total_duration = None
    if audio_filepath and os.path.exists(audio_filepath) and AudioSegment:
        try:
            audio = AudioSegment.from_file(audio_filepath)
            total_duration = len(audio) / 1000.0
        except Exception as exc:
            print(f"[WARN] Failed to inspect audio duration for SRT export: {exc}")

    with open(lrc_filepath, "r", encoding="utf-8") as lrc_file:
        lrc_content = lrc_file.read()

    lyric_entries = _parse_lrc_content(lrc_content, duration=total_duration or 0.0)
    if not lyric_entries:
        raise ValueError(f"No lyrics could be parsed from {lrc_filepath}")

    originals = [item["original"] for item in lyric_entries]
    translations = await translate_lyrics(originals)

    srt_lines: List[str] = []
    for index, entry in enumerate(lyric_entries, start=1):
        start_time = float(entry["start_time"])
        if index < len(lyric_entries):
            end_time = float(lyric_entries[index].get("start_time"))
        elif total_duration is not None:
            end_time = total_duration
        else:
            end_time = start_time + default_duration

        translated = (
            translations[index - 1]
            if index - 1 < len(translations)
            else entry["original"]
        )
        srt_lines.extend(
            [
                str(index),
                f"{seconds_to_srt_timestamp(start_time)} --> {seconds_to_srt_timestamp(end_time)}",
                entry["original"],
                translated,
                "",
            ]
        )

    with open(srt_filepath, "w", encoding="utf-8") as srt_file:
        srt_file.write("\n".join(srt_lines))

    print(f"[INFO] SRT file saved to {srt_filepath}")
    return srt_filepath
