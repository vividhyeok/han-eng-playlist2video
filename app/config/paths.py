"""Centralized filesystem paths for source and packaged runtimes."""
from __future__ import annotations

import os
import shutil
import sys
from typing import Iterable


def _source_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = os.path.abspath(getattr(sys, "_MEIPASS", _source_root()))
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else _source_root()

DATA_DIR = os.path.join(BASE_DIR, "data")
TEMP_DIR = os.path.join(DATA_DIR, "temp")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
LYRICS_DIR = os.path.join(DATA_DIR, "lyrics")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
AUDIO_CACHE_DIR = os.path.join(CACHE_DIR, "audio")
ART_CACHE_DIR = os.path.join(CACHE_DIR, "art")
RENDER_CACHE_DIR = os.path.join(CACHE_DIR, "render_assets")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
REVIEW_DIR = os.path.join(DATA_DIR, "review")

TRANSLATION_CACHE_PATH = os.path.join(CACHE_DIR, "translation_cache.json")
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, "config.json")

_RESOURCE_FFMPEG_BIN = os.path.join(RESOURCE_DIR, "bin", "ffmpeg", "bin")
_LOCAL_FFMPEG_BIN = os.path.join(BASE_DIR, "bin", "ffmpeg", "bin")


def _resolve_tool(name: str) -> str:
    exe_name = f"{name}.exe" if os.name == "nt" else name
    for directory in (_RESOURCE_FFMPEG_BIN, _LOCAL_FFMPEG_BIN):
        candidate = os.path.join(directory, exe_name)
        if os.path.exists(candidate):
            return candidate
    discovered = shutil.which(name)
    if discovered:
        return discovered
    return os.path.join(_RESOURCE_FFMPEG_BIN, exe_name)


FFMPEG_PATH = _resolve_tool("ffmpeg")
FFPROBE_PATH = _resolve_tool("ffprobe")
FFMPEG_DIR = os.path.dirname(FFMPEG_PATH)

LEGACY_TEMP_DIR = os.path.join(BASE_DIR, "temp")
LEGACY_LYRICS_DIR = os.path.join(BASE_DIR, "result")


def _ensure_directories(paths: Iterable[str]) -> None:
    for path in paths:
        os.makedirs(path, exist_ok=True)


def ensure_data_dirs() -> None:
    _ensure_directories(
        (
            DATA_DIR, TEMP_DIR, OUTPUT_DIR, LYRICS_DIR, CACHE_DIR,
            AUDIO_CACHE_DIR, ART_CACHE_DIR, RENDER_CACHE_DIR, CONFIG_DIR, REVIEW_DIR,
        )
    )


ensure_data_dirs()
