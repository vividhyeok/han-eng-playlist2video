"""Album art lookup and download helpers."""

from __future__ import annotations

import os
import shutil
import traceback
from typing import Optional
from urllib.parse import quote

import musicbrainzngs
import requests
from bs4 import BeautifulSoup
from PIL import Image

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


def search_album_art(artist: str, title: str) -> Optional[str]:
    """Search album art through MusicBrainz and Cover Art Archive."""

    if not artist or not title:
        return None

    try:
        result = musicbrainzngs.search_recordings(
            artist=artist,
            recording=title,
            limit=1,
        )
        recordings = result.get("recording-list", [])
        if not recordings:
            return None

        recording = recordings[0]
        releases = recording.get("release-list", [])
        if not releases:
            return None

        release_id = releases[0]["id"]
        image_info = musicbrainzngs.get_image_list(release_id)
        images = image_info.get("images", [])
        for image in images:
            if "front" not in image.get("types", []):
                continue
            thumbnails = image.get("thumbnails", {})
            return thumbnails.get("large") or thumbnails.get("small") or image.get("image")
    except Exception as exc:
        print(f"[WARN] MusicBrainz album art lookup failed: {exc}")

    return None


def search_album_art_bugs(artist: str, title: str) -> Optional[str]:
    """Search album art from Bugs as a Korean music fallback."""

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
        album_art = soup.select_one(
            "table.trackList > tbody > tr:first-child figure.thumbnail img"
        )
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
    """Resolve album art from a URL, local path, or metadata fallback."""

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    candidates = []
    if source:
        candidates.append(source)
    fallback_art = search_album_art(artist, title) or search_album_art_bugs(artist, title)
    if fallback_art and fallback_art not in candidates:
        candidates.append(fallback_art)

    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isfile(candidate):
            try:
                shutil.copyfile(candidate, filepath)
                if _prepare_album_art_image(filepath):
                    return True
                _remove_invalid_image(filepath)
            except Exception as exc:
                print(f"[WARN] Failed to copy local album art {candidate}: {exc}")
                continue

        if _download_url_to_file(candidate, filepath):
            return True

    return False


def _download_url_to_file(
    url: str,
    filepath: str,
) -> bool:
    for attempt in range(3):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
            response.raise_for_status()
            with open(filepath, "wb") as image_file:
                image_file.write(response.content)
            if _prepare_album_art_image(filepath):
                return True
            print(f"[WARN] Album art candidate could not be normalized: {url}")
            _remove_invalid_image(filepath)
        except Exception as exc:
            print(f"[WARN] Album art download attempt {attempt + 1} failed: {exc}")
            _remove_invalid_image(filepath)

    return False


def _validate_image(
    filepath: str,
) -> bool:
    try:
        if os.path.getsize(filepath) < 1024:
            return False
        with Image.open(filepath) as image:
            image.verify()
        return True
    except Exception:
        return False


def _prepare_album_art_image(filepath: str) -> bool:
    if not _validate_image(filepath):
        return False

    try:
        with Image.open(filepath) as image:
            normalized = _center_crop_square(image.convert("RGB"))
            normalized.save(filepath, format="JPEG", quality=95)
        return _validate_image(filepath)
    except Exception:
        return False


def _center_crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _remove_invalid_image(filepath: str) -> None:
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except OSError:
        pass
