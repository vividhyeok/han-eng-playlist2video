"""Lyric source helpers.

Provider order favors synchronized data: Genie -> LRCLIB -> optional Musixmatch.
Musixmatch is used only when MUSIXMATCH_API_KEY is configured.
"""
from __future__ import annotations

import os
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from genieapi import GenieAPI

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36"}
LRCLIB_BASE_URL = "https://lrclib.net/api"
MUSIXMATCH_BASE_URL = "https://api.musixmatch.com/ws/1.1"


def search_genie_songs(query: str, limit: int = 4) -> List[Tuple[str, str, str, str, Optional[int]]]:
    try:
        songs = GenieAPI().search_song(query, limit=limit)
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
                extra = f"{artist} - {album}" if album else artist
                fallback_art = str(song.get("thumbnail") or "").strip()
            else:
                unpacked = list(song)
                title = str(unpacked[0]).strip()
                song_id = str(unpacked[1]).strip()
                extra = str(unpacked[2]).strip() if len(unpacked) > 2 else ""
                fallback_art = ""
            art, duration = get_song_details(song_id)
            results.append((title, song_id, extra, art or fallback_art, duration))
        except Exception as exc:
            print(f"[WARN] Failed to normalize Genie result: {exc}")
    return results


def parse_genie_extra_info(extra_info: str) -> Tuple[str, str]:
    artist, _, album = (extra_info or "").partition(" - ")
    return artist.strip(), album.strip()


def normalize_lyrics_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in str(text or "").replace("\r\n", "\n").splitlines()).strip()


def lyrics_are_synced(text: str) -> bool:
    return bool(text) and any(line.strip().startswith("[") and "]" in line for line in text.splitlines())


def get_genie_lyrics(song_id: str) -> Optional[str]:
    if not song_id:
        return None
    try:
        lyrics = GenieAPI().get_lyrics(song_id)
    except Exception as exc:
        print(f"[WARN] Genie lyric fetch failed for {song_id}: {exc}")
        return None
    if not lyrics:
        return None
    if isinstance(lyrics, str):
        for candidate in (lyrics, os.path.join(os.getcwd(), lyrics)):
            if os.path.isfile(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as file:
                        return normalize_lyrics_text(file.read())
                except Exception:
                    pass
        return normalize_lyrics_text(lyrics)
    return None


def _request_lrclib(endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
    try:
        response = requests.get(f"{LRCLIB_BASE_URL}/{endpoint}", params=params, headers=HEADERS, timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[WARN] LRCLIB request failed ({endpoint}): {exc}")
        return None


def _extract_lrclib(payload: Dict[str, Any], prefer_synced: bool) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    synced = normalize_lyrics_text(payload.get("syncedLyrics") or "")
    plain = normalize_lyrics_text(payload.get("plainLyrics") or "")
    if prefer_synced and synced:
        return synced
    return plain or synced or None


def get_lrclib_lyrics(*, title: str, artist: str, album: str = "", duration: Optional[int] = None, prefer_synced: bool = True) -> Optional[str]:
    if not title or not artist:
        return None
    params: Dict[str, Any] = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)
    exact = _request_lrclib("get", params)
    if exact:
        result = _extract_lrclib(exact, prefer_synced)
        if result:
            return result
    search = _request_lrclib("search", params)
    if isinstance(search, list):
        for item in search:
            result = _extract_lrclib(item, prefer_synced)
            if result:
                return result
    return None


def _request_musixmatch(endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("MUSIXMATCH_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        response = requests.get(f"{MUSIXMATCH_BASE_URL}/{endpoint}", params={**params, "apikey": api_key}, headers=HEADERS, timeout=10)
        response.raise_for_status()
        payload = response.json()
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        header = message.get("header", {}) if isinstance(message, dict) else {}
        if int(header.get("status_code", 0) or 0) != 200:
            return None
        body = message.get("body", {})
        return body if isinstance(body, dict) else None
    except Exception as exc:
        print(f"[WARN] Musixmatch request failed ({endpoint}): {exc}")
        return None


def get_musixmatch_lyrics(*, title: str, artist: str, duration: Optional[int] = None, prefer_synced: bool = True) -> Optional[str]:
    if not title or not artist or not os.getenv("MUSIXMATCH_API_KEY", "").strip():
        return None
    base: Dict[str, Any] = {"q_track": title, "q_artist": artist}
    if prefer_synced:
        params = {**base, "subtitle_format": "lrc"}
        if duration:
            params["f_subtitle_length"] = int(duration)
            params["f_subtitle_length_max_deviation"] = 4
        body = _request_musixmatch("matcher.subtitle.get", params)
        subtitle = body.get("subtitle", {}) if body else {}
        text = normalize_lyrics_text(subtitle.get("subtitle_body") or "") if isinstance(subtitle, dict) else ""
        if text:
            return text
    body = _request_musixmatch("matcher.lyrics.get", base)
    lyrics = body.get("lyrics", {}) if body else {}
    text = normalize_lyrics_text(lyrics.get("lyrics_body") or "") if isinstance(lyrics, dict) else ""
    if "*******" in text:
        text = text.split("*******", 1)[0].rstrip()
    return text or None


def get_best_lyrics(*, song_id: str = "", title: str = "", artist: str = "", album: str = "", duration: Optional[int] = None) -> Optional[str]:
    genie = get_genie_lyrics(song_id) if song_id else None
    if lyrics_are_synced(genie or ""):
        return genie
    lrclib_synced = get_lrclib_lyrics(title=title, artist=artist, album=album, duration=duration, prefer_synced=True)
    if lrclib_synced and lyrics_are_synced(lrclib_synced):
        return lrclib_synced
    mxm_synced = get_musixmatch_lyrics(title=title, artist=artist, duration=duration, prefer_synced=True)
    if mxm_synced and lyrics_are_synced(mxm_synced):
        return mxm_synced
    if genie:
        return genie
    if lrclib_synced:
        return lrclib_synced
    lrclib_plain = get_lrclib_lyrics(title=title, artist=artist, album=album, duration=duration, prefer_synced=False)
    if lrclib_plain:
        return lrclib_plain
    return get_musixmatch_lyrics(title=title, artist=artist, duration=duration, prefer_synced=False)


def get_song_details(song_id: str) -> Tuple[Optional[str], Optional[int]]:
    if not song_id:
        return None, None
    try:
        response = requests.get(f"https://www.genie.co.kr/detail/songInfo?xgnm={song_id}", headers=HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        print(f"[WARN] Failed to load Genie song page for {song_id}: {exc}")
        return None, None
    art_url: Optional[str] = None
    duration: Optional[int] = None
    image = soup.select_one("div.photo-zone span.cover > img")
    if image and image.get("src"):
        art_url = str(image["src"])
        if art_url.startswith("//"):
            art_url = f"https:{art_url}"
        art_url = art_url.replace("/dims/resize/Q_80,0", "")
    for item in soup.select("ul.info-data li"):
        title_span = item.select_one("span.title")
        value_span = item.select_one("span.value")
        if not title_span or not value_span or "재생시간" not in title_span.get_text(strip=True):
            continue
        try:
            minutes, seconds = value_span.get_text(strip=True).split(":")
            duration = int(minutes) * 60 + int(seconds)
        except ValueError:
            pass
        break
    return art_url, duration


def get_album_arts_url(song_id: str) -> List[str]:
    art, _ = get_song_details(song_id)
    return [art] if art else []


def get_song_album_id_and_art_url(song_id: str) -> Optional[Tuple[str, str]]:
    art, _ = get_song_details(song_id)
    if not art:
        return None
    return os.path.splitext(os.path.basename(art))[0], art
