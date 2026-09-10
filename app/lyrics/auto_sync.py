"""Automatic timing for plain lyrics using the same OpenAI API key.

Only tracks without trustworthy synchronized lyrics use this path. Audio is transcribed
with segment timestamps, then the exact known lyric lines are aligned to those segments by
a cheap GPT-5.6 Luna structured-output pass. Low-confidence results remain editable with
the tap-sync UI instead of being silently accepted.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from app.lyrics.ai_models import DEFAULT_TRANSLATION_MODEL, has_openai_api_key
from app.lyrics.translator_v2 import format_lrc_timestamp, parse_lyrics_for_review

try:
    from openai import AsyncOpenAI, OpenAI
except ImportError:  # pragma: no cover
    AsyncOpenAI = None
    OpenAI = None

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None
    Field = None

TRANSCRIBE_MODEL = "gpt-transcribe"
ALIGN_MODEL = DEFAULT_TRANSLATION_MODEL
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'’.-]{1,30}|[가-힣]{2,12}")

if BaseModel is not None:
    class AlignedLine(BaseModel):
        index: int
        start_time: float
        confidence: float = 1.0

    class AlignmentPayload(BaseModel):
        lines: List[AlignedLine] = Field(default_factory=list)


@dataclass(frozen=True)
class AutoSyncResult:
    lrc_path: str
    confidence: float
    low_confidence_indexes: tuple[int, ...]
    transcript_segments: int


def _keyword_hints(lyrics: Sequence[str], artist: str, title: str) -> List[str]:
    hints: List[str] = []
    for candidate in [artist, title, *lyrics]:
        for token in TOKEN.findall(candidate or ""):
            value = token.strip()
            if value and value.casefold() not in {item.casefold() for item in hints}:
                hints.append(value)
            if len(hints) >= 40:
                return hints
    return hints


def _segments_from_response(response: Any) -> List[Dict[str, Any]]:
    raw = getattr(response, "segments", None)
    if raw is None and hasattr(response, "model_dump"):
        raw = response.model_dump().get("segments")
    result: List[Dict[str, Any]] = []
    for segment in raw or []:
        if hasattr(segment, "model_dump"):
            segment = segment.model_dump()
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text", "")).strip()
        if text:
            result.append({"start": start, "end": max(start, end), "text": text})
    return result


def _transcribe_sync(audio_path: str, lyrics: Sequence[str], artist: str, title: str) -> List[Dict[str, Any]]:
    if OpenAI is None or not has_openai_api_key():
        raise RuntimeError("OpenAI transcription is unavailable.")
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    prompt = (
        f"Korean rap/hip-hop song by {artist}, title {title}. Preserve Korean and English "
        "code-switching, ad-libs, names, brands, and slang as heard."
    )
    with open(audio_path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            languages=["ko", "en"],
            keywords=_keyword_hints(lyrics, artist, title),
            prompt=prompt,
        )
    return _segments_from_response(response)


async def _align(
    lyrics: Sequence[str], segments: Sequence[Dict[str, Any]], artist: str, title: str,
) -> Dict[int, Dict[str, float]]:
    if AsyncOpenAI is None or BaseModel is None or not has_openai_api_key():
        raise RuntimeError("OpenAI alignment is unavailable.")
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    payload = {
        "artist": artist,
        "title": title,
        "exact_lyric_lines": [{"index": i, "text": line} for i, line in enumerate(lyrics)],
        "transcript_segments": list(segments),
    }
    instructions = """Align exact known lyric lines to timestamped ASR segments from the same song.
The exact lyric text is authoritative; the ASR text may contain recognition errors, especially
Korean rap slang and Korean/English code-switching. Use chronology, repeated hooks, lexical
similarity, neighboring lines, and segment duration. Return one start time for every lyric
line. Times must be nondecreasing. When several lyric lines occur inside one ASR segment,
interpolate plausible starts across that segment in lyric order. Do not rewrite lyrics.
`confidence` measures timing confidence, not transcription spelling accuracy. Return only
the structured payload."""
    response = await client.responses.parse(
        model=ALIGN_MODEL,
        instructions=instructions,
        input=json.dumps(payload, ensure_ascii=False),
        text_format=AlignmentPayload,
        max_output_tokens=max(2048, min(12000, len(lyrics) * 90)),
    )
    parsed = response.output_parsed
    if parsed is None:
        raise ValueError("Auto-sync alignment returned no structured payload.")
    expected = set(range(len(lyrics)))
    returned = {item.index for item in parsed.lines}
    if returned != expected:
        raise ValueError(f"Auto-sync index mismatch: missing={sorted(expected-returned)}")
    return {
        item.index: {
            "start_time": max(0.0, float(item.start_time)),
            "confidence": max(0.0, min(1.0, float(item.confidence))),
        }
        for item in parsed.lines
    }


def _enforce_monotonic(aligned: Dict[int, Dict[str, float]], count: int) -> None:
    last = 0.0
    for index in range(count):
        current = max(last, float(aligned[index]["start_time"]))
        aligned[index]["start_time"] = current
        last = current


async def auto_sync_plain_lyrics(
    *, lrc_path: str, audio_path: str, artist: str, title: str,
) -> AutoSyncResult:
    if not has_openai_api_key():
        raise RuntimeError("OpenAI API key is required for automatic sync.")
    with open(lrc_path, "r", encoding="utf-8") as file:
        content = file.read()
    parsed = parse_lyrics_for_review(content, duration=0.0)
    lyrics = [str(item.get("original", "")).strip() for item in parsed if str(item.get("original", "")).strip()]
    if not lyrics:
        raise ValueError("Plain lyric file contains no usable lines.")

    segments = await asyncio.to_thread(_transcribe_sync, audio_path, lyrics, artist, title)
    if not segments:
        raise RuntimeError("Audio transcription returned no timestamped segments.")
    aligned = await _align(lyrics, segments, artist, title)
    _enforce_monotonic(aligned, len(lyrics))

    confidences = [aligned[i]["confidence"] for i in range(len(lyrics))]
    low = tuple(i for i, value in enumerate(confidences) if value < 0.72)
    overall = sum(confidences) / max(1, len(confidences))

    with open(lrc_path, "w", encoding="utf-8") as file:
        for index, line in enumerate(lyrics):
            file.write(f"[{format_lrc_timestamp(aligned[index]['start_time'])}] {line}\n")

    return AutoSyncResult(
        lrc_path=lrc_path,
        confidence=overall,
        low_confidence_indexes=low,
        transcript_segments=len(segments),
    )
