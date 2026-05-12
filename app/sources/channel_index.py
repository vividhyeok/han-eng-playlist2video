"""YouTube channel indexing and duplicate-upload detection helpers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse

import yt_dlp

from app.config.config_manager import get_config
from app.config.paths import CHANNEL_VIDEO_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import has_openai_api_key, resolve_model
from app.sources.youtube_handler import sanitize_youtube_url

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

_NORMALIZE_PATTERN = re.compile(r"[^0-9a-z\uac00-\ud7a3]+", re.IGNORECASE)
_ARTIST_SPLIT_PATTERN = re.compile(r"\s*(?:,|&|/| x | X | feat\.?|ft\.?|with|and)\s*", re.IGNORECASE)
_LEADING_TAG_PATTERN = re.compile(r"^\s*(?:\[[^\]]+\]|\([^)]*\)|【[^】]+】)\s*")
_NOISE_PATTERN = re.compile(
    r"\b(?:han|eng|lyrics?|lyric\s*video|visualizer|official\s*(?:audio|video)|audio|mv|m/v|ver|cover)\b|"
    r"(?:한글해석|가사해석|번역|자막)",
    re.IGNORECASE,
)
_AI_DUPLICATE_REVIEW_MAX_SCORE = 114
_ai_duplicate_client: Optional[OpenAI] = None
_ai_duplicate_cache: dict[str, bool] = {}


@dataclass(frozen=True)
class ChannelVideoRecord:
    video_id: str
    title: str
    video_url: str
    normalized_title: str


@dataclass(frozen=True)
class ChannelVideoIndex:
    channel_url: str
    channel_title: str
    fetched_at: str
    videos: list[ChannelVideoRecord] = field(default_factory=list)


def normalize_channel_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{(url or '').strip()}")
    path = re.sub(r"/+$", "", parsed.path or "")
    normalized = parsed._replace(scheme="https", netloc=parsed.netloc.lower(), path=path, params="", query="", fragment="")
    return urlunparse(normalized).strip()


def build_channel_video_index(
    channel_url: str,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> ChannelVideoIndex:
    normalized_url = normalize_channel_url(channel_url)
    if not normalized_url:
        raise ValueError("채널 URL이 비어 있습니다.")

    _emit(progress_callback, "채널 영상 목록을 불러오는 중...")
    options = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
        "playlistend": None,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(normalized_url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("채널 영상을 불러오지 못했습니다.")

    entries = info.get("entries") or []
    records: list[ChannelVideoRecord] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        video_id = str(entry.get("id") or "").strip()
        video_url = sanitize_youtube_url(str(entry.get("webpage_url") or entry.get("url") or ""))
        if not video_url and video_id:
            video_url = sanitize_youtube_url(f"https://www.youtube.com/watch?v={video_id}")
        if not title or not video_url:
            continue
        records.append(
            ChannelVideoRecord(
                video_id=video_id,
                title=title,
                video_url=video_url,
                normalized_title=_normalize_uploaded_video_title(title),
            )
        )
        if index % 50 == 0:
            _emit(progress_callback, f"채널 영상 {len(records)}개 정리 완료...")

    channel_title = str(info.get("title") or "YouTube Channel").strip()
    _emit(progress_callback, f"채널 영상 {len(records)}개를 캐시에 저장합니다.")
    return ChannelVideoIndex(
        channel_url=normalized_url,
        channel_title=channel_title,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        videos=records,
    )


def save_channel_video_index(index: ChannelVideoIndex) -> None:
    ensure_data_dirs()
    os.makedirs(os.path.dirname(CHANNEL_VIDEO_CACHE_PATH), exist_ok=True)
    payload = {
        "channel_url": index.channel_url,
        "channel_title": index.channel_title,
        "fetched_at": index.fetched_at,
        "videos": [asdict(video) for video in index.videos],
    }
    with open(CHANNEL_VIDEO_CACHE_PATH, "w", encoding="utf-8") as cache_file:
        json.dump(payload, cache_file, ensure_ascii=False, indent=2)


def load_channel_video_index(expected_channel_url: str = "") -> Optional[ChannelVideoIndex]:
    if not os.path.exists(CHANNEL_VIDEO_CACHE_PATH):
        return None
    try:
        with open(CHANNEL_VIDEO_CACHE_PATH, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
    except Exception:
        return None

    channel_url = normalize_channel_url(str(payload.get("channel_url") or ""))
    if expected_channel_url and normalize_channel_url(expected_channel_url) != channel_url:
        return None

    videos = []
    for item in payload.get("videos") or []:
        if not isinstance(item, dict):
            continue
        videos.append(
            ChannelVideoRecord(
                video_id=str(item.get("video_id") or "").strip(),
                title=str(item.get("title") or "").strip(),
                video_url=str(item.get("video_url") or "").strip(),
                normalized_title=str(item.get("normalized_title") or "").strip(),
            )
        )
    return ChannelVideoIndex(
        channel_url=channel_url,
        channel_title=str(payload.get("channel_title") or "YouTube Channel").strip(),
        fetched_at=str(payload.get("fetched_at") or "").strip(),
        videos=videos,
    )


def find_duplicate_channel_video(
    index: Optional[ChannelVideoIndex],
    *,
    artist: str,
    title: str,
) -> Optional[ChannelVideoRecord]:
    if index is None or not title.strip():
        return None

    normalized_title = _normalize_text(title)
    normalized_artist = _normalize_text(_primary_artist(artist))
    normalized_track = _normalize_text(f"{artist} {title}")
    best_match: tuple[int, ChannelVideoRecord] | None = None

    for video in index.videos:
        score = _score_duplicate_match(
            uploaded_title=video.normalized_title,
            normalized_artist=normalized_artist,
            normalized_title=normalized_title,
            normalized_track=normalized_track,
        )
        if score < _minimum_duplicate_score(normalized_artist):
            continue
        if best_match is None or score > best_match[0]:
            best_match = (score, video)

    if best_match is None:
        return None

    best_score, best_video = best_match
    if not _should_review_with_ai(best_score):
        return best_video

    ai_decision = _ai_is_duplicate(
        artist=artist,
        title=title,
        candidate_title=best_video.title,
    )
    if ai_decision is None:
        return best_video
    return best_video if ai_decision else None


def _score_duplicate_match(
    *,
    uploaded_title: str,
    normalized_artist: str,
    normalized_title: str,
    normalized_track: str,
) -> int:
    if not uploaded_title or not normalized_title:
        return 0

    title_similarity = _similarity_score(normalized_title, uploaded_title)
    title_is_contained = normalized_title in uploaded_title
    # Guard against false positives when artist strings are similar but song title differs.
    if not title_is_contained and title_similarity < 0.6:
        return 0

    score = 0
    track_similarity = _similarity_score(normalized_track, uploaded_title)
    score = max(score, int(title_similarity * 55), int(track_similarity * 70))

    if title_is_contained:
        score += 25
    if normalized_artist:
        if normalized_artist in uploaded_title:
            score += 30
        elif _similarity_score(normalized_artist, uploaded_title) < 0.2:
            score -= 15
    if normalized_artist and normalized_title and normalized_artist in uploaded_title and normalized_title in uploaded_title:
        score += 20
    return score


def _minimum_duplicate_score(normalized_artist: str) -> int:
    return 88 if normalized_artist else 93


def _should_review_with_ai(score: int) -> bool:
    config = get_config()
    enabled = bool(config.get("enable_ai_duplicate_match", True))
    if not enabled:
        return False
    return score <= _AI_DUPLICATE_REVIEW_MAX_SCORE


def _ai_is_duplicate(*, artist: str, title: str, candidate_title: str) -> Optional[bool]:
    client = _get_ai_duplicate_client()
    if client is None:
        return None

    cache_key = "|".join(
        [
            _normalize_text(artist),
            _normalize_text(title),
            _normalize_text(candidate_title),
        ]
    )
    if cache_key in _ai_duplicate_cache:
        return _ai_duplicate_cache[cache_key]

    try:
        config = get_config()
        model = resolve_model(str(config.get("translation_model", "gpt-4o-mini")))
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=80,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict music metadata matcher. "
                        "Return JSON only: {\"is_duplicate\": true|false}. "
                        "True only when both entries are clearly the same song title. "
                        "Different song titles by the same artist must be false."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"playlist_artist: {artist}\n"
                        f"playlist_title: {title}\n"
                        f"channel_video_title: {candidate_title}\n"
                        "Compare after ignoring wrappers like [HAN/ENG], (Official Video), feat. info, producer tags, and punctuation."
                    ),
                },
            ],
        )
    except Exception as exc:
        print(f"[WARN] AI duplicate review failed: {exc}")
        return None

    try:
        content = response.choices[0].message.content or "{}"
        payload = json.loads(content)
        decision = bool(payload.get("is_duplicate", False))
    except Exception:
        return None

    _ai_duplicate_cache[cache_key] = decision
    return decision


def _get_ai_duplicate_client() -> Optional[OpenAI]:
    global _ai_duplicate_client
    if _ai_duplicate_client is not None:
        return _ai_duplicate_client
    if OpenAI is None or not has_openai_api_key():
        return None
    try:
        _ai_duplicate_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception as exc:
        print(f"[WARN] Failed to initialize OpenAI client for duplicate review: {exc}")
        return None
    return _ai_duplicate_client


def _normalize_uploaded_video_title(title: str) -> str:
    cleaned = (title or "").strip()
    while True:
        updated = _LEADING_TAG_PATTERN.sub("", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated
    cleaned = cleaned.replace("|", " ").replace(":", " ")
    cleaned = _NOISE_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    return _normalize_text(cleaned)


def _primary_artist(value: str) -> str:
    artists = [part.strip() for part in _ARTIST_SPLIT_PATTERN.split(value or "") if part.strip()]
    return artists[0] if artists else (value or "").strip()


def _normalize_text(value: str) -> str:
    return _NORMALIZE_PATTERN.sub("", (value or "").strip().lower())


def _similarity_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _emit(progress_callback: Optional[Callable[[str], None]], message: str) -> None:
    if progress_callback:
        progress_callback(message)
