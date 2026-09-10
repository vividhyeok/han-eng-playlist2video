"""Album art lookup and download helpers with persistent caching."""
from __future__ import annotations

import hashlib
import os
import shutil
import traceback
from typing import Optional
from urllib.parse import quote

import musicbrainzngs
import requests
from bs4 import BeautifulSoup
from PIL import Image

from app.config.paths import ART_CACHE_DIR, ensure_data_dirs

musicbrainzngs.set_useragent(
    "han-eng-lyricvideo-maker",
    "2.0",
    "https://github.com/"
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}


def _cache_key(source: str, artist: str, title: str) -> str:
    raw = f"{source.strip()}\n{artist.strip().casefold()}\n{title.strip().casefold()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(source: str, artist: str, title: str) -> str:
    ensure_data_dirs()
    return os.path.join(ART_CACHE_DIR, f"{_cache_key(source, artist, title)}.jpg")


def search_album_art(artist: str, title: str) -> Optional[str]:
    if not artist or not title:
        return None
    try:
        result = musicbrainzngs.search_recordings(artist=artist, recording=title, limit=1)
        recordings = result.get("recording-list", [])
        if not recordings:
            return None
        releases = recordings[0].get("release-list", [])
        if not releases:
            return None
        image_info = musicbrainzngs.get_image_list(releases[0]["id"])
        for image in image_info.get("images", []):
            if "front" not in image.get("types", []):
                continue
            thumbnails = image.get("thumbnails", {})
            return thumbnails.get("large") or thumbnails.get("small") or image.get("image")
    except Exception as exc:
        print(f"[WARN] MusicBrainz album art lookup failed: {exc}")
    return None


def search_album_art_bugs(artist: str, title: str) -> Optional[str]:
    if not artist or not title:
        return None
    try:
        query = quote(f"{artist} {title}")
        response = requests.get(
            f"https://music.bugs.co.kr/search/track?q={query}",
            headers=REQUEST_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        album_art = soup.select_one("table.trackList > tbody > tr:first-child figure.thumbnail img")
        if not album_art or not album_art.get("src"):
            return None
        image_url = album_art["src"].replace("50x50", "1000x1000")
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        return image_url
    except Exception as exc:
        print(f"[WARN] Bugs album art lookup failed: {exc}")
        traceback.print_exc()
        return None


def download_album_art(
    source: str,
    filepath: str,
    *,
    artist: str = "",
    title: str = "",
) -> bool:
    """Resolve art from URL/local metadata and persist the resolved image for reuse."""
    ensure_data_dirs()
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    cache = _cache_path(source or "", artist, title)
    if _validate_image(cache):
        shutil.copyfile(cache, filepath)
        return _validate_image(filepath)

    candidates = []
    if source:
        candidates.append(source)
    fallback_art = search_album_art(artist, title) or search_album_art_bugs(artist, title)
    if fallback_art and fallback_art not in candidates:
        candidates.append(fallback_art)

    for candidate in candidates:
        if not candidate:
            continue
        success = False
        if os.path.isfile(candidate):
            try:
                shutil.copyfile(candidate, filepath)
                success = _validate_image(filepath)
            except Exception as exc:
                print(f"[WARN] Failed to copy local album art {candidate}: {exc}")
        else:
            success = _download_url_to_file(candidate, filepath)
        if success:
            try:
                shutil.copyfile(filepath, cache)
            except OSError:
                pass
            return True
    return False


def _download_url_to_file(url: str, filepath: str) -> bool:
    for attempt in range(3):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
            response.raise_for_status()
            with open(filepath, "wb") as image_file:
                image_file.write(response.content)
            if _validate_image(filepath):
                return True
        except Exception as exc:
            print(f"[WARN] Album art download attempt {attempt + 1} failed: {exc}")
    return False


def _validate_image(filepath: str) -> bool:
    try:
        if not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) < 1024:
            return False
        with Image.open(filepath) as image:
            image.verify()
        return True
    except Exception:
        return False
