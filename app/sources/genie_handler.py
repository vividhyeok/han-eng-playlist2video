"""Genie and fallback lyric helpers."""

from __future__ import annotations

import os
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from genieapi import GenieAPI

GENIE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}

LRCLIB_BASE_URL = "https://lrclib.net/api"


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
) -> Optional[str]:
    """Return the best available lyric text, preferring synced lyrics."""

    genie_lyrics = get_genie_lyrics(song_id) if song_id else None
    if lyrics_are_synced(genie_lyrics or ""):
        return genie_lyrics

    lrclib_lyrics = get_lrclib_lyrics(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        prefer_synced=True,
    )
    if lrclib_lyrics:
        return lrclib_lyrics

    if genie_lyrics:
        return genie_lyrics

    return get_lrclib_lyrics(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        prefer_synced=False,
    )


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

    response = _request_lrclib("get", params)
    if response:
        lyrics = _extract_lrclib_lyrics(response, prefer_synced=prefer_synced)
        if lyrics:
            return lyrics

    search_results = _request_lrclib("search", params)
    if isinstance(search_results, list):
        for item in search_results:
            lyrics = _extract_lrclib_lyrics(item, prefer_synced=prefer_synced)
            if lyrics:
                return lyrics

    return None


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


def _extract_lrclib_lyrics(
    payload: Dict[str, Any],
    *,
    prefer_synced: bool,
) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    synced = normalize_lyrics_text(str(payload.get("syncedLyrics") or ""))
    plain = normalize_lyrics_text(str(payload.get("plainLyrics") or ""))

    if prefer_synced and synced:
        return synced
    if plain:
        return plain
    if synced:
        return synced
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
