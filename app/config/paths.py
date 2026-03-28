"""Centralized filesystem paths for runtime data."""

from __future__ import annotations

import os
from typing import Iterable

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

DATA_DIR = os.path.join(BASE_DIR, "data")
TEMP_DIR = os.path.join(DATA_DIR, "temp")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
LYRICS_DIR = os.path.join(DATA_DIR, "lyrics")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
CONFIG_DIR = os.path.join(DATA_DIR, "config")

TRANSLATION_CACHE_PATH = os.path.join(CACHE_DIR, "translation_cache.json")
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, "config.json")

# FFMPEG paths
import shutil
import sys
import sysconfig

# Augment PATH to ensure we find tools installed via Chocolatey or pip --user
extra_paths = [
    # 0. Local Project Bin (Highest Priority)
    os.path.join(BASE_DIR, "bin", "ffmpeg", "bin"),
    # Chocolatey bin
    r"C:\ProgramData\chocolatey\bin",
    # Specific ffmpeg install location from chocolatey (often more reliable than shim for some libs)
    r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg-8.0.1-essentials_build\bin",
    # User Scripts (explicitly found for Python 3.14)
    r"C:\Users\user\AppData\Roaming\Python\Python314\Scripts",
]

# Try to find generic user scripts if the specific one above fails
try:
    user_scripts = sysconfig.get_path("scripts", f"{os.name}_user")
    if user_scripts:
        extra_paths.append(user_scripts)
except Exception:
    pass

for p in extra_paths:
    if os.path.isdir(p) and p.lower() not in os.environ["PATH"].lower():
        # Prepend to ensure priority
        os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]

# FFMPEG paths
CHOCOLATEY_FFMPEG_DIR = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg-8.0.1-essentials_build\bin"
LOCAL_FFMPEG_BIN = os.path.join(BASE_DIR, "bin", "ffmpeg", "bin")

# 1. Try to find in Local Project Bin
if os.path.exists(os.path.join(LOCAL_FFMPEG_BIN, "ffmpeg.exe")):
    FFMPEG_DIR = LOCAL_FFMPEG_BIN
    FFMPEG_PATH = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    FFPROBE_PATH = os.path.join(FFMPEG_DIR, "ffprobe.exe")
# 2. Try to find in system PATH
elif shutil.which("ffmpeg"):
    FFMPEG_PATH = shutil.which("ffmpeg")
    FFMPEG_DIR = os.path.dirname(FFMPEG_PATH)
    FFPROBE_PATH = shutil.which("ffprobe") or os.path.join(FFMPEG_DIR, "ffprobe.exe")
elif os.path.exists(os.path.join(CHOCOLATEY_FFMPEG_DIR, "ffmpeg.exe")):
    # 3. Fallback to known Chocolatey path
    FFMPEG_DIR = CHOCOLATEY_FFMPEG_DIR
    FFMPEG_PATH = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    FFPROBE_PATH = os.path.join(FFMPEG_DIR, "ffprobe.exe")
else:
    # 4. Fallback to local build (legacy)
    FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg-8.0.1-essentials_build", "bin")
    FFMPEG_PATH = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    FFPROBE_PATH = os.path.join(FFMPEG_DIR, "ffprobe.exe")

# Legacy locations retained for compatibility (do not remove without migration plan).
LEGACY_TEMP_DIR = os.path.join(BASE_DIR, "temp")
LEGACY_LYRICS_DIR = os.path.join(BASE_DIR, "result")


def _ensure_directories(paths: Iterable[str]) -> None:
    for path in paths:
        os.makedirs(path, exist_ok=True)


def ensure_data_dirs() -> None:
    """Create the runtime data directories if they are missing."""
    _ensure_directories((DATA_DIR, TEMP_DIR, OUTPUT_DIR, LYRICS_DIR, CACHE_DIR, CONFIG_DIR))


ensure_data_dirs()
