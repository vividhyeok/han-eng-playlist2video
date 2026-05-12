"""Genie and fallback lyric helpers."""

from __future__ import annotations

import os
import re
import traceback
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from genieapi import GenieAPI

try:
    import syncedlyrics
except ImportError:  # pragma: no cover
    syncedlyrics = None

try:
    from ytmusicapi import YTMusic
except ImportError:  # pragma: no cover
    YTMusic = None

GENIE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}

LRCLIB_BASE_URL = "https://lrclib.net/api"
SYNCEDLYRICS_PROVIDERS = ["Musixmatch", "NetEase", "Megalobiz", "Genius"]
_NORMALIZE_PATTERN = re.compile(r"[^0-9a-z\uac00-\ud7a3]+", re.IGNORECASE)
_ARTIST_SPLIT_PATTERN = re.compile(r"\s*(?:,|&|/| x | X | feat\.?|ft\.?|with|and)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class LyricsFetchResult:
    text: str
    source: str
    lyrics_mode: str


def search_genie_songs(
    query: str,
    limit: int = 4,
) -> List[Tuple[str, str, str, str, Optional[int]]]:
    """Search songs on Genie and return tuples used by the UI."""

    try:
        genie = GenieAPI()
        songs = genie.search_song(query, limit=limit)
    except Exception as exc:
        print(f"[ERROR] Genie search failed: {exc}")
        traceback.print_exc()
        return []

    results: List[Tuple[str, str, str, str, Optional[int]]] = []
    for song in songs:
        try:
            if isinstance(song, dict):
                title = str(song.get("title", "")).strip()
                song_id = str(song.get("id") or song.get("song_id") or "").strip()
                artist = str(song.get("artist", "")).strip()
                album = str(song.get("album") or song.get("album_name") or "").strip()
                extra_info = f"{artist} - {album}" if album else artist
                fallback_art = str(song.get("thumbnail") or "").strip()
            else:
                unpacked = list(song)
                title = str(unpacked[0]).strip()
                song_id = str(unpacked[1]).strip()
                extra_info = str(unpacked[2]).strip() if len(unpacked) > 2 else ""
                fallback_art = ""

            album_art_url, duration = get_song_details(song_id)
            results.append(
                (
                    title,
                    song_id,
                    extra_info,
                    album_art_url or fallback_art,
                    duration,
                )
            )
        except Exception as exc:
            print(f"[WARN] Failed to normalize Genie result: {exc}")

    return results


def parse_genie_extra_info(extra_info: str) -> Tuple[str, str]:
    if not extra_info:
        return "", ""
    artist, _, album = extra_info.partition(" - ")
    return artist.strip(), album.strip()


def normalize_lyrics_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").splitlines()).strip()


def lyrics_are_synced(text: str) -> bool:
    if not text:
        return False
    return any(line.startswith("[") and "]" in line for line in text.splitlines())


def get_genie_lyrics(song_id: str) -> Optional[str]:
    """Fetch lyrics from Genie. Some GenieAPI versions return a file path."""

    if not song_id:
        return None

    try:
        genie = GenieAPI()
        lyrics = genie.get_lyrics(song_id)
    except Exception as exc:
        print(f"[WARN] Genie lyric fetch failed for {song_id}: {exc}")
        return None

    if not lyrics:
        return None

    if isinstance(lyrics, str):
        potential_paths = [lyrics, os.path.join(os.getcwd(), lyrics)]
        for potential_path in potential_paths:
            if os.path.isfile(potential_path):
                try:
                    with open(potential_path, "r", encoding="utf-8") as lyric_file:
                        return normalize_lyrics_text(lyric_file.read())
                except Exception as exc:
                    print(f"[WARN] Failed to read Genie lyric file {potential_path}: {exc}")

        return normalize_lyrics_text(lyrics)

    return None


def get_best_lyrics(
    *,
    song_id: str = "",
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: Optional[int] = None,
    youtube_url: str = "",
) -> Optional[str]:
    result = get_best_lyrics_result(
        song_id=song_id,
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        youtube_url=youtube_url,
    )
    return result.text if result else None


def get_best_lyrics_result(
    *,
    song_id: str = "",
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: Optional[int] = None,
    youtube_url: str = "",
    allow_fuzzy_match: bool = False,
) -> Optional[LyricsFetchResult]:
    """Return the best available lyric text and source, preferring synced lyrics."""

    genie_lyrics = get_genie_lyrics(song_id) if song_id else None
    if lyrics_are_synced(genie_lyrics or ""):
        return LyricsFetchResult(genie_lyrics or "", "Genie", "synced")

    lrclib_synced = get_lrclib_lyrics(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        prefer_synced=True,
    )
    if lrclib_synced and lyrics_are_synced(lrclib_synced):
        return LyricsFetchResult(lrclib_synced, "LRCLIB", "synced")

    ytmusic_synced = get_ytmusic_lyrics(
        youtube_url=youtube_url,
        timestamps=True,
        title=title,
        artist=artist,
    )
    if ytmusic_synced:
        return LyricsFetchResult(ytmusic_synced, "YouTube Music", "synced")

    if allow_fuzzy_match:
        syncedlyrics_synced = get_syncedlyrics_result(
            title=title,
            artist=artist,
            prefer_synced=True,
        )
        if syncedlyrics_synced and syncedlyrics_synced.lyrics_mode == "synced":
            return syncedlyrics_synced

    if genie_lyrics:
        return LyricsFetchResult(genie_lyrics, "Genie", "plain")

    ytmusic_plain = get_ytmusic_lyrics(
        youtube_url=youtube_url,
        timestamps=False,
        title=title,
        artist=artist,
    )
    if ytmusic_plain:
        return LyricsFetchResult(ytmusic_plain, "YouTube Music", "plain")

    if allow_fuzzy_match:
        syncedlyrics_plain = get_syncedlyrics_result(
            title=title,
            artist=artist,
            prefer_synced=False,
        )
        if syncedlyrics_plain:
            return syncedlyrics_plain

    lrclib_plain = get_lrclib_lyrics(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        prefer_synced=False,
    )
    if lrclib_plain:
        mode = "synced" if lyrics_are_synced(lrclib_plain) else "plain"
        return LyricsFetchResult(lrclib_plain, "LRCLIB", mode)

    return None


def get_lrclib_lyrics(
    *,
    title: str,
    artist: str,
    album: str = "",
    duration: Optional[int] = None,
    prefer_synced: bool = True,
) -> Optional[str]:
    """Fetch lyrics from lrclib.net.

    This uses the documented public query shape exposed by lrclib wrappers:
    track name, artist name, optional album name, and optional duration.
    """

    if not title or not artist:
        return None

    params: Dict[str, Any] = {
        "track_name": title,
        "artist_name": artist,
    }
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)

    candidate_payloads: List[Dict[str, Any]] = []

    response = _request_lrclib("get", params)
    if isinstance(response, dict):
        candidate_payloads.append(response)

    search_results = _request_lrclib("search", params)
    if isinstance(search_results, list):
        candidate_payloads.extend(item for item in search_results if isinstance(item, dict))

    scored_candidates: List[Tuple[int, str]] = []
    for payload in candidate_payloads:
        candidate = _extract_lrclib_candidate(
            payload,
            requested_title=title,
            requested_artist=artist,
            requested_album=album,
            requested_duration=duration,
            prefer_synced=prefer_synced,
        )
        if candidate is not None:
            scored_candidates.append(candidate)

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_lyrics = scored_candidates[0]
    minimum_score = 78 if _normalize_text(_primary_artist(artist)) else 70
    return best_lyrics if best_score >= minimum_score else None


