"""Playlist extraction and queue preparation helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable, Literal, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import yt_dlp

from app.config.paths import LYRICS_DIR, ensure_data_dirs
from app.lyrics.lyric_text_utils import (
    normalize_lyric_text,
    prepare_lyric_text_for_subtitles,
)
from app.pipeline.process_manager import OutputMode, ProcessConfig
from app.sources.genie_handler import (
    get_best_lyrics,
    lyrics_are_synced,
    parse_genie_extra_info,
    search_genie_songs,
)
from app.sources.youtube_handler import sanitize_youtube_url

LyricsPolicy = Literal["allow_plain", "require_synced"]

_NORMALIZE_PATTERN = re.compile(r"[^0-9a-z\uac00-\ud7a3]+", re.IGNORECASE)
_ARTIST_SPLIT_PATTERN = re.compile(
    r"\s*(?:,|&|/| x | X | feat\.?|ft\.?|with|and)\s*",
    re.IGNORECASE,
)


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
        artist = self.artist or "Unknown artist"
        title = self.title or "Unknown title"
        return f"{artist} - {title}"


@dataclass(frozen=True)
class PlaylistPreparedTrack:
    config: ProcessConfig
    label: str
    lyrics_mode: str
    source: PlaylistSourceTrack


@dataclass(frozen=True)
class PlaylistSkippedTrack:
    source: PlaylistSourceTrack
    reason: str


@dataclass(frozen=True)
class PlaylistImportReport:
    playlist_title: str
    playlist_url: str
    prepared_tracks: list[PlaylistPreparedTrack]
    skipped_tracks: list[PlaylistSkippedTrack]


def import_playlist(
    playlist_url: str,
    *,
    output_mode: OutputMode = "video",
    lyrics_policy: LyricsPolicy = "allow_plain",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> PlaylistImportReport:
    normalized_url = normalize_youtube_playlist_url(playlist_url)
    playlist_title, source_tracks = extract_playlist_tracks(normalized_url)

    prepared_tracks: list[PlaylistPreparedTrack] = []
    skipped_tracks: list[PlaylistSkippedTrack] = []

    for index, source_track in enumerate(source_tracks, start=1):
        _emit_progress(
            progress_callback,
            f"[Playlist {index}/{len(source_tracks)}] Resolving {source_track.label}",
        )
        try:
            prepared_track = _prepare_track(
                source_track,
                output_mode=output_mode,
                lyrics_policy=lyrics_policy,
            )
        except Exception as exc:
            skipped_tracks.append(
                PlaylistSkippedTrack(source=source_track, reason=f"Resolution failed: {exc}")
            )
            continue

        if prepared_track is None:
            skipped_tracks.append(
                PlaylistSkippedTrack(
                    source=source_track,
                    reason=_build_skip_reason(source_track, lyrics_policy),
                )
            )
            continue

        prepared_tracks.append(prepared_track)

    return PlaylistImportReport(
        playlist_title=playlist_title,
        playlist_url=normalized_url,
        prepared_tracks=prepared_tracks,
        skipped_tracks=skipped_tracks,
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
        "playlistreverse": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(normalized_url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("The supplied URL did not return playlist metadata.")

    playlist_title = str(info.get("title") or "Imported Playlist").strip()
    entries = info.get("entries") or []
    tracks: list[PlaylistSourceTrack] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue

        video_id = str(entry.get("id") or "").strip()
        youtube_url = sanitize_youtube_url(
            str(entry.get("webpage_url") or entry.get("url") or "")
        )
        if not youtube_url and video_id:
            youtube_url = sanitize_youtube_url(f"https://www.youtube.com/watch?v={video_id}")
        if not youtube_url:
            continue

        track_title = str(entry.get("track") or entry.get("title") or "").strip()
        artist = str(entry.get("artist") or "").strip()
        if not artist:
            artist = _clean_uploader_name(
                str(entry.get("channel") or entry.get("uploader") or "").strip()
            )
        album = str(entry.get("album") or "").strip()
        if not album:
            album = _clean_playlist_title(playlist_title)

        thumbnail = str(entry.get("thumbnail") or "").strip()
        if not thumbnail:
            thumbnails = entry.get("thumbnails") or []
            if thumbnails and isinstance(thumbnails[-1], dict):
                thumbnail = str(thumbnails[-1].get("url") or "").strip()

        tracks.append(
            PlaylistSourceTrack(
                index=position,
                title=track_title or f"Track {position}",
                artist=artist,
                album=album,
                youtube_url=youtube_url,
                duration=_coerce_int(entry.get("duration")),
                thumbnail_url=thumbnail,
                playlist_title=playlist_title,
            )
        )

    if not tracks:
        raise RuntimeError("No playable tracks were found in the supplied playlist.")

    return playlist_title, tracks


def normalize_youtube_playlist_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [""])[0].strip()

    if not playlist_id:
        raise ValueError("A YouTube playlist URL with a list parameter is required.")

    return f"https://www.youtube.com/playlist?{urlencode({'list': playlist_id})}"


def _prepare_track(
    source_track: PlaylistSourceTrack,
    *,
    output_mode: OutputMode,
    lyrics_policy: LyricsPolicy,
) -> Optional[PlaylistPreparedTrack]:
    best_result = _find_best_genie_result(source_track)

    resolved_title = source_track.title
    resolved_artist = source_track.artist
    resolved_album = source_track.album
    album_art_url = source_track.thumbnail_url
    duration = source_track.duration
    lyrics_text: Optional[str] = None

    if best_result is not None:
        result_title, song_id, extra_info, genie_art_url, genie_duration = best_result
        genie_artist, genie_album = parse_genie_extra_info(extra_info)
        resolved_title = result_title or resolved_title
        resolved_artist = genie_artist or resolved_artist
        resolved_album = genie_album or resolved_album
        album_art_url = genie_art_url or album_art_url
        duration = genie_duration or duration
        lyrics_text = get_best_lyrics(
            song_id=song_id,
            title=resolved_title,
            artist=resolved_artist,
            album=resolved_album,
            duration=duration,
        )

    if not lyrics_text:
        lyrics_text = get_best_lyrics(
            title=source_track.title,
            artist=source_track.artist,
            album=source_track.album,
            duration=source_track.duration,
        )

    if not lyrics_text:
        return None

    synced = lyrics_are_synced(lyrics_text)
    if lyrics_policy == "require_synced" and not synced:
        return None

    lrc_path = _save_lyrics_file(
        artist=resolved_artist or source_track.artist,
        title=resolved_title or source_track.title,
        lyrics_text=lyrics_text,
    )
    display_artist = resolved_artist or source_track.artist or "Unknown artist"
    display_title = resolved_title or source_track.title or "Unknown title"

    config = ProcessConfig(
        title=display_title,
        artist=display_artist,
        album_art_url=album_art_url,
        youtube_url=source_track.youtube_url,
        output_mode=output_mode,
        lrc_path=lrc_path,
        prefer_youtube=True,
    )

    return PlaylistPreparedTrack(
        config=config,
        label=f"{display_artist} - {display_title} [{output_mode}]",
        lyrics_mode="synced" if synced else "plain",
        source=source_track,
    )


def _find_best_genie_result(
    source_track: PlaylistSourceTrack,
) -> Optional[tuple[str, str, str, str, Optional[int]]]:
    seen_song_ids: set[str] = set()
    candidates: list[tuple[str, str, str, str, Optional[int]]] = []

    for query in _build_search_queries(source_track):
        for candidate in search_genie_songs(query, limit=6):
            song_id = str(candidate[1]).strip()
            if song_id in seen_song_ids:
                continue
            seen_song_ids.add(song_id)
            candidates.append(candidate)

    if not candidates:
        return None

    scored_candidates = [
        (_score_genie_candidate(source_track, candidate), candidate)
        for candidate in candidates
    ]
    scored_candidates.sort(key=lambda item: item[0], reverse=True)

    best_score, best_candidate = scored_candidates[0]
    return best_candidate if best_score >= 55 else None


def _build_search_queries(source_track: PlaylistSourceTrack) -> Iterable[str]:
    artist = source_track.artist.strip()
    title = source_track.title.strip()
    album = source_track.album.strip()

    queries = []
    if artist and title:
        queries.append(f"{artist} {title}")
    if title:
        queries.append(title)
    if artist and album and title:
        queries.append(f"{artist} {album} {title}")
    return queries


def _score_genie_candidate(
    source_track: PlaylistSourceTrack,
    candidate: tuple[str, str, str, str, Optional[int]],
) -> int:
    candidate_title, _, extra_info, _, candidate_duration = candidate
    candidate_artist, _candidate_album = parse_genie_extra_info(extra_info)

    source_title = _normalize_text(source_track.title)
    source_artist = _normalize_text(_primary_artist(source_track.artist))
    result_title = _normalize_text(candidate_title)
    result_artist = _normalize_text(_primary_artist(candidate_artist))

    score = int(SequenceMatcher(None, source_title, result_title).ratio() * 70)
    if source_title and source_title == result_title:
        score += 25
    elif source_title and source_title in result_title:
        score += 15

    if source_artist and result_artist:
        artist_ratio = SequenceMatcher(None, source_artist, result_artist).ratio()
        score += int(artist_ratio * 25)
        if source_artist == result_artist:
            score += 15
        elif source_artist in result_artist or result_artist in source_artist:
            score += 8

    if source_track.duration and candidate_duration:
        delta = abs(int(source_track.duration) - int(candidate_duration))
        if delta <= 2:
            score += 20
        elif delta <= 5:
            score += 12
        elif delta <= 10:
            score += 6
        elif delta > 25:
            score -= 10

    return score


def _save_lyrics_file(*, artist: str, title: str, lyrics_text: str) -> str:
    ensure_data_dirs()
    os.makedirs(LYRICS_DIR, exist_ok=True)

    filename = _sanitize_filename(f"{artist or 'Unknown'} - {title or 'Unknown'}")
    path = _build_available_lyrics_path(filename, lyrics_text)

    prepared_text = lyrics_text.strip()
    if not lyrics_are_synced(prepared_text):
        prepared_text = prepare_lyric_text_for_subtitles(
            normalize_lyric_text(prepared_text)
        )

    with open(path, "w", encoding="utf-8") as lyric_file:
        lyric_file.write(prepared_text.strip() + "\n")

    return path


def _build_available_lyrics_path(filename: str, lyrics_text: str) -> str:
    candidate = os.path.join(LYRICS_DIR, f"{filename}.lrc")
    if not os.path.exists(candidate):
        return candidate

    try:
        with open(candidate, "r", encoding="utf-8") as existing_file:
            if existing_file.read().strip() == lyrics_text.strip():
                return candidate
    except OSError:
        pass

    counter = 2
    while True:
        numbered = os.path.join(LYRICS_DIR, f"{filename}_{counter}.lrc")
        if not os.path.exists(numbered):
            return numbered
        counter += 1


def _build_skip_reason(
    source_track: PlaylistSourceTrack,
    lyrics_policy: LyricsPolicy,
) -> str:
    if lyrics_policy == "require_synced":
        return "No synced lyrics were found."
    return "No lyrics were found."


def _emit_progress(
    progress_callback: Optional[Callable[[str], None]],
    message: str,
) -> None:
    if progress_callback:
        progress_callback(message)


def _clean_uploader_name(value: str) -> str:
    cleaned = value.strip()
    if cleaned.endswith(" - Topic"):
        cleaned = cleaned[:-8].strip()
    return cleaned


def _clean_playlist_title(value: str) -> str:
    cleaned = value.strip()
    if cleaned.lower().startswith("album - "):
        cleaned = cleaned[8:].strip()
    return cleaned


def _primary_artist(value: str) -> str:
    if not value:
        return ""
    artists = [part.strip() for part in _ARTIST_SPLIT_PATTERN.split(value) if part.strip()]
    return artists[0] if artists else value.strip()


def _normalize_text(value: str) -> str:
    return _NORMALIZE_PATTERN.sub("", value.strip().lower())


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", filename)


def _coerce_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
