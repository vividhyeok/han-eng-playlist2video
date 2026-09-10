"""Context-aware Korean rap lyric translation with automatic model routing.

Pipeline:
- English-only/non-Korean lyrics: zero API translation calls.
- Korean/mixed lyrics: whole-song GPT-5.6 Luna pass.
- Only suspicious/ambiguous lines: GPT-5.6 Terra repair.
- Only still-ambiguous lines: GPT-5.6 Sol final adjudication.
- If Sol still cannot disambiguate from song context, keep a provisional translation and
  surface a human-review question instead of silently inventing meaning.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from app.config.paths import TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import DEFAULT_TRANSLATION_MODEL, REVIEW_TRANSLATION_MODEL, has_openai_api_key
from app.lyrics.exception_policy import classify_lyrics, extract_protected_english, protected_terms_preserved
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
PROMPT_VERSION = "kr-rap-context-escalation-v6"
CACHE_VERSION = 6
BASE_MODEL = DEFAULT_TRANSLATION_MODEL
REVIEW_MODEL = REVIEW_TRANSLATION_MODEL
FINAL_MODEL = "gpt-5.6-sol"

if BaseModel is not None:
    class TranslationLine(BaseModel):
        index: int
        translated: str
        confidence: float = 1.0
        needs_review: bool = False
        ambiguity_question: str = ""

    class TranslationPayload(BaseModel):
        lines: List[TranslationLine] = Field(default_factory=list)


@dataclass
class TranslationIssue:
    index: int
    source: str
    translated: str
    question: str
    confidence: float
    stage: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "source": self.source,
            "translated": self.translated,
            "question": self.question,
            "confidence": round(float(self.confidence), 3),
            "stage": self.stage,
        }


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


def _repeat_groups(lines: Sequence[str]) -> List[List[int]]:
    positions: Dict[str, List[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        key = _normalize_repeat_key(line)
        if key:
            positions[key].append(index)
    return [indexes for indexes in positions.values() if len(indexes) > 1]


def _instructions(stage: str) -> str:
    strictness = {
        "base": "Translate confidently when ordinary song context resolves the meaning. Flag only genuinely material ambiguity.",
        "review": "Re-examine only the supplied difficult lines very carefully. Prefer contextual Korean-rap usage over literal dictionary readings.",
        "final": "Act as the final expert adjudicator. Resolve the line if the intended reading is reasonably inferable; request human clarification only when two materially different readings remain plausible.",
    }.get(stage, "")
    return f"""You are an expert Korean-to-English lyric translator specializing in Korean rap, hip-hop, R&B, indie music, and internet-influenced language.

Your process is interpretive, not mechanical. First reconstruct the intended Korean sentence and scene from the whole-song context, then write a concise natural English subtitle for each source line. Korean rap frequently uses inversion (도치), omitted subjects/objects/particles, sentence fragments spanning multiple lyric lines, deliberate grammar breaking, phonetic spellings, puns, homophones, metaphor, metonymy, boasts, profanity, code-switching, and references whose literal dictionary meaning is wrong in context.

Important Korean-rap interpretation rules:
- Reconstruct inverted or fragmented syntax across adjacent lines before translating, but keep the final output aligned one-to-one with the original line indices.
- Infer omitted subjects and objects only when context supports them. Do not hallucinate a concrete referent when the Korean intentionally stays vague.
- Read metaphor, metonymy, slang, flex language, irony, and double meaning as song language. Translate the intended pragmatic effect, not merely the surface noun meanings.
- Korean cultural references can work as shorthand. For example, references to banknote figures such as 세종 or 사임당 can, when the surrounding bars concern money, function metonymically as cash/bills rather than merely naming historical people. This is an example of the reasoning pattern, not a fixed substitution rule.
- Treat names, crews, labels, neighborhoods, brands, games, memes, Korean web slang, and artist-specific coined expressions as potential proper/cultural terms. Preserve or romanize them when translation would destroy the reference.
- A line may intentionally remain ambiguous. Preserve useful ambiguity when English can do so naturally.
- Existing English inside Korean/English code-switched lyrics is protected source material. Keep the original English words/phrases as intact as natural English syntax permits; never paraphrase a brand, ad-lib, catchphrase, rhyme anchor, or English slang merely to make the sentence more conventional.
- Lines already entirely in English must be copied unchanged.
- Preserve explicit profanity, sexuality, aggression, intimacy, swagger, humor, melancholy, and register. Do not sanitize.
- Repeated identical hooks must use identical English wording.
- Keep subtitles concise; do not add explanations or translator notes to the translated field.