def get_ytmusic_lyrics(
    *,
    youtube_url: str,
    timestamps: bool,
    title: str = "",
    artist: str = "",
) -> Optional[str]:
    if YTMusic is None or not youtube_url:
        return None

    video_id = _extract_video_id(youtube_url)
    if not video_id:
        return None

    try:
        ytmusic = YTMusic()
        watch = ytmusic.get_watch_playlist(videoId=video_id, limit=1)
        if not _ytmusic_watch_matches_track(watch, expected_title=title, expected_artist=artist):
            return None
        browse_id = str(watch.get("lyrics") or "").strip()
        if not browse_id:
            return None
        lyrics = ytmusic.get_lyrics(browse_id, timestamps=timestamps)
    except Exception as exc:
        print(f"[WARN] YouTube Music lyric fetch failed: {exc}")
        return None

    if not isinstance(lyrics, dict):
        return None

    payload = lyrics.get("lyrics")
    has_timestamps = bool(lyrics.get("hasTimestamps"))
    if isinstance(payload, list) and has_timestamps:
        lines = []
        for line in payload:
            start_time = getattr(line, "start_time", None)
            text = getattr(line, "text", None)
            if start_time is None or not text:
                continue
            lines.append(f"[{_format_lrc_timestamp(int(start_time))}]{str(text).strip()}")
        return normalize_lyrics_text("\n".join(lines)) or None

    if isinstance(payload, str):
        normalized = normalize_lyrics_text(payload)
        if normalized:
            return normalized
    return None


