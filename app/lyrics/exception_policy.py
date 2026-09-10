"""Batch-safe lyric classification and timing quality checks.

The goal is to turn unusual tracks into explicit policies instead of hard failures:
English-only lyrics skip translation, missing lyrics may render as lyricless, mixed
Korean/English lyrics preserve existing English, and suspicious timing is routed to review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence

HANGUL = re.compile(r"[\uac00-\ud7a3]")
LATIN = re.compile(r"[A-Za-z]")
TIMESTAMP = re.compile(r"\[(\d{1,2}:\d{2}(?:[.:]\d{1,3})?)\]")
INSTRUMENTAL_HINT = re.compile(
    r"(?:\binstrumental\b|\binst\.?\b|\binterlude\b|\bintro\b|\boutro\b|"
    r"\bskit\b|연주곡|인스트루멘탈|비트|인트로|아웃트로|인터루드|스킷)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LyricPolicy:
    language_mode: str
    translation_required: bool
    lyricless: bool
    reason: str = ""


@dataclass(frozen=True)
class TimingQA:
    suspicious: bool
    score: int
    reasons: tuple[str, ...]


def classify_lyrics(lines: Sequence[str], *, title: str = "") -> LyricPolicy:
    cleaned = [str(line or "").strip() for line in lines if str(line or "").strip()]
    if not cleaned:
        reason = "가사를 찾지 못했습니다. 앨범아트+음원만으로 렌더링합니다."
        if INSTRUMENTAL_HINT.search(title or ""):
            reason = "instrumental/interlude 계열로 판단되어 가사 없이 렌더링합니다."
        return LyricPolicy("lyricless", False, True, reason)

    korean_chars = sum(len(HANGUL.findall(line)) for line in cleaned)
    latin_chars = sum(len(LATIN.findall(line)) for line in cleaned)
    if korean_chars == 0 and latin_chars > 0:
        return LyricPolicy("english", False, False, "영어-only 가사: 번역 API를 호출하지 않습니다.")
    if korean_chars > 0 and latin_chars > 0:
        return LyricPolicy("mixed", True, False, "한영 혼용: 기존 영어 표현을 보존하며 한국어 부분만 해석합니다.")
    if korean_chars > 0:
        return LyricPolicy("korean", True, False)
    return LyricPolicy("other", False, False, "번역 대상 언어가 아니므로 원문만 표시합니다.")


def extract_protected_english(text: str) -> List[str]:
    """Return English-ish tokens that should survive a mixed-language translation.

    Single-letter articles are ignored, while abbreviations, brands, slang, numbers joined
    to Latin tokens, and apostrophe forms are kept.
    """
    tokens = re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)?(?:[-+&.][A-Za-z0-9]+)*(?![A-Za-z0-9])", text or "")
    ignored = {"a", "an", "the", "i"}
    result: List[str] = []
    for token in tokens:
        if token.casefold() in ignored and len(token) <= 3:
            continue
        if token not in result:
            result.append(token)
    return result


def protected_terms_preserved(source: str, translated: str) -> bool:
    target = (translated or "").casefold()
    return all(term.casefold() in target for term in extract_protected_english(source))


def _to_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0.0
    return 0.0


def timestamps_from_lrc(text: str) -> List[float]:
    return [_to_seconds(value) for value in TIMESTAMP.findall(text or "")]


def assess_timing(entries: Iterable[dict[str, Any]], duration: float) -> TimingQA:
    rows = [row for row in entries if str(row.get("original", "")).strip()]
    if not rows:
        return TimingQA(False, 100, ())

    times = [float(row.get("start_time", 0.0) or 0.0) for row in rows]
    reasons: List[str] = []
    score = 100

    if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
        reasons.append("타임코드 역전")
        score -= 45

    duplicate_or_tiny = sum(1 for a, b in zip(times, times[1:]) if b - a < 0.18)
    if len(times) > 5 and duplicate_or_tiny / max(1, len(times) - 1) > 0.12:
        reasons.append("지나치게 촘촘하거나 중복된 타임코드")
        score -= 25

    huge_gaps = [b - a for a, b in zip(times, times[1:]) if b - a > 25]
    if len(huge_gaps) >= 2:
        reasons.append("긴 무자막 구간이 반복됨")
        score -= 12

    if duration > 0:
        if times[-1] > duration + 2.0:
            reasons.append("마지막 가사가 음원 길이를 초과")
            score -= 35
        if len(times) >= 8 and times[-1] < duration * 0.45:
            reasons.append("가사 타임라인이 곡 전반부에 과도하게 몰림")
            score -= 20
        if times[0] > min(45.0, duration * 0.35) and len(times) >= 8:
            reasons.append("첫 가사 시작이 비정상적으로 늦음")
            score -= 10

    score = max(0, min(100, score))
    return TimingQA(score < 75, score, tuple(reasons))


def should_render_lyricless_when_missing(*, title: str = "", user_policy: str = "render") -> bool:
    if user_policy == "review":
        return bool(INSTRUMENTAL_HINT.search(title or ""))
    return True