Ambiguity policy:
- `confidence` is 0.0-1.0 for the intended meaning, not grammar quality.
- Set `needs_review=true` only if a materially different interpretation could change the English meaning and the available song context cannot reliably choose between them.
- If review is needed, still provide the best provisional translation and write one short Korean clarification question in `ambiguity_question` that a human familiar with the lyric can answer quickly.
- Do not ask about harmless nuance, stylistic alternatives, or obvious slang you can infer.

Output rules:
1. Return every requested index exactly once.
2. Never merge, split, omit, reorder, or renumber lines.
3. English only in `translated`; no Hangul there.
4. No markdown or commentary outside the structured payload.

{strictness}
"""


def _cache_key(artist: str, title: str, lyrics: Sequence[str]) -> str:
    digest = hashlib.sha256("\n".join(lyrics).encode("utf-8")).hexdigest()
    return f"{CACHE_VERSION}:{PROMPT_VERSION}:{artist.casefold()}:{title.casefold()}:{digest}"


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
    text = re.sub(r"^(translation:|english:)\s*", "", str(text or "").strip(), flags=re.IGNORECASE)
    return text.strip("`\"'").strip()


def _context(lines: Sequence[str], index: int, radius: int = 4) -> List[dict[str, Any]]:
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    return [{"index": i, "text": lines[i]} for i in range(start, end) if lines[i].strip()]


def _line_is_suspicious(source: str, translated: str, confidence: float, needs_review: bool) -> bool:
    if needs_review or confidence < 0.78:
        return True
    if not translated or HANGUL_PATTERN.search(translated):
        return True
    if not protected_terms_preserved(source, translated):
        return True
    if len(source.strip()) >= 6 and len(translated.strip()) > max(150, len(source.strip()) * 4.5):
        return True
    return False


async def _request_translation(
    client: Any,
    *,
    model: str,
    stage: str,
    artist: str,
    title: str,
    lyrics: Sequence[str],
    indexes: Sequence[int],
    current: Optional[Dict[int, dict[str, Any]]] = None,
    human_hints: Optional[Dict[int, str]] = None,
) -> Dict[int, dict[str, Any]]:
    selected = []
    for index in indexes:
        source = lyrics[index]
        item: dict[str, Any] = {
            "index": index,
            "text": source,
            "protected_english": extract_protected_english(source),
            "nearby_context": _context(lyrics, index),
        }
        if current and index in current:
            item["previous_attempt"] = current[index]
        if human_hints and human_hints.get(index):
            item["human_meaning_hint"] = human_hints[index]
        selected.append(item)

    payload = {
        "artist": artist,
        "title": title,
        "stage": stage,
        "repeated_line_groups": _repeat_groups(lyrics),
        "lines": selected,
    }
    response = await client.responses.parse(
        model=model,
        instructions=_instructions(stage),
        input=json.dumps(payload, ensure_ascii=False),
        text_format=TranslationPayload,
        max_output_tokens=max(2048, min(16384, 220 * max(1, len(indexes)))),
    )
    parsed = response.output_parsed
    if parsed is None:
        raise ValueError(f"{model} returned no structured translation payload")

    expected = set(indexes)
    returned = {item.index for item in parsed.lines}
    if returned != expected:
        raise ValueError(f"{model} index mismatch: missing={sorted(expected-returned)} extra={sorted(returned-expected)}")

    result: Dict[int, dict[str, Any]] = {}
    for item in parsed.lines:
        translated = _clean(item.translated)
        confidence = max(0.0, min(1.0, float(item.confidence)))
        result[item.index] = {
            "translated": translated,
            "confidence": confidence,
            "needs_review": bool(item.needs_review),
            "ambiguity_question": str(item.ambiguity_question or "").strip(),
            "model": model,
        }
    return result


async def translate_lyrics_detailed(
    lyrics: List[str], artist: str = "Unknown Artist", title: str = "Unknown Title",
    *, human_hints: Optional[Dict[int, str]] = None,
) -> tuple[List[str], List[TranslationIssue], Dict[int, dict[str, Any]]]:
    policy = classify_lyrics(lyrics, title=title)
    stripped = [line.strip() for line in lyrics]
    if not lyrics or not policy.translation_required:
        return stripped, [], {}
    if AsyncOpenAI is None or BaseModel is None or not has_openai_api_key():
        raise RuntimeError("OPENAI_API_KEY is not configured or OpenAI dependencies are missing.")

    use_cache = not human_hints
    cache = _load_cache()
    key = _cache_key(artist, title, lyrics)
    cached = cache.get("songs", {}).get(key) if use_cache else None
    if isinstance(cached, dict):
        output = cached.get("translations")
        issues_raw = cached.get("issues", [])
        meta = cached.get("meta", {})
        if isinstance(output, list) and len(output) == len(lyrics):
            issues = [TranslationIssue(**item) for item in issues_raw if isinstance(item, dict)]
            return [str(item) for item in output], issues, {int(k): v for k, v in meta.items()}

    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    target_indexes = [i for i, line in enumerate(lyrics) if line.strip()]
    last_error: Optional[Exception] = None
    base: Dict[int, dict[str, Any]] = {}
    for attempt in range(3):
        try:
            base = await _request_translation(
                client, model=BASE_MODEL, stage="base", artist=artist, title=title,
                lyrics=lyrics, indexes=target_indexes, human_hints=human_hints,
            )
            break
        except Exception as exc:
            last_error = exc
            print(f"[WARN] Base translation attempt {attempt + 1} failed: {exc}")
    if not base:
        raise RuntimeError(f"Base lyric translation failed: {last_error}")

    difficult = [
        i for i in target_indexes
        if _line_is_suspicious(
            lyrics[i], base[i]["translated"], base[i]["confidence"], base[i]["needs_review"]
        )
    ]

    merged = dict(base)
    if difficult:
        try:
            repaired = await _request_translation(
                client, model=REVIEW_MODEL, stage="review", artist=artist, title=title,
                lyrics=lyrics, indexes=difficult, current=merged, human_hints=human_hints,
            )
            merged.update(repaired)
        except Exception as exc:
            print(f"[WARN] Selective Terra review failed; keeping Luna results: {exc}")

    final_candidates = [
        i for i in difficult
        if _line_is_suspicious(
            lyrics[i], merged[i]["translated"], merged[i]["confidence"], merged[i]["needs_review"]
        )
    ]
    if final_candidates:
        try:
            final = await _request_translation(
                client, model=FINAL_MODEL, stage="final", artist=artist, title=title,
                lyrics=lyrics, indexes=final_candidates, current=merged, human_hints=human_hints,
            )
            merged.update(final)
        except Exception as exc:
            print(f"[WARN] Selective Sol adjudication failed; keeping previous results: {exc}")

    output = [""] * len(lyrics)
    for index, source in enumerate(lyrics):
        if not source.strip():
            continue
        data = merged.get(index)
        output[index] = data["translated"] if data else source.strip()

    # Identical source hooks must remain identical after all repair passes.
    for group in _repeat_groups(lyrics):
        canonical_index = max(group, key=lambda idx: merged.get(idx, {}).get("confidence", 0.0))
        canonical = output[canonical_index]
        if canonical:
            for index in group:
                output[index] = canonical
                if index in merged:
                    merged[index]["translated"] = canonical

    issues: List[TranslationIssue] = []
    for index in final_candidates:
        data = merged[index]
        still_ambiguous = bool(data.get("needs_review")) or float(data.get("confidence", 1.0)) < 0.68
        if not protected_terms_preserved(lyrics[index], data.get("translated", "")):
            still_ambiguous = True
        if still_ambiguous:
            question = data.get("ambiguity_question") or "이 구절에서 의도한 의미나 대상이 무엇인지 한 줄로 알려주세요."
            issues.append(TranslationIssue(
                index=index,
                source=lyrics[index],
                translated=output[index],
                question=question,
                confidence=float(data.get("confidence", 0.0)),
                stage=str(data.get("model", FINAL_MODEL)),
            ))

    if use_cache:
        cache.setdefault("songs", {})[key] = {
            "translations": output,
            "issues": [issue.as_dict() for issue in issues],
            "meta": {str(index): value for index, value in merged.items()},
        }
        cache["version"] = CACHE_VERSION
        _save_cache(cache)
    return output, issues, merged


async def translate_lyrics(
    lyrics: List[str], artist: str = "Unknown Artist", title: str = "Unknown Title"
) -> List[str]:
    translations, _issues, _meta = await translate_lyrics_detailed(lyrics, artist=artist, title=title)
    return translations


async def parse_lrc_and_translate(
    lrc_filepath: str,
    json_filepath: str,
    duration: float = 0.0,
    *,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    human_hints: Optional[Dict[int, str]] = None,
) -> str:
    with open(lrc_filepath, "r", encoding="utf-8") as file:
        content = file.read()
    entries = _parse_lrc_content(content, duration=duration)
    if not entries:
        raise ValueError(f"No lyric lines could be parsed from {lrc_filepath}")

    originals = [str(item["original"]) for item in entries]
    translations, issues, meta = await translate_lyrics_detailed(
        originals,
        artist=artist or os.getenv("CURRENT_ARTIST", "Unknown Artist"),
        title=title or os.getenv("CURRENT_TITLE", "Unknown Title"),
        human_hints=human_hints,
    )
    if len(translations) != len(entries):
        raise RuntimeError("Translation line count changed unexpectedly.")

    issues_by_index = {issue.index: issue.as_dict() for issue in issues}
    for index, (entry, translated) in enumerate(zip(entries, translations)):
        entry["english"] = translated
        if index in meta:
            entry["translation_meta"] = meta[index]
        if index in issues_by_index:
            entry["translation_review"] = issues_by_index[index]

    os.makedirs(os.path.dirname(json_filepath), exist_ok=True)
    with open(json_filepath, "w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)
    return json_filepath


def get_translation_review_issues(json_filepath: str) -> List[dict[str, Any]]:
    try:
        with open(json_filepath, "r", encoding="utf-8") as file:
            entries = json.load(file)
    except Exception:
        return []
    result = []
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        review = entry.get("translation_review") if isinstance(entry, dict) else None
        if isinstance(review, dict):
            result.append({**review, "index": index})
    return result


async def apply_human_translation_hints(
    json_filepath: str,
    *,
    artist: str,
    title: str,
    hints: Dict[int, str],
) -> List[dict[str, Any]]:
    with open(json_filepath, "r", encoding="utf-8") as file:
        entries = json.load(file)
    originals = [str(entry.get("original", "")) for entry in entries]
    translations, issues, meta = await translate_lyrics_detailed(
        originals, artist=artist, title=title, human_hints=hints,
    )
    issues_by_index = {issue.index: issue.as_dict() for issue in issues}
    for index, entry in enumerate(entries):
        entry["english"] = translations[index]
        entry.pop("translation_review", None)
        if index in meta:
            entry["translation_meta"] = meta[index]
        if index in issues_by_index:
            entry["translation_review"] = issues_by_index[index]
    with open(json_filepath, "w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)
    return [issue.as_dict() for issue in issues]


def parse_lyrics_for_review(content: str, duration: float = 0.0) -> List[Dict[str, Any]]:
    return _parse_lrc_content(content, duration=duration)
