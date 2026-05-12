"""Playlist extraction and review-track preparation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import yt_dlp

from app.config.paths import LYRICS_DIR, ensure_data_dirs
from app.pipeline.process_manager import OutputMode, ProcessConfig
from app.sources.channel_index import ChannelVideoIndex, ChannelVideoRecord, find_duplicate_channel_video
from app.sources.genie_handler import get_best_lyrics_result, lyrics_are_synced, parse_genie_extra_info, search_genie_songs
from app.sources.youtube_handler import sanitize_youtube_url

_NORMALIZE_PATTERN = re.compile(r"[^0-9a-z\uac00-\ud7a3]+", re.IGNORECASE)
_ARTIST_SPLIT_PATTERN = re.compile(r"\s*(?:,|&|/| x | X | feat\.?|ft\.?|with|and)\s*", re.IGNORECASE)
_HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
_LATIN_PATTERN = re.compile(r"[a-z]", re.IGNORECASE)


@dataclass(frozen=True)
class PlaylistSourceTrack:
    index: int
    title: str
    artist: str
    album: str
    youtube_url: str
    duration: Optional[int]
    thumbnail_url: str
    playlist_title: str

    @property
    def label(self) -> str:
        return f"{self.artist or '아티스트 미상'} - {self.title or '제목 미상'}"


@dataclass
class PlaylistReviewTrack:
    source: PlaylistSourceTrack
    title: str
    artist: str
    album: str
    youtube_url: str
    album_art_url: str
    duration: Optional[int]
    lyrics_text: str = ""
    lrc_path: Optional[str] = None
    lyrics_mode: str = "missing"
    lyrics_source: str = "none"
    status: str = "missing_lyrics"
    note: str = ""
    include_in_batch: bool = False

    @property
    def label(self) -> str:
        return f"{self.artist or '아티스트 미상'} - {self.title or '제목 미상'}"

    def has_synced_lyrics(self) -> bool:
        return bool(
            self.lyrics_mode == "synced"
            and self.lrc_path
            and os.path.exists(self.lrc_path)
        )

    def can_render(self) -> bool:
        return bool(
            self.title.strip()
            and self.artist.strip()
            and self.youtube_url.strip()
            and self.has_synced_lyrics()
        )

    def to_process_config(self, output_mode: OutputMode) -> ProcessConfig:
        return ProcessConfig(
            title=self.title.strip(),
            artist=self.artist.strip(),
            album_art_url=self.album_art_url.strip(),
            youtube_url=self.youtube_url.strip(),
            output_mode=output_mode,
            lrc_path=self.lrc_path,
            prefer_youtube=True,
        )


@dataclass(frozen=True)
class PlaylistDuplicateSkip:
    source_label: str
    matched_video_title: str
    matched_video_url: str


@dataclass(frozen=True)
class PlaylistImportReport:
    playlist_title: str
    playlist_url: str
    source_track_count: int
    tracks: list[PlaylistReviewTrack]
    duplicate_skips: list[PlaylistDuplicateSkip] = field(default_factory=list)

    @property
    def duplicate_skip_count(self) -> int:
        return len(self.duplicate_skips)


def import_playlist(
    playlist_url: str,
    *,
    output_mode: OutputMode = "video",
    channel_index: Optional[ChannelVideoIndex] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> PlaylistImportReport:
    normalized_url = normalize_youtube_playlist_url(playlist_url)
    playlist_title, source_tracks = extract_playlist_tracks(normalized_url)
    review_tracks: list[PlaylistReviewTrack] = []
    duplicate_skips: list[PlaylistDuplicateSkip] = []
    for index, source_track in enumerate(source_tracks, start=1):
        duplicate = _match_channel_duplicate(channel_index, artist=source_track.artist, title=source_track.title)
        if duplicate is not None:
            _emit(progress_callback, f"[{index}/{len(source_tracks)}] 채널 중복 제외: {source_track.label} -> {duplicate.title}")
            duplicate_skips.append(_build_duplicate_skip(source_track.label, duplicate))
            continue
        _emit(progress_callback, f"[{index}/{len(source_tracks)}] {source_track.label} 확인 중...")
        review_track = _resolve_review_track(source_track, output_mode=output_mode)
        duplicate = _match_channel_duplicate(channel_index, artist=review_track.artist, title=review_track.title)
        if duplicate is not None:
            _emit(progress_callback, f"[{index}/{len(source_tracks)}] 채널 중복 제외: {review_track.label} -> {duplicate.title}")
            duplicate_skips.append(_build_duplicate_skip(review_track.label, duplicate))
            continue
        review_tracks.append(review_track)
    return PlaylistImportReport(
        playlist_title=playlist_title,
        playlist_url=normalized_url,
        source_track_count=len(source_tracks),
        tracks=review_tracks,
        duplicate_skips=duplicate_skips,
    )


def extract_playlist_tracks(playlist_url: str) -> tuple[str, list[PlaylistSourceTrack]]:
    normalized_url = normalize_youtube_playlist_url(playlist_url)
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "extract_flat": False,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(normalized_url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("플레이리스트 메타데이터를 불러오지 못했습니다.")

    playlist_title = str(info.get("title") or "가져온 플레이리스트").strip()
    tracks: list[PlaylistSourceTrack] = []
    for position, entry in enumerate(info.get("entries") or [], start=1):
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "").strip()
        youtube_url = sanitize_youtube_url(str(entry.get("webpage_url") or entry.get("url") or ""))
        if not youtube_url and video_id:
            youtube_url = sanitize_youtube_url(f"https://www.youtube.com/watch?v={video_id}")
        if not youtube_url:
            continue

        artist = str(entry.get("artist") or "").strip() or _clean_uploader_name(str(entry.get("channel") or entry.get("uploader") or "").strip())
        album = str(entry.get("album") or "").strip() or _clean_playlist_title(playlist_title)
        thumbnail = str(entry.get("thumbnail") or "").strip()
        if not thumbnail:
            thumbnails = entry.get("thumbnails") or []
            if thumbnails and isinstance(thumbnails[-1], dict):
                thumbnail = str(thumbnails[-1].get("url") or "").strip()
        tracks.append(
            PlaylistSourceTrack(
                index=position,
                title=str(entry.get("track") or entry.get("title") or f"트랙 {position}").strip(),
                artist=artist,
                album=album,
                youtube_url=youtube_url,
                duration=_coerce_int(entry.get("duration")),
                thumbnail_url=thumbnail,
                playlist_title=playlist_title,
            )
        )
    if not tracks:
        raise RuntimeError("재생 가능한 트랙을 찾지 못했습니다.")
    return playlist_title, tracks


def normalize_youtube_playlist_url(url: str) -> str:
    parsed = urlparse(url.strip())
    playlist_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
    if not playlist_id:
        raise ValueError("list 파라미터가 포함된 플레이리스트 URL이 필요합니다.")
    return f"https://www.youtube.com/playlist?{urlencode({'list': playlist_id})}"


def save_review_track_lyrics(
    *,
    artist: str,
    title: str,
    lyrics_text: str,
    existing_path: Optional[str] = None,
) -> tuple[str, str]:
    ensure_data_dirs()
    os.makedirs(LYRICS_DIR, exist_ok=True)
    prepared_text = _preserve_lyric_text(lyrics_text)
    if existing_path:
        path = existing_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        filename = _sanitize_filename(f"{artist or 'Unknown'} - {title or 'Unknown'}")
        path = _build_available_lyrics_path(filename, prepared_text)
    with open(path, "w", encoding="utf-8") as lyric_file:
        lyric_file.write(prepared_text + "\n")
    return path, prepared_text


def _resolve_review_track(source_track: PlaylistSourceTrack, *, output_mode: OutputMode) -> PlaylistReviewTrack:
    track = PlaylistReviewTrack(
        source=source_track,
        title=source_track.title,
        artist=source_track.artist,
        album=source_track.album,
        youtube_url=source_track.youtube_url,
        album_art_url=source_track.thumbnail_url,
        duration=source_track.duration,
        note="자동 확인 대기 중입니다.",
    )
    try:
        best = _find_best_genie_result(source_track)
        if best is not None:
            title, song_id, extra_info, art_url, duration = best
            artist, album = parse_genie_extra_info(extra_info)
            track.title = title or track.title
            track.artist = artist or track.artist
            track.album = album or track.album
            track.album_art_url = art_url or track.album_art_url
            track.duration = duration or track.duration
            lyrics_result = get_best_lyrics_result(
                song_id=song_id,
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration=track.duration,
                youtube_url=track.youtube_url,
            )
        else:
            lyrics_result = get_best_lyrics_result(
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration=track.duration,
                youtube_url=track.youtube_url,
            )
    except Exception as exc:
        track.status = "error"
        track.note = f"자동 확인 실패: {exc}"
        return track

    lyrics_text = lyrics_result.text if lyrics_result else None
    if not lyrics_text:
        track.status = "missing_lyrics"
        track.note = "가사를 찾지 못했습니다. 직접 입력하거나 수정해 주세요."
        return track

    track.lrc_path, track.lyrics_text = save_review_track_lyrics(artist=track.artist, title=track.title, lyrics_text=lyrics_text)
    track.lyrics_mode = "synced" if lyrics_are_synced(track.lyrics_text) else "plain"
    track.lyrics_source = "generated"
    source_label = lyrics_result.source if lyrics_result else "자동 검색"
    if track.lyrics_mode == "synced":
        track.status = "ready"
        track.include_in_batch = True
        track.note = f"{source_label}에서 싱크 가사를 찾았습니다."
    else:
        track.status = "plain_lyrics"
        track.include_in_batch = False
        track.note = f"{source_label}에서 일반 가사만 찾았습니다. 싱크 작업 전에는 렌더하지 않습니다."
    return track


def _find_best_genie_result(source_track: PlaylistSourceTrack) -> Optional[tuple[str, str, str, str, Optional[int]]]:
    seen_song_ids: set[str] = set()
    candidates = []
    for query in _build_search_queries(source_track):
        for candidate in search_genie_songs(query, limit=6):
            song_id = str(candidate[1]).strip()
            if song_id in seen_song_ids:
                continue
            seen_song_ids.add(song_id)
            candidates.append(candidate)
    if not candidates:
        return None
    scored = [(_score_genie_candidate(source_track, candidate), candidate) for candidate in candidates]
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_candidate = scored[0]
    return best_candidate if best_score >= _minimum_genie_match_score(source_track) else None


def _build_search_queries(source_track: PlaylistSourceTrack) -> Iterable[str]:
    queries: list[str] = []
    if source_track.artist and source_track.album and source_track.title:
        queries.append(f"{source_track.artist} {source_track.album} {source_track.title}")
    if source_track.artist and source_track.title:
        queries.append(f"{source_track.artist} {source_track.title}")
    if not source_track.artist and source_track.album and source_track.title:
        queries.append(f"{source_track.album} {source_track.title}")
    if not source_track.artist and source_track.title:
        queries.append(source_track.title)

    seen_queries: set[str] = set()
    for query in queries:
        normalized_query = _normalize_text(query)
        if not normalized_query or normalized_query in seen_queries:
            continue
        seen_queries.add(normalized_query)
        yield query


def _score_genie_candidate(source_track: PlaylistSourceTrack, candidate: tuple[str, str, str, str, Optional[int]]) -> int:
    candidate_title, _, extra_info, _, candidate_duration = candidate
    candidate_artist, candidate_album = parse_genie_extra_info(extra_info)
    source_title = _normalize_text(source_track.title)
    source_artist = _normalize_text(_primary_artist(source_track.artist))
    source_album = _normalize_text(source_track.album)
    result_title = _normalize_text(candidate_title)
    result_artist = _normalize_text(_primary_artist(candidate_artist))
    result_album = _normalize_text(candidate_album)

    title_similarity = _similarity_score(source_title, result_title)
    artist_similarity = _similarity_score(source_artist, result_artist)
    album_similarity = _similarity_score(source_album, result_album)

    score = int(title_similarity * 55)
    if title_similarity >= 0.98:
        score += 15

    if source_artist and result_artist:
        score += int(artist_similarity * 30)
        if _normalized_texts_overlap(source_artist, result_artist):
            score += 20
        elif artist_similarity < 0.35 and _shares_comparable_script(source_track.artist, candidate_artist):
            score -= 35

    if source_album and result_album:
        score += int(album_similarity * 12)
        if _normalized_texts_overlap(source_album, result_album):
            score += 8

    if source_track.duration and candidate_duration:
        delta = abs(int(source_track.duration) - int(candidate_duration))
        if delta <= 2:
            score += 15
        elif delta <= 5:
            score += 10
        elif delta <= 10:
            score += 4
    return score


def _minimum_genie_match_score(source_track: PlaylistSourceTrack) -> int:
    return 78 if _normalize_text(_primary_artist(source_track.artist)) else 55


def _build_available_lyrics_path(filename: str, prepared_text: str) -> str:
    candidate = os.path.join(LYRICS_DIR, f"{filename}.lrc")
    if not os.path.exists(candidate):
        return candidate
    try:
        with open(candidate, "r", encoding="utf-8") as existing_file:
            if existing_file.read().strip() == prepared_text:
                return candidate
    except OSError:
        pass
    counter = 2
    while True:
        numbered = os.path.join(LYRICS_DIR, f"{filename}_{counter}.lrc")
        if not os.path.exists(numbered):
            return numbered
        counter += 1


def _match_channel_duplicate(
    channel_index: Optional[ChannelVideoIndex],
    *,
    artist: str,
    title: str,
) -> Optional[ChannelVideoRecord]:
    return find_duplicate_channel_video(channel_index, artist=artist, title=title)


def _build_duplicate_skip(source_label: str, duplicate: ChannelVideoRecord) -> PlaylistDuplicateSkip:
    return PlaylistDuplicateSkip(
        source_label=source_label,
        matched_video_title=duplicate.title,
        matched_video_url=duplicate.video_url,
    )


def _emit(progress_callback: Optional[Callable[[str], None]], message: str) -> None:
    if progress_callback:
        progress_callback(message)


def _clean_uploader_name(value: str) -> str:
    return value[:-8].strip() if value.endswith(" - Topic") else value.strip()


def _clean_playlist_title(value: str) -> str:
    return value[8:].strip() if value.lower().startswith("album - ") else value.strip()


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


def _normalized_texts_overlap(left: str, right: str) -> bool:
    return bool(left and right and (left in right or right in left))


def _shares_comparable_script(left: str, right: str) -> bool:
    left = left or ""
    right = right or ""
    if not left.strip() or not right.strip():
        return False
    shares_hangul = bool(_HANGUL_PATTERN.search(left) and _HANGUL_PATTERN.search(right))
    shares_latin = bool(_LATIN_PATTERN.search(left) and _LATIN_PATTERN.search(right))
    return shares_hangul or shares_latin


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", filename)


def _preserve_lyric_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _coerce_int(value: object) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
