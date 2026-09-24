"""Fast lyric-video rendering while preserving the existing visual identity.

Primary path: one pre-rendered blurred album-art background + ASS subtitles rendered
inside FFmpeg. Missing lyrics are a valid state and render as an album-art video with
audio only. Hardware H.264 encoders are tried opportunistically before libx264.
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from typing import List, Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config.paths import FFMPEG_PATH, FFPROBE_PATH, TEMP_DIR, ensure_data_dirs

FRAME_SIZE = (1920, 1080)
ART_SIZE = 500
ART_TOP = 170
_ENCODERS: Optional[set[str]] = None


def get_audio_duration(audio_path: str) -> float:
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except Exception as exc:
        print(f"[ERROR] Failed to inspect audio duration: {exc}")
        return 0.0


def _available_encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is not None:
        return _ENCODERS
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False,
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        _ENCODERS = {
            name for name in ("h264_nvenc", "h264_qsv", "h264_amf")
            if name in text
        }
    except Exception:
        _ENCODERS = set()
    return _ENCODERS


def _video_encoder_candidates() -> List[List[str]]:
    available = _available_encoders()
    candidates: List[List[str]] = []
    # Generic bitrate settings are intentionally used for portability across driver versions.
    if "h264_nvenc" in available:
        candidates.append(["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "5M", "-maxrate", "7M", "-bufsize", "10M"])
    if "h264_qsv" in available:
        candidates.append(["-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", "5M", "-maxrate", "7M", "-bufsize", "10M"])
    if "h264_amf" in available:
        candidates.append(["-c:v", "h264_amf", "-quality", "speed", "-b:v", "5M", "-maxrate", "7M", "-bufsize", "10M"])
    candidates.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-tune", "stillimage"])
    return candidates


def _resolve_font_path() -> Optional[str]:
    env_font = os.getenv("LYRIC_FONT_PATH")
    candidates: Sequence[str] = (
        env_font or "",
        "C:/Windows/Fonts/malgun.ttf",
        "C:/Windows/Fonts/malgunbd.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    return next((path for path in candidates if path and os.path.exists(path)), None)


def _load_font(size: int) -> ImageFont.ImageFont:
    path = _resolve_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _crop_to_aspect(image: Image.Image, target_ratio: float) -> Image.Image:
    """Center-crop without stretching, preserving the source artwork geometry."""
    width, height = image.size
    source_ratio = width / max(1, height)
    if source_ratio > target_ratio:
        crop_width = max(1, round(height * target_ratio))
        left = (width - crop_width) // 2
        return image.crop((left, 0, left + crop_width, height))
    crop_height = max(1, round(width / target_ratio))
    top = (height - crop_height) // 2
    return image.crop((0, top, width, top + crop_height))


def prepare_base_frame(background_img: Image.Image) -> Image.Image:
    source = background_img.convert("RGBA")
    background = _crop_to_aspect(source, FRAME_SIZE[0] / FRAME_SIZE[1])
    frame = background.resize(FRAME_SIZE, Image.Resampling.LANCZOS)
    blurred = frame.filter(ImageFilter.GaussianBlur(radius=32))
    base = Image.alpha_composite(blurred, Image.new("RGBA", frame.size, (0, 0, 0, 145)))

    # Playlist sources sometimes provide a 16:9 video thumbnail with the square cover
    # centered inside it. Crop the center square first; never squash the full thumbnail.
    square_art = _crop_to_aspect(source, 1.0)
    art_img = square_art.resize((ART_SIZE, ART_SIZE), Image.Resampling.LANCZOS)
    base.paste(art_img, ((FRAME_SIZE[0] - ART_SIZE) // 2, ART_TOP), art_img)
    return base


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}:{minutes:02d}:{seconds % 60:05.2f}"


def _ass_escape(text: str) -> str:
    return (
        str(text or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        .replace("\r", " ").replace("\n", r"\N")
    )


def _font_override(text: str, base_size: int, *, korean: bool) -> str:
    length = max((len(line) for line in text.split(r"\N")), default=0)
    if korean:
        size = base_size if length <= 30 else 54 if length <= 45 else 48 if length <= 62 else 42
    else:
        size = base_size if length <= 60 else 50 if length <= 82 else 46 if length <= 110 else 40
    return f"{{\\fs{size}}}"


def _wrap_subtitle(text: str, *, korean: bool, max_lines: int = 2) -> str:
    """Wrap ASS text predictably without adding per-frame rendering work."""
    clean = " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())
    if not clean:
        return ""
    width = 30 if korean else 52
    lines = textwrap.wrap(
        clean,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        remainder = clean[len(" ".join(lines[:-1])):].strip() if max_lines > 1 else clean
        lines[-1] = textwrap.shorten(remainder, width=width, placeholder="…")
    return "\n".join(lines)


def _group_simultaneous_lyrics(lyrics: List[dict]) -> List[dict]:
    """Merge equal timestamps so ASS events never paint on top of one another."""
    grouped: List[dict] = []
    for item in sorted(lyrics, key=lambda row: float(row.get("start_time", 0.0))):
        start = float(item.get("start_time", 0.0))
        if grouped and abs(float(grouped[-1]["start_time"]) - start) < 0.01:
            for key in ("original", "english"):
                value = str(item.get(key, "")).strip()
                if value:
                    grouped[-1][key] = f"{grouped[-1].get(key, '')} {value}".strip()
        else:
            grouped.append({**item, "start_time": start})
    return grouped


def _write_ass(lyrics: List[dict], duration: float, ass_path: str) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Original,Malgun Gothic,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,8,110,110,0,1
Style: English,Malgun Gothic,55,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,8,110,110,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    events: List[str] = []
    ordered = _group_simultaneous_lyrics(lyrics)
    for index, item in enumerate(ordered):
        start = float(item.get("start_time", 0.0))
        end = float(ordered[index + 1].get("start_time", duration)) if index < len(ordered) - 1 else duration
        end = max(start + 0.12, min(end, duration))
        raw_original = str(item.get("original", ""))
        raw_english = str(item.get("english", ""))
        original = _ass_escape(_wrap_subtitle(raw_original, korean=True))
        english = _ass_escape(_wrap_subtitle(raw_english, korean=False))
        # English-only tracks intentionally avoid duplicating the same line twice.
        same_line = raw_original.casefold().strip() == raw_english.casefold().strip()
        if original:
            y = 730 if not same_line else 750
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Original,,0,0,0,,"
                f"{{\\an8\\pos(960,{y})\\q2}}{_font_override(original, 60, korean=True)}{original}"
            )
        if english and not same_line:
            original_lines = original.count(r"\N") + 1
            english_y = 835 + (original_lines - 1) * 72
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},English,,0,0,0,,"
                f"{{\\an8\\pos(960,{english_y})\\q2}}{_font_override(english, 55, korean=False)}{english}"
            )
    with open(ass_path, "w", encoding="utf-8-sig") as file:
        file.write(header + "\n".join(events) + "\n")


def _ffmpeg_subtitle_path(path: str) -> str:
    normalized = os.path.abspath(path).replace("\\", "/")
    return normalized.replace(":", r"\:").replace("'", r"\'")


def _encode_still_video(
    *, audio_path: str, base_path: str, output_path: str, duration: float,
    subtitle_filter: Optional[str] = None,
) -> None:
    last_error: Optional[subprocess.CalledProcessError] = None
    for encoder_args in _video_encoder_candidates():
        command = [
            FFMPEG_PATH, "-y", "-loop", "1", "-framerate", "30", "-i", base_path,
            "-i", audio_path,
        ]
        if subtitle_filter:
            command += ["-vf", subtitle_filter]
        command += [
            "-t", f"{duration:.3f}", *encoder_args,
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", output_path,
        ]
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            print(f"[WARN] Video encoder failed ({encoder_args[1]}), trying fallback.")
    if last_error:
        raise last_error
    raise RuntimeError("No video encoder candidate was available.")


def _render_fast(audio_path: str, base_path: str, ass_path: str, output_path: str, duration: float) -> None:
    _encode_still_video(
        audio_path=audio_path,
        base_path=base_path,
        output_path=output_path,
        duration=duration,
        subtitle_filter=f"subtitles='{_ffmpeg_subtitle_path(ass_path)}'",
    )


def _render_lyricless(audio_path: str, base_path: str, output_path: str, duration: float) -> None:
    _encode_still_video(
        audio_path=audio_path,
        base_path=base_path,
        output_path=output_path,
        duration=duration,
    )


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        box = draw.textbbox((0, 0), text, font=font)
        return float(box[2] - box[0])


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: float) -> List[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    current = ""
    for word in words:
        test = word if not current else f"{current} {word}"
        if _text_width(draw, test, font) <= width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_centered_block(draw: ImageDraw.ImageDraw, lines: List[str], font: ImageFont.ImageFont, y: int) -> None:
    bbox = draw.textbbox((0, 0), "Ag", font=font, stroke_width=3)
    line_h = max(20, bbox[3] - bbox[1])
    for offset, line in enumerate(lines):
        box = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        x = (FRAME_SIZE[0] - (box[2] - box[0])) / 2
        draw.text((x, y + offset * (line_h + 10)), line, font=font, fill="white", stroke_width=3, stroke_fill="black")


def _render_fallback(audio_path: str, base: Image.Image, lyrics: List[dict], output_path: str, duration: float) -> None:
    if not lyrics:
        base_path = os.path.join(TEMP_DIR, "lyricless_base.jpg")
        base.convert("RGB").save(base_path, quality=94, optimize=True)
        _render_lyricless(audio_path, base_path, output_path, duration)
        return

    frames_dir = os.path.join(TEMP_DIR, "fallback_frames")
    os.makedirs(frames_dir, exist_ok=True)
    for entry in os.listdir(frames_dir):
        path = os.path.join(frames_dir, entry)
        if os.path.isfile(path):
            os.remove(path)
    base_path = os.path.join(frames_dir, "base.png")
    base.convert("RGB").save(base_path, optimize=True)
    original_font, english_font = _load_font(60), _load_font(55)
    concat_path = os.path.join(frames_dir, "concat.txt")
    entries: List[str] = []
    current = 0.0
    ordered = _group_simultaneous_lyrics(lyrics)
    for index, lyric in enumerate(ordered):
        start = float(lyric.get("start_time", 0.0))
        if start > current:
            entries += [f"file '{base_path.replace(os.sep, '/')}'", f"duration {start-current:.3f}"]
        end = float(ordered[index + 1].get("start_time", duration)) if index < len(ordered)-1 else duration
        end = max(end, start + 0.12)
        frame = base.copy().convert("RGB")
        draw = ImageDraw.Draw(frame)
        original = str(lyric.get("original", ""))
        english = str(lyric.get("english", ""))
        same_line = original.casefold().strip() == english.casefold().strip()
        original_lines = _wrap(draw, original, original_font, 1650)[:2]
        _draw_centered_block(draw, original_lines, original_font, 750 if same_line else 730)
        if english and not same_line:
            english_y = 835 + (len(original_lines) - 1) * 72
            _draw_centered_block(draw, _wrap(draw, english, english_font, 1650)[:2], english_font, english_y)
        path = os.path.join(frames_dir, f"frame_{index:04d}.jpg")
        frame.save(path, quality=92, optimize=True)
        entries += [f"file '{path.replace(os.sep, '/')}'", f"duration {end-start:.3f}"]
        current = end
    entries.append(f"file '{base_path.replace(os.sep, '/')}'")
    with open(concat_path, "w", encoding="utf-8") as file:
        file.write("\n".join(entries))
    subprocess.run([
        FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
        "-i", audio_path, "-c:v", "libx264", "-preset", "fast", "-crf", "19",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-shortest", output_path,
    ], check=True)


def make_lyric_video(audio_path: str, album_art_path: str, lyrics_json_path: str, output_path: str) -> None:
    ensure_data_dirs()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    duration = get_audio_duration(audio_path)
    if duration <= 0:
        raise ValueError("Failed to determine audio duration.")
    with Image.open(album_art_path) as image:
        base = prepare_base_frame(image)
    with open(lyrics_json_path, "r", encoding="utf-8") as file:
        lyrics = json.load(file)
    if not isinstance(lyrics, list):
        raise ValueError("Lyrics JSON must be a list.")

    work_dir = os.path.join(TEMP_DIR, "render_assets")
    os.makedirs(work_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(output_path))[0]
    base_path = os.path.join(work_dir, f"{stem}_base.jpg")
    ass_path = os.path.join(work_dir, f"{stem}.ass")
    base.convert("RGB").save(base_path, quality=94, optimize=True)

    if not lyrics:
        _render_lyricless(audio_path, base_path, output_path, duration)
        print(f"[INFO] Lyricless render saved to {output_path}")
        return

    _write_ass(lyrics, duration, ass_path)
    try:
        _render_fast(audio_path, base_path, ass_path, output_path, duration)
        print(f"[INFO] Fast ASS render saved to {output_path}")
    except subprocess.CalledProcessError as exc:
        print(f"[WARN] Fast subtitle renderer failed ({exc}); using image-sequence fallback.")
        _render_fallback(audio_path, base, lyrics, output_path, duration)


def parse_lyrics_json(json_path: str) -> List[dict]:
    with open(json_path, "r", encoding="utf-8") as file:
        return json.load(file)


def convert_timestamp_to_seconds(timestamp: str) -> float:
    hours, minutes, seconds = timestamp.replace(",", ".").split(":")
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


def convert_milliseconds_to_seconds(milliseconds: float) -> float:
    return milliseconds / 1000.0
