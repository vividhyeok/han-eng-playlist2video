"""Premiere-compatible XML export."""

from __future__ import annotations

import json
import os
import subprocess
from typing import List
from xml.etree.ElementTree import Element, ElementTree, SubElement

from app.config.paths import FFPROBE_PATH


def _get_audio_duration(audio_path: str) -> float:
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
        return float(result.stdout.strip())
    except Exception as exc:
        print(f"[WARN] Failed to inspect audio duration for XML export: {exc}")
        return 0.0


def _pathurl(path: str) -> str:
    return "file://" + os.path.abspath(path).replace("\\", "/")


def _add_rate(parent: Element, fps: int) -> None:
    rate = SubElement(parent, "rate")
    SubElement(rate, "timebase").text = str(fps)
    SubElement(rate, "ntsc").text = "FALSE"


def _append_markers(clipitem: Element, lyrics: List[dict], fps: int, total_duration: float) -> None:
    for index, lyric in enumerate(lyrics):
        start = float(lyric.get("start_time", 0.0))
        end = (
            float(lyrics[index + 1].get("start_time", total_duration))
            if index < len(lyrics) - 1
            else total_duration
        )
        start_frames = int(round(start * fps))
        end_frames = max(start_frames + 1, int(round(end * fps)))

        marker = SubElement(clipitem, "marker")
        SubElement(marker, "name").text = (lyric.get("original") or "").strip()
        SubElement(marker, "comment").text = "\n".join(
            filter(None, [lyric.get("original", ""), lyric.get("english", "")])
        ).strip()
        SubElement(marker, "in").text = str(start_frames)
        SubElement(marker, "out").text = str(end_frames)


def export_premiere_xml(
    audio_path: str,
    album_art_path: str,
    lyrics_json_path: str,
    output_xml_path: str,
    fps: int = 30,
) -> str:
    if not os.path.exists(lyrics_json_path):
        raise FileNotFoundError(f"Lyrics JSON not found: {lyrics_json_path}")

    with open(lyrics_json_path, "r", encoding="utf-8") as json_file:
        lyrics: List[dict] = json.load(json_file)
    lyrics.sort(key=lambda item: float(item.get("start_time", 0.0)))

    total_duration = _get_audio_duration(audio_path)
    total_frames = int(round(total_duration * fps))
    sequence_name = os.path.splitext(os.path.basename(audio_path))[0]

    root = Element("xmeml", version="5")
    sequence = SubElement(root, "sequence", id="sequence-1")
    SubElement(sequence, "name").text = sequence_name
    _add_rate(sequence, fps)
    SubElement(sequence, "duration").text = str(total_frames)

    media = SubElement(sequence, "media")
    video = SubElement(media, "video")
    track = SubElement(video, "track")

    clipitem = SubElement(track, "clipitem", id="clipitem-1")
    SubElement(clipitem, "name").text = sequence_name
    _add_rate(clipitem, fps)
    SubElement(clipitem, "start").text = "0"
    SubElement(clipitem, "end").text = str(total_frames)
    SubElement(clipitem, "in").text = "0"
    SubElement(clipitem, "out").text = str(total_frames)

    file_element = SubElement(clipitem, "file", id="file-1")
    SubElement(file_element, "name").text = os.path.basename(audio_path)
    SubElement(file_element, "pathurl").text = _pathurl(audio_path)
    _add_rate(file_element, fps)
    SubElement(file_element, "duration").text = str(total_frames)

    logging_info = SubElement(file_element, "logginginfo")
    SubElement(logging_info, "description").text = f"Album art: {_pathurl(album_art_path)}"

    _append_markers(clipitem, lyrics, fps, total_duration)

    os.makedirs(os.path.dirname(output_xml_path) or ".", exist_ok=True)
    ElementTree(root).write(output_xml_path, encoding="utf-8", xml_declaration=True)
    return output_xml_path
