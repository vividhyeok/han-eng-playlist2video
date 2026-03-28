"""Playlist extraction and review-track preparation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import yt_dlp

from app.config.paths import LYRICS_DIR, ensure_data_dirs
from app.lyrics.lyric_text_utils import normalize_lyric_text, prepare_lyric_text_for_subtitles
from app.pipeline.process_manager import OutputMode, ProcessConfig
from app.sources.genie_handler import get_best_lyrics, lyrics_are_synced, parse_genie_extra_info, search_genie_songs
from app.sources.youtube_handler import sanitize_youtube_url

_NORMALIZE_PATTERN = re.compile(r"[^0-9a-z\uac00-\ud7a3]+", re.IGNORECASE)
_ARTIST_SPLIT_PATTERN = re.compile(r"\s*(?:,|&|/| x | X | feat\.?|ft\.?|with|and)\s*", re.IGNORECASE)


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

    def can_render(self) -> bool:
        return bool(self.title.strip() and self.artist.strip() and self.youtube_url.strip() and self.lrc_path and os.path.exists(self.lrc_path))

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
class PlaylistImportReport:
    playlist_title: str
    playlist_url: str
    tracks: list[PlaylistReviewTrack]


def import_playlist(
    playlist_url: str,
    *,
    output_mode: OutputMode = "video",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> PlaylistImportReport:
    normalized_url = normalize_youtube_playlist_url(playlist_url)
    playlist_title, source_tracks = extract_playlist_tracks(normalized_url)
    review_tracks = []
    for index, source_track in enumerate(source_tracks, start=1):
        _emit(progress_callback, f"[{index}/{len(source_tracks)}] {source_track.label} 확인 중...")
        review_tracks.append(_resolve_review_track(source_track, output_mode=output_mode))
    return PlaylistImportReport(playlist_title=playlist_title, playlist_url=normalized_url, tracks=review_tracks)


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


def save_review_track_lyrics(*, artist: str, title: str, lyrics_text: str) -> tuple[str, str]:
    ensure_data_dirs()
    os.makedirs(LYRICS_DIR, exist_ok=True)
    prepared_text = lyrics_text.strip()
    if not lyrics_are_synced(prepared_text):
        prepared_text = prepare_lyric_text_for_subtitles(normalize_lyric_text(prepared_text))
    prepared_text = prepared_text.strip()
    filename = _sanitize_filename(f"{artist or 'Unknown'} - {title or 'Unknown'}")
    path = _build_available_lyrics_path(filename, prepared_text)
    with open(path, "w", encoding="utf-8") as lyric_file:
        lyric_file.write(prepared_text + "\n")
    return path, prepared_text


def load_review_track_lyrics_file(path: str) -> tuple[str, str]:
    lyrics_text = _read_lyrics_text_file(path).strip()
    if not lyrics_text:
        return "", "missing"
    return lyrics_text, "synced" if lyrics_are_synced(lyrics_text) else "plain"


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
            lyrics_text = get_best_lyrics(song_id=song_id, title=track.title, artist=track.artist, album=track.album, duration=track.duration)
        else:
            lyrics_text = get_best_lyrics(title=track.title, artist=track.artist, album=track.album, duration=track.duration)
    except Exception as exc:
        track.status = "error"
        track.note = f"자동 확인 실패: {exc}"
        return track

    if not lyrics_text:
        track.status = "missing_lyrics"
        track.note = "가사를 찾지 못했습니다. 직접 입력하거나 수정해 주세요."
        return track

    track.lrc_path, track.lyrics_text = save_review_track_lyrics(artist=track.artist, title=track.title, lyrics_text=lyrics_text)
    track.lyrics_mode = "synced" if lyrics_are_synced(track.lyrics_text) else "plain"
    track.lyrics_source = "generated"
    track.status = "ready"
    track.include_in_batch = True
    track.note = "싱크 가사를 찾았습니다." if track.lyrics_mode == "synced" else "일반 가사를 찾았습니다."
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
    return best_candidate if best_score >= 55 else None


def _build_search_queries(source_track: PlaylistSourceTrack) -> Iterable[str]:
    if source_track.artist and source_track.title:
        yield f"{source_track.artist} {source_track.title}"
    if source_track.title:
        yield source_track.title
    if source_track.artist and source_track.album and source_track.title:
        yield f"{source_track.artist} {source_track.album} {source_track.title}"


def _score_genie_candidate(source_track: PlaylistSourceTrack, candidate: tuple[str, str, str, str, Optional[int]]) -> int:
    candidate_title, _, extra_info, _, candidate_duration = candidate
    candidate_artist, _ = parse_genie_extra_info(extra_info)
    source_title = _normalize_text(source_track.title)
    source_artist = _normalize_text(_primary_artist(source_track.artist))
    result_title = _normalize_text(candidate_title)
    result_artist = _normalize_text(_primary_artist(candidate_artist))
    score = int(SequenceMatcher(None, source_title, result_title).ratio() * 70)
    if source_title == result_title:
        score += 25
    if source_artist and result_artist:
        score += int(SequenceMatcher(None, source_artist, result_artist).ratio() * 25)
    if source_track.duration and candidate_duration:
        delta = abs(int(source_track.duration) - int(candidate_duration))
        if delta <= 2:
            score += 20
        elif delta <= 5:
            score += 12
        elif delta <= 10:
            score += 6
    return score


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


def _read_lyrics_text_file(path: str) -> str:
    encodings = ("utf-8-sig", "utf-8", "cp949", "euc-kr")
    last_error: Optional[Exception] = None
    for encoding in encodings:
        try:
            with open(path, "r", encoding=encoding) as lyric_file:
                return lyric_file.read().replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise ValueError(f"가사 파일 인코딩을 읽지 못했습니다: {path}") from last_error
    with open(path, "r", encoding="utf-8") as lyric_file:
        return lyric_file.read().replace("\r\n", "\n").replace("\r", "\n")


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


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", filename)


def _coerce_int(value: object) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
