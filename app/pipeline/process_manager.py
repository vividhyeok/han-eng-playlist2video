"""Batch-safe orchestration for lyric video generation."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Optional

from app.config.paths import LEGACY_LYRICS_DIR, LYRICS_DIR, OUTPUT_DIR, TEMP_DIR, ensure_data_dirs
from app.export.premiere_exporter import export_premiere_xml
from app.lyrics.ai_models import has_openai_api_key
from app.lyrics.translator_v2 import parse_lrc_and_translate
from app.media.video_maker import get_audio_duration, make_lyric_video
from app.sources.album_art_finder import download_album_art
from app.sources.genie_handler import get_best_lyrics
from app.sources.spotdl_handler import download_audio_simple
from app.sources.youtube_handler import download_youtube_audio, validate_audio_file

OutputMode = Literal["video", "premiere_xml"]


@dataclass
class ProcessConfig:
    title: str
    artist: str
    album_art_url: str
    youtube_url: str
    output_mode: OutputMode = "video"
    target_dir: str = TEMP_DIR
    output_dir: str = OUTPUT_DIR
    lrc_path: Optional[str] = None
    prefer_youtube: bool = False
    batch_name: Optional[str] = None


class ProcessManager:
    def __init__(self, update_progress: Callable[[str, int], None]):
        self.update_progress = update_progress

    async def process_async(self, config: ProcessConfig) -> str:
        ensure_data_dirs()
        base_filename = self._sanitize_filename(f"{config.artist} - {config.title}")
        if config.batch_name:
            run_folder_name = config.batch_name
            run_target_dir = os.path.join(config.target_dir, run_folder_name)
            run_output_dir = os.path.join(config.output_dir, run_folder_name)
            os.makedirs(run_target_dir, exist_ok=True)
            os.makedirs(run_output_dir, exist_ok=True)
            filename = self._build_available_filename(base_filename, run_target_dir, run_output_dir)
        else:
            filename = base_filename
            run_folder_name = self._build_run_folder_name(filename)
            run_target_dir = os.path.join(config.target_dir, run_folder_name)
            run_output_dir = os.path.join(config.output_dir, run_folder_name)

        os.makedirs(run_target_dir, exist_ok=True)
        os.makedirs(run_output_dir, exist_ok=True)
        audio_path = os.path.join(run_target_dir, f"{filename}.mp3")
        image_path = os.path.join(run_target_dir, f"{filename}.jpg")
        lyrics_json_path = os.path.join(run_output_dir, f"{filename}_lyrics.json")
        output_path = os.path.join(run_output_dir, f"{filename}.mp4")
        premiere_xml_path = os.path.join(run_output_dir, f"{filename}.xml")
        copied_lrc_path = os.path.join(run_output_dir, f"{filename}.lrc")

        self.update_progress("음원 준비", 10)
        resolved_audio = self._prepare_audio(config, audio_path)

        self.update_progress("앨범아트 준비", 28)
        if not download_album_art(config.album_art_url, image_path, artist=config.artist, title=config.title):
            raise RuntimeError("앨범아트를 가져오지 못했습니다.")

        self.update_progress("가사 준비", 44)
        lrc_path = self._resolve_lrc_path(config, filename)
        if not lrc_path:
            raise RuntimeError("사용 가능한 가사를 찾지 못했습니다.")
        shutil.copyfile(lrc_path, copied_lrc_path)

        self.update_progress("전체 문맥 번역", 62)
        duration = get_audio_duration(resolved_audio)
        if duration <= 0:
            raise RuntimeError("음원 길이를 읽지 못했습니다.")
        await parse_lrc_and_translate(
            lrc_path,
            lyrics_json_path,
            duration=duration,
            artist=config.artist,
            title=config.title,
        )
        self._ensure_required_files(resolved_audio, image_path, lyrics_json_path)

        if config.output_mode == "premiere_xml":
            self.update_progress("Premiere XML 생성", 88)
            result = export_premiere_xml(
                audio_path=resolved_audio,
                album_art_path=image_path,
                lyrics_json_path=lyrics_json_path,
                output_xml_path=premiere_xml_path,
            )
        else:
            self.update_progress("빠른 영상 렌더링", 84)
            make_lyric_video(resolved_audio, image_path, lyrics_json_path, output_path)
            result = output_path

        self.update_progress("완료", 100)
        return result

    def process(self, config: ProcessConfig) -> str:
        return asyncio.run(self.process_async(config))

    def validate_config(self, config: ProcessConfig) -> Optional[str]:
        if not config.title.strip() or not config.artist.strip():
            return "제목과 아티스트가 필요합니다."
        if not config.youtube_url.strip():
            return "YouTube 음원 URL이 필요합니다."
        if config.output_mode not in ("video", "premiere_xml"):
            return "지원하지 않는 출력 형식입니다."
        if not has_openai_api_key():
            return "OpenAI API 키가 설정되지 않았습니다."
        return None

    def _prepare_audio(self, config: ProcessConfig, audio_path: str) -> str:
        if validate_audio_file(audio_path):
            return audio_path
        if not config.prefer_youtube:
            spotdl_result = download_audio_simple(config.artist, config.title, os.path.dirname(audio_path))
            if spotdl_result and validate_audio_file(spotdl_result):
                if os.path.abspath(spotdl_result) != os.path.abspath(audio_path):
                    shutil.move(spotdl_result, audio_path)
                return audio_path
        youtube_result = download_youtube_audio(config.youtube_url, audio_path)
        if youtube_result and validate_audio_file(youtube_result):
            if os.path.abspath(youtube_result) != os.path.abspath(audio_path):
                shutil.move(youtube_result, audio_path)
            return audio_path
        raise RuntimeError("음원 다운로드에 실패했습니다.")

    def _resolve_lrc_path(self, config: ProcessConfig, filename: str) -> Optional[str]:
        if config.lrc_path and os.path.exists(config.lrc_path):
            return config.lrc_path

        search_dirs = [LYRICS_DIR]
        if os.path.isdir(LEGACY_LYRICS_DIR):
            search_dirs.append(LEGACY_LYRICS_DIR)
        preferred = {f"{filename}.lrc", f"{filename}.txt"}
        for directory in search_dirs:
            for name in preferred:
                candidate = os.path.join(directory, name)
                if os.path.exists(candidate):
                    return candidate

        artist_token = self._normalize_search_token(config.artist)
        title_token = self._normalize_search_token(config.title)
        matches = []
        for directory in search_dirs:
            if not os.path.isdir(directory):
                continue
            for entry in os.listdir(directory):
                if not entry.lower().endswith((".lrc", ".txt")):
                    continue
                normalized = self._normalize_search_token(entry)
                score = int(bool(artist_token and artist_token in normalized)) + int(bool(title_token and title_token in normalized))
                if score:
                    matches.append((score, os.path.getmtime(os.path.join(directory, entry)), os.path.join(directory, entry)))
        if matches:
            matches.sort(reverse=True)
            return matches[0][2]

        fetched = get_best_lyrics(title=config.title, artist=config.artist)
        if fetched:
            target = os.path.join(LYRICS_DIR, f"{filename}.lrc")
            with open(target, "w", encoding="utf-8") as file:
                file.write(fetched.strip() + "\n")
            return target
        return None

    @staticmethod
    def _ensure_required_files(*paths: str) -> None:
        for path in paths:
            if not path or not os.path.exists(path):
                raise FileNotFoundError(f"Required file is missing: {path}")

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", filename)

    @staticmethod
    def _normalize_search_token(text: str) -> str:
        return re.sub(r"[^a-z0-9가-힣]+", "", text.casefold())

    @staticmethod
    def _build_run_folder_name(filename: str) -> str:
        return f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}__{filename}"

    @classmethod
    def _build_available_filename(cls, filename: str, target_dir: str, output_dir: str) -> str:
        if not cls._filename_exists(filename, target_dir, output_dir):
            return filename
        counter = 2
        while cls._filename_exists(f"{filename}_{counter}", target_dir, output_dir):
            counter += 1
        return f"{filename}_{counter}"

    @staticmethod
    def _filename_exists(filename: str, target_dir: str, output_dir: str) -> bool:
        return any(os.path.exists(path) for path in (
            os.path.join(target_dir, f"{filename}.mp3"),
            os.path.join(target_dir, f"{filename}.jpg"),
            os.path.join(output_dir, f"{filename}.mp4"),
            os.path.join(output_dir, f"{filename}.xml"),
            os.path.join(output_dir, f"{filename}.lrc"),
            os.path.join(output_dir, f"{filename}_lyrics.json"),
        ))
