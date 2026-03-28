"""spotDL-based audio download helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
from glob import glob
from shutil import which
from typing import Optional

from app.config.paths import TEMP_DIR, ensure_data_dirs
from app.sources.youtube_handler import validate_audio_file


def _ensure_spotdl_exists() -> None:
    if which("spotdl") is None:
        raise FileNotFoundError(
            "spotdl CLI was not found. Install it with 'pip install spotdl'."
        )


def download_audio_with_spotdl(
    artist: str,
    title: str,
    output_dir: str = TEMP_DIR,
) -> Optional[str]:
    """Download audio with spotDL and return the final audio path."""

    try:
        _ensure_spotdl_exists()
        ensure_data_dirs()
        os.makedirs(output_dir, exist_ok=True)

        query = f"{artist} - {title}".strip(" -")
        expected_stem = os.path.join(output_dir, f"{artist} - {title}")
        output_template = f"{expected_stem}.{{output-ext}}"
        existing_files = set(glob(os.path.join(output_dir, "*")))

        command = [
            "spotdl",
            "download",
            query,
            "--output",
            output_template,
            "--format",
            "mp3",
            "--bitrate",
            "320k",
        ]
        print(f"[INFO] Running spotDL: {' '.join(shlex.quote(part) for part in command)}")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            print(f"[WARN] spotDL failed: {stderr}")
            return None

        preferred_path = f"{expected_stem}.mp3"
        if validate_audio_file(preferred_path):
            return preferred_path

        updated_candidates = sorted(
            set(glob(os.path.join(output_dir, "*"))) - existing_files,
            key=lambda path: os.path.getmtime(path),
            reverse=True,
        )
        for candidate in updated_candidates:
            if not candidate.lower().endswith(".mp3"):
                continue
            if validate_audio_file(candidate):
                return candidate

        fallback_candidates = sorted(
            glob(os.path.join(output_dir, "*.mp3")),
            key=lambda path: os.path.getmtime(path),
            reverse=True,
        )
        for candidate in fallback_candidates:
            if validate_audio_file(candidate):
                return candidate
    except Exception as exc:
        print(f"[WARN] spotDL download failed: {exc}")

    return None


def download_audio_simple(
    artist: str,
    title: str,
    output_dir: str = TEMP_DIR,
) -> Optional[str]:
    return download_audio_with_spotdl(artist, title, output_dir)
