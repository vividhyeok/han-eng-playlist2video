"""Video rendering helpers."""

from __future__ import annotations

import json
import os
import subprocess
import traceback
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config.paths import FFMPEG_PATH, FFPROBE_PATH, TEMP_DIR, ensure_data_dirs


def get_audio_duration(audio_path: str) -> float:
    """Return audio duration in seconds using ffprobe."""

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
        print(f"[ERROR] Failed to inspect audio duration: {exc}")
        return 0.0


def draw_outlined_text(
    draw: ImageDraw.ImageDraw,
    pos: Tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    text_color=(255, 255, 255),
    outline_color=(0, 0, 0),
    outline_width: int = 3,
) -> None:
    """Draw text with a simple outline for readability."""

    x, y = int(round(pos[0])), int(round(pos[1]))
    for offset_x in range(-outline_width, outline_width + 1):
        for offset_y in range(-outline_width, outline_width + 1):
            draw.text((x + offset_x, y + offset_y), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=text_color)


def resolve_font_path() -> Optional[str]:
    env_font = os.getenv("LYRIC_FONT_PATH")
    if env_font and os.path.exists(env_font):
        return env_font

    candidates: Sequence[str] = (
        os.path.join(os.getcwd(), "assets", "fonts", "NotoSansCJK-Regular.otf"),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "C:/Windows/Fonts/malgunbd.ttf",
    )
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _load_font(font_path: Optional[str], size: int) -> ImageFont.ImageFont:
    for candidate in (
        font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def prepare_fonts() -> Tuple[ImageFont.ImageFont, ImageFont.ImageFont]:
    font_path = resolve_font_path()
    return _load_font(font_path, 60), _load_font(font_path, 55)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    if hasattr(draw, "textlength"):
        return draw.textlength(text, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _split_long_token(
    token: str,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    max_width: float,
) -> List[str]:
    if not token:
        return [""]
    parts: List[str] = []
    buffer = ""
    for character in token:
        tentative = buffer + character
        if _text_width(draw, tentative, font) <= max_width or not buffer:
            buffer = tentative
        else:
            parts.append(buffer)
            buffer = character
    if buffer:
        parts.append(buffer)
    return parts


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: float,
) -> List[str]:
    if not text:
        return [""]

    lines: List[str] = []
    current = ""
    for word in text.split():
        tentative = word if not current else f"{current} {word}"
        if _text_width(draw, tentative, font) <= max_width:
            current = tentative
            continue
        if current:
            lines.append(current)
        if _text_width(draw, word, font) <= max_width:
            current = word
        else:
            split_word = _split_long_token(word, draw, font, max_width)
            lines.extend(split_word[:-1])
            current = split_word[-1]
    if current:
        lines.append(current)
    return lines or [text]


def _draw_multiline_centered(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    font: ImageFont.ImageFont,
    frame_width: int,
    center_y: float,
    spacing_ratio: float = 0.3,
) -> None:
    lines = [line for line in lines if line is not None]
    if not lines:
        return
    heights = [_text_height(draw, line, font) for line in lines]
    spacing = max(int(heights[0] * spacing_ratio), 10)
    total_height = sum(heights) + spacing * (len(lines) - 1)
    y_cursor = center_y - total_height / 2
    for line, height in zip(lines, heights):
        width = _text_width(draw, line, font)
        draw_outlined_text(draw, ((frame_width - width) / 2, y_cursor), line, font)
        y_cursor += height + spacing


def _center_crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def prepare_base_frame(album_art_img: Image.Image) -> Image.Image:
    cover = _center_crop_square(album_art_img.convert("RGB"))
    frame = cover.resize((1920, 1080), Image.Resampling.LANCZOS).convert("RGBA")
    blurred = frame.filter(ImageFilter.GaussianBlur(radius=30))
    base = Image.alpha_composite(blurred, Image.new("RGBA", frame.size, (0, 0, 0, 160)))
    art_size = (500, 500)
    art_img = cover.resize(art_size, Image.Resampling.LANCZOS).convert("RGBA")
    art_x = (frame.width - art_size[0]) // 2
    art_y = 180
    base.paste(art_img, (art_x, art_y), art_img)
    return base


def create_lyric_frame(
    base_frame: Image.Image,
    lyric: Dict[str, str],
    fonts: Tuple[ImageFont.ImageFont, ImageFont.ImageFont],
    max_width_ratio: float = 0.86,
) -> Image.Image:
    frame = base_frame.copy()
    draw = ImageDraw.Draw(frame)
    original_font, english_font = fonts
    max_text_width = frame.width * max_width_ratio

    original_lines = _wrap_text(draw, lyric.get("original", ""), original_font, max_text_width)
    english_lines = _wrap_text(draw, lyric.get("english", ""), english_font, max_text_width)
    _draw_multiline_centered(draw, original_lines, original_font, frame.width, 765)
    _draw_multiline_centered(draw, english_lines, english_font, frame.width, 870)
    return frame.convert("RGB")


def parse_srt_file(srt_path: str) -> List[dict]:
    with open(srt_path, "r", encoding="utf-8") as srt_file:
        content = srt_file.read()
    segments = content.strip().split("\n\n")
    parsed: List[dict] = []
    for segment in segments:
        lines = segment.split("\n")
        if len(lines) < 3:
            continue
        start_time, end_time = lines[1].split(" --> ")
        parsed.append(
            {
                "start": start_time.replace(",", "."),
                "end": end_time.replace(",", "."),
                "text": "\n".join(lines[2:]),
            }
        )
    return parsed


def make_lyric_video(audio_path: str, album_art_path: str, lyrics_json_path: str, output_path: str) -> None:
    """Render the lyric video by generating still frames and muxing with FFmpeg."""

    try:
        print("[INFO] Starting lyric video render")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        ensure_data_dirs()
        os.makedirs(TEMP_DIR, exist_ok=True)
        frames_dir = os.path.join(TEMP_DIR, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        for entry in os.listdir(frames_dir):
            os.remove(os.path.join(frames_dir, entry))

        duration = get_audio_duration(audio_path)
        if duration <= 0:
            raise ValueError("Failed to determine audio duration.")

        with Image.open(album_art_path) as album_image:
            base_frame = prepare_base_frame(album_image)
        fonts = prepare_fonts()

        with open(lyrics_json_path, "r", encoding="utf-8") as json_file:
            lyrics_data = json.load(json_file)
        if not lyrics_data:
            raise ValueError("Lyrics JSON is empty.")
        lyrics_data.sort(key=lambda item: float(item.get("start_time", 0.0)))

        concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
        concat_entries: List[str] = []
        current_time = 0.0
        base_frame_path = os.path.join(frames_dir, "base_frame.png")
        base_frame.save(base_frame_path)

        for index, lyric in enumerate(lyrics_data):
            start_time = float(lyric.get("start_time", 0.0))
            if start_time > current_time:
                concat_entries.append(f"file '{base_frame_path.replace(os.sep, '/')}'")
                concat_entries.append(f"duration {start_time - current_time:.3f}")
                current_time = start_time

            next_start = (
                float(lyrics_data[index + 1].get("start_time", duration))
                if index < len(lyrics_data) - 1
                else duration
            )
            next_start = max(next_start, start_time + 0.1)

            frame_path = os.path.join(frames_dir, f"frame_{index:04d}.png")
            create_lyric_frame(base_frame, lyric, fonts).save(frame_path)
            concat_entries.append(f"file '{frame_path.replace(os.sep, '/')}'")
            concat_entries.append(f"duration {next_start - start_time:.3f}")
            current_time = next_start

        if current_time < duration:
            concat_entries.append(f"file '{base_frame_path.replace(os.sep, '/')}'")
            concat_entries.append(f"duration {duration - current_time:.3f}")
        concat_entries.append(f"file '{base_frame_path.replace(os.sep, '/')}'")

        with open(concat_list_path, "w", encoding="utf-8") as concat_file:
            concat_file.write("\n".join(concat_entries))

        command = [
            FFMPEG_PATH,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_path,
            "-i",
            audio_path,
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-level",
            "4.0",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-shortest",
            "-preset",
            "fast",
            "-crf",
            "18",
            output_path,
        ]
        print(f"[INFO] Running FFmpeg: {' '.join(command)}")
        subprocess.run(command, check=True)
        print(f"[INFO] Lyric video saved to {output_path}")
    except Exception as exc:
        print(f"[ERROR] Video render failed: {exc}")
        traceback.print_exc()
        raise


def convert_timestamp_to_seconds(timestamp: str) -> float:
    hours, minutes, seconds = timestamp.replace(",", ".").split(":")
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


def convert_milliseconds_to_seconds(milliseconds: float) -> float:
    return milliseconds / 1000.0


def parse_lyrics_json(json_path: str) -> List[dict]:
    with open(json_path, "r", encoding="utf-8") as json_file:
        return json.load(json_file)
