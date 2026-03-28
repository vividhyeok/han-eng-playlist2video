"""Helpers for preparing lyric text before syncing or subtitle rendering."""

from __future__ import annotations

from dataclasses import dataclass
import re

HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
TIMESTAMP_PATTERN = re.compile(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
INLINE_SEPARATOR_PATTERN = re.compile(
    r"\s*(?:/{1,2}|\||\u2022|\u00b7|\u2016|\u00a6|\uff0f|\uff3c)\s*"
)
CONJUNCTION_PATTERN = re.compile(
    r"\s+(?P<word>"
    r"and|but|or|so|then|because|cause|when|if|while|though|with|without|"
    r"after|before|where|like|than|as|"
    r"\uadf8\ub9ac\uace0|\uadfc\ub370|\ud558\uc9c0\ub9cc|\uadf8\ub798\uc11c|"
    r"\ub610|\ub9c8\uce58|\ucc98\ub7fc|\ubcf4\ub2e4|\ud558\uba74\uc11c|\ud558\uba70"
    r")\s+",
    re.IGNORECASE,
)
CONNECTIVE_ENDING_PATTERN = re.compile(
    r"(?:"
    r"\uace0|\uc11c|\uc9c0\ub9cc|\ub294\ub370|\uba70|\uba74|\ub2c8\uae4c|"
    r"\ub77c\uace0|\uc774\ub77c|\uc778\ub370|\ud574\uc11c|\ud558\uba70|"
    r"\ud558\ub2e4\uac00|\uac70\ub4e0|\ucc98\ub7fc|\ub4ef\uc774?|\ub9c8\ub0e5|"
    r"\ucc44|\ubcf4\ub2e4"
    r")$"
)


@dataclass(frozen=True)
class LyricTextSummary:
    line_count: int
    long_line_count: int
    max_visual_length: float


def normalize_lyric_text(text: str) -> str:
    """Normalize pasted lyric text into one subtitle candidate per line."""

    normalized_lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = TIMESTAMP_PATTERN.sub("", raw_line).strip().strip("\ufeff")
        if not stripped:
            continue

        split_parts = [
            chunk.strip()
            for chunk in INLINE_SEPARATOR_PATTERN.split(stripped)
            if chunk.strip()
        ]
        normalized_lines.extend(split_parts or [stripped])

    return "\n".join(normalized_lines)


def split_long_lyric_lines(text: str) -> str:
    """Split visually long lyric lines into shorter subtitle-sized lines."""

    split_lines: list[str] = []
    for line in normalize_lyric_text(text).splitlines():
        split_lines.extend(_split_line_recursive(line))
    return "\n".join(line for line in split_lines if line.strip())


def prepare_lyric_text_for_subtitles(text: str) -> str:
    return split_long_lyric_lines(normalize_lyric_text(text))


def summarize_lyric_text(text: str) -> LyricTextSummary:
    lines = [line for line in normalize_lyric_text(text).splitlines() if line.strip()]
    if not lines:
        return LyricTextSummary(line_count=0, long_line_count=0, max_visual_length=0.0)

    visual_lengths = [_visual_length(line) for line in lines]
    long_line_count = sum(
        length > _line_limit(line)
        for line, length in zip(lines, visual_lengths)
    )
    return LyricTextSummary(
        line_count=len(lines),
        long_line_count=long_line_count,
        max_visual_length=max(visual_lengths),
    )


def count_lyric_lines(text: str) -> int:
    return summarize_lyric_text(text).line_count


def _split_line_recursive(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped:
        return []

    limit = _line_limit(stripped)
    if _visual_length(stripped) <= limit:
        return [stripped]

    break_points = _find_break_points(stripped)
    if not break_points:
        return [stripped]

    left_end, right_start = _choose_best_break(stripped, break_points)
    left = stripped[:left_end].strip()
    right = stripped[right_start:].strip()
    if not left or not right:
        return [stripped]

    return _split_line_recursive(left) + _split_line_recursive(right)


def _line_limit(line: str) -> float:
    hangul_count = len(HANGUL_PATTERN.findall(line))
    ascii_count = len(line) - hangul_count
    if hangul_count and ascii_count:
        return 30.0
    if hangul_count:
        return 26.0
    return 42.0


def _visual_length(text: str) -> float:
    length = 0.0
    for character in text:
        if HANGUL_PATTERN.search(character):
            length += 1.7
        elif character.isspace():
            length += 0.4
        else:
            length += 1.0
    return length


def _find_break_points(line: str) -> list[tuple[int, int, int]]:
    points: list[tuple[int, int, int]] = []

    for match in INLINE_SEPARATOR_PATTERN.finditer(line):
        points.append((match.start(), match.end(), 0))

    for match in re.finditer(r"[,;:!?]\s+|\.\s+", line):
        points.append((match.start() + 1, match.end(), 1))

    for match in CONJUNCTION_PATTERN.finditer(line):
        points.append((match.start(), match.start("word"), 2))

    for match in re.finditer(r"\s+", line):
        points.append((match.start(), match.end(), 3))

    if CONNECTIVE_ENDING_PATTERN.search(line):
        midpoint = len(line) // 2
        for match in re.finditer(r"\s+", line):
            if match.start() >= midpoint:
                points.append((match.start(), match.end(), 2))

    return points


def _choose_best_break(
    line: str,
    break_points: list[tuple[int, int, int]],
) -> tuple[int, int]:
    midpoint = len(line) / 2

    def score(item: tuple[int, int, int]) -> tuple[int, float]:
        left_end, _, priority = item
        return priority, abs(left_end - midpoint)

    left_end, right_start, _ = min(break_points, key=score)
    return left_end, right_start