def get_syncedlyrics_result(
    *,
    title: str,
    artist: str,
    prefer_synced: bool,
) -> Optional[LyricsFetchResult]:
    if syncedlyrics is None or not title or not artist:
        return None

    search_term = f"{artist} {title}".strip()
    try:
        lyrics = syncedlyrics.search(
            search_term,
            synced_only=prefer_synced,
            providers=SYNCEDLYRICS_PROVIDERS,
        )
    except Exception as exc:
        print(f"[WARN] syncedlyrics lookup failed: {exc}")
        return None

    normalized = normalize_lyrics_text(str(lyrics or ""))
    if not normalized:
        return None
    mode = "synced" if lyrics_are_synced(normalized) else "plain"
    return LyricsFetchResult(normalized, "SyncedLyrics", mode)


def _request_lrclib(endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
    url = f"{LRCLIB_BASE_URL}/{endpoint}"
    try:
        response = requests.get(url, params=params, headers=GENIE_HEADERS, timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[WARN] lrclib request failed ({endpoint}): {exc}")
        return None


def _extract_lrclib_candidate(
    payload: Dict[str, Any],
    *,
    requested_title: str,
    requested_artist: str,
    requested_album: str,
    requested_duration: Optional[int],
    prefer_synced: bool,
) -> Optional[Tuple[int, str]]:
    if not isinstance(payload, dict):
        return None

    candidate_title = str(payload.get("trackName") or payload.get("track_name") or "").strip()
    candidate_artist = str(payload.get("artistName") or payload.get("artist_name") or "").strip()
    candidate_album = str(payload.get("albumName") or payload.get("album_name") or "").strip()
    candidate_duration = _coerce_int(payload.get("duration"))

    if not candidate_title or not candidate_artist:
        return None

    synced = normalize_lyrics_text(str(payload.get("syncedLyrics") or ""))
    plain = normalize_lyrics_text(str(payload.get("plainLyrics") or ""))

    lyrics = ""
    if prefer_synced and synced:
        lyrics = synced
    elif plain:
        lyrics = plain
    elif synced:
        lyrics = synced

    if not lyrics:
        return None

    score = _score_track_match(
        expected_title=requested_title,
        expected_artist=requested_artist,
        expected_album=requested_album,
        expected_duration=requested_duration,
        candidate_title=candidate_title,
        candidate_artist=candidate_artist,
        candidate_album=candidate_album,
        candidate_duration=candidate_duration,
    )
    return score, lyrics


def _extract_video_id(youtube_url: str) -> str:
    parsed = urlparse(youtube_url.strip())
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0]
    query = parse_qs(parsed.query)
    if "v" in query and query["v"]:
        return query["v"][0]
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        return path_parts[1]
    return ""


def _format_lrc_timestamp(milliseconds: int) -> str:
    total_hundredths = max(0, milliseconds // 10)
    minutes = total_hundredths // 6000
    seconds = (total_hundredths % 6000) // 100
    hundredths = total_hundredths % 100
    return f"{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def _ytmusic_watch_matches_track(
    watch_payload: Dict[str, Any],
    *,
    expected_title: str,
    expected_artist: str,
) -> bool:
    tracks = watch_payload.get("tracks") or []
    if not tracks or not isinstance(tracks[0], dict):
        return True

    track = tracks[0]
    candidate_title = str(track.get("title") or "").strip()
    artist_items = track.get("artists") or []
    candidate_artist = ", ".join(
        str(item.get("name") or "").strip()
        for item in artist_items
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    )
    score = _score_track_match(
        expected_title=expected_title,
        expected_artist=expected_artist,
        expected_album="",
        expected_duration=None,
        candidate_title=candidate_title,
        candidate_artist=candidate_artist,
        candidate_album="",
        candidate_duration=None,
    )
    minimum_score = 78 if _normalize_text(_primary_artist(expected_artist)) else 70
    return score >= minimum_score


def _score_track_match(
    *,
    expected_title: str,
    expected_artist: str,
    expected_album: str,
    expected_duration: Optional[int],
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_duration: Optional[int],
) -> int:
    normalized_expected_title = _normalize_text(expected_title)
    normalized_expected_artist = _normalize_text(_primary_artist(expected_artist))
    normalized_expected_album = _normalize_text(expected_album)
    normalized_candidate_title = _normalize_text(candidate_title)
    normalized_candidate_artist = _normalize_text(_primary_artist(candidate_artist))
    normalized_candidate_album = _normalize_text(candidate_album)

    if not normalized_expected_title or not normalized_candidate_title:
        return 0

    title_similarity = _similarity_score(normalized_expected_title, normalized_candidate_title)
    artist_similarity = _similarity_score(normalized_expected_artist, normalized_candidate_artist)
    album_similarity = _similarity_score(normalized_expected_album, normalized_candidate_album)

    score = int(title_similarity * 60)
    if _normalized_texts_overlap(normalized_expected_title, normalized_candidate_title):
        score += 15

    if normalized_expected_artist and normalized_candidate_artist:
        score += int(artist_similarity * 28)
        if _normalized_texts_overlap(normalized_expected_artist, normalized_candidate_artist):
            score += 12
        elif artist_similarity < 0.35:
            score -= 25

    if normalized_expected_album and normalized_candidate_album:
        score += int(album_similarity * 10)

    if expected_duration and candidate_duration:
        delta = abs(int(expected_duration) - int(candidate_duration))
        if delta <= 2:
            score += 10
        elif delta <= 5:
            score += 6
        elif delta <= 10:
            score += 2

    return score


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


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def get_song_details(song_id: str) -> Tuple[Optional[str], Optional[int]]:
    """Fetch album art URL and duration from the public Genie song page."""

    if not song_id:
        return None, None

    try:
        song_url = f"https://www.genie.co.kr/detail/songInfo?xgnm={song_id}"
        response = requests.get(song_url, headers=GENIE_HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        print(f"[WARN] Failed to load Genie song page for {song_id}: {exc}")
        return None, None

    album_art_url = None
    duration = None

    album_img = soup.select_one("div.photo-zone span.cover > img")
    if album_img and album_img.get("src"):
        image_url = album_img["src"]
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        album_art_url = image_url.replace("/dims/resize/Q_80,0", "")

    for item in soup.select("ul.info-data li"):
        title_span = item.select_one("span.title")
        value_span = item.select_one("span.value")
        if not title_span or not value_span:
            continue
        if "재생시간" not in title_span.get_text(strip=True):
            continue
        try:
            minutes, seconds = value_span.get_text(strip=True).split(":")
            duration = int(minutes) * 60 + int(seconds)
        except ValueError:
            duration = None
        break

    return album_art_url, duration


def get_album_arts_url(song_id: str) -> List[str]:
    """Return album art candidates for a Genie song."""

    art_url, _ = get_song_details(song_id)
    return [art_url] if art_url else []


def get_song_album_id_and_art_url(song_id: str) -> Optional[Tuple[str, str]]:
    """Return album art URL and a derived album identifier for compatibility."""

    art_url, _ = get_song_details(song_id)
    if not art_url:
        return None
    album_id = os.path.splitext(os.path.basename(art_url))[0]
    return art_url, album_id
