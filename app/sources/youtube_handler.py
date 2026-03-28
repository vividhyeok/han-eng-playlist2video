"""YouTube search and audio download helpers."""

from __future__ import annotations

import os
import subprocess
from glob import glob
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import yt_dlp

try:
    from youtubesearchpython import VideosSearch
except ImportError:  # pragma: no cover
    VideosSearch = None

from app.config.paths import FFMPEG_DIR, FFPROBE_PATH, TEMP_DIR, ensure_data_dirs


def sanitize_youtube_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    video_id = _extract_video_id(parsed, host)
    if not video_id:
        return url.strip()

    normalized_query = {"v": video_id}
    query = parse_qs(parsed.query)
    if "t" in query and query["t"]:
        normalized_query["t"] = query["t"][0]

    return f"https://www.youtube.com/watch?{urlencode(normalized_query)}"


def _extract_video_id(parsed, host: str) -> str:
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0]

    if "music.youtube.com" in host or "youtube.com" in host:
        query = parse_qs(parsed.query)
        if "v" in query and query["v"]:
            return query["v"][0]

        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            return path_parts[1]

    return ""


def parse_duration(duration_str: str) -> Optional[int]:
    if not duration_str:
        return None
    parts = duration_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1:
            return int(parts[0])
    except ValueError:
        return None
    return None


def youtube_search(query: str, target_duration: Optional[int] = None) -> List[Dict[str, Any]]:
    """Search YouTube, preferring videos closest to the target duration."""

    providers = [_search_with_videos_search, _search_with_yt_dlp]
    for provider in providers:
        try:
            results = provider(query, target_duration)
            if results:
                return results
        except Exception as exc:
            print(f"[WARN] YouTube search provider failed: {exc}")

    return []


def _search_with_videos_search(
    query: str,
    target_duration: Optional[int],
) -> List[Dict[str, Any]]:
    if VideosSearch is None:
        return []

    search = VideosSearch(f"{query} audio", limit=5)
    raw_results = search.result().get("result", [])
    formatted_results: List[Dict[str, Any]] = []
    for result in raw_results:
        formatted_results.append(
            {
                "title": result.get("title", ""),
                "link": sanitize_youtube_url(result.get("link", "")),
                "thumbnail": result.get("thumbnails", [{}])[0].get("url", ""),
                "duration": result.get("duration", "N/A"),
                "duration_sec": parse_duration(result.get("duration", "")),
            }
        )
    return _pick_by_duration(formatted_results, target_duration)


def _search_with_yt_dlp(
    query: str,
    target_duration: Optional[int],
) -> List[Dict[str, Any]]:
    options = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch8:{query} audio", download=False)

    entries = info.get("entries", []) if isinstance(info, dict) else []
    formatted_results: List[Dict[str, Any]] = []
    for entry in entries:
        video_id = entry.get("id", "")
        duration = entry.get("duration")
        thumbnails = entry.get("thumbnails") or [{}]
        formatted_results.append(
            {
                "title": entry.get("title", ""),
                "link": sanitize_youtube_url(
                    f"https://www.youtube.com/watch?v={video_id}"
                ),
                "thumbnail": thumbnails[0].get("url", ""),
                "duration": entry.get("duration_string") or str(duration or "N/A"),
                "duration_sec": duration,
            }
        )
    return _pick_by_duration(formatted_results, target_duration)


def _pick_by_duration(
    results: List[Dict[str, Any]],
    target_duration: Optional[int],
) -> List[Dict[str, Any]]:
    if not results:
        return []

    if target_duration is None:
        return results[:5]

    def sort_key(result: Dict[str, Any]) -> tuple[int, int]:
        duration = result.get("duration_sec")
        if duration is None:
            return (1, 999999)
        return (0, abs(int(duration) - int(target_duration)))

    return sorted(results, key=sort_key)[:5]


def download_youtube_audio(url: str, output_name: str) -> Optional[str]:
    """Download a YouTube video's audio as MP3 and validate the result."""

    normalized_url = sanitize_youtube_url(url)
    if not normalized_url:
        return None

    ensure_data_dirs()
    os.makedirs(TEMP_DIR, exist_ok=True)

    target_path = _resolve_output_path(output_name)
    stem, _ = os.path.splitext(target_path)

    for attempt in range(3):
        _cleanup_partial_downloads(stem)
        try:
            options = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "outtmpl": f"{stem}.%(ext)s",
                "ffmpeg_location": FFMPEG_DIR,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "overwrites": True,
                "retries": 5,
                "fragment_retries": 5,
                "extractor_retries": 3,
                "socket_timeout": 30,
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0 Safari/537.36"
                    )
                },
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([normalized_url])

            final_path = _find_audio_file(stem)
            if final_path and final_path != target_path:
                if os.path.exists(target_path):
                    os.remove(target_path)
                os.replace(final_path, target_path)
                final_path = target_path

            if final_path and validate_audio_file(final_path):
                print(f"[INFO] Downloaded YouTube audio to {final_path}")
                return final_path
        except Exception as exc:
            print(f"[WARN] YouTube download attempt {attempt + 1} failed: {exc}")

    return None


def validate_audio_file(audio_path: str) -> bool:
    if not audio_path or not os.path.exists(audio_path):
        return False
    if os.path.getsize(audio_path) < 128 * 1024:
        return False

    try:
        result = subprocess.run(
            [
                FFPROBE_PATH,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        duration = float(result.stdout.strip())
        return duration >= 5.0
    except Exception as exc:
        print(f"[WARN] Failed to validate audio file {audio_path}: {exc}")
        return False


def _resolve_output_path(output_name: str) -> str:
    if not output_name:
        return os.path.join(TEMP_DIR, "downloaded_audio.mp3")

    if os.path.isabs(output_name):
        base_path = output_name
    else:
        base_path = os.path.join(TEMP_DIR, output_name)

    if base_path.lower().endswith(".mp3"):
        return base_path
    return f"{base_path}.mp3"


def _find_audio_file(stem: str) -> Optional[str]:
    preferred = f"{stem}.mp3"
    if os.path.exists(preferred):
        return preferred

    candidates = sorted(
        glob(f"{stem}.*"),
        key=lambda path: os.path.getmtime(path),
        reverse=True,
    )
    for candidate in candidates:
        if candidate.lower().endswith((".mp3", ".m4a", ".webm", ".opus")):
            return candidate
    return None


def _cleanup_partial_downloads(stem: str) -> None:
    for candidate in glob(f"{stem}*"):
        if os.path.isdir(candidate):
            continue
        try:
            os.remove(candidate)
        except OSError:
            pass
