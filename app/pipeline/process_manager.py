"""리릭비디오 배치 처리 파이프라인."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Optional

from app.config.paths import (
    LEGACY_LYRICS_DIR,
    LYRICS_DIR,
    OUTPUT_DIR,
    TEMP_DIR,
    ensure_data_dirs,
)
from app.export.premiere_exporter import export_premiere_xml
from app.lyrics.ai_models import has_openai_api_key
from app.lyrics.openai_handler import parse_lrc_and_translate
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
            filename = self._build_available_filename(
                base_filename,
                run_target_dir,
                run_output_dir,
            )
        else:
            filename = base_filename
            run_folder_name = self._build_run_folder_name(filename)
            run_target_dir = os.path.join(config.target_dir, run_folder_name)
            run_output_dir = os.path.join(config.output_dir, run_folder_name)

        audio_path = os.path.join(run_target_dir, f"{filename}.mp3")
        image_path = os.path.join(run_target_dir, f"{filename}.jpg")
        lyrics_json_path = os.path.join(run_output_dir, f"{filename}_lyrics.json")
        output_path = os.path.join(run_output_dir, f"{filename}.mp4")
        premiere_xml_path = os.path.join(run_output_dir, f"{filename}.xml")
        copied_lrc_path = os.path.join(run_output_dir, f"{filename}.lrc")

        os.makedirs(run_target_dir, exist_ok=True)
        os.makedirs(run_output_dir, exist_ok=True)

        self.update_progress("오디오 준비 중...", 10)
        resolved_audio_path = self._prepare_audio(config, audio_path)

        self.update_progress("앨범아트 준비 중...", 30)
        if not download_album_art(
            config.album_art_url,
            image_path,
            artist=config.artist,
            title=config.title,
        ):
            raise RuntimeError("앨범아트를 준비하지 못했습니다.")

        self.update_progress("가사 준비 중...", 50)
        lrc_path = self._resolve_lrc_path(config, filename)
        if not lrc_path:
            raise RuntimeError("이 곡에 사용할 가사 파일을 찾지 못했습니다.")
        shutil.copyfile(lrc_path, copied_lrc_path)

        self.update_progress("OpenAI로 가사 번역 중...", 70)
        os.environ["CURRENT_ARTIST"] = config.artist
        os.environ["CURRENT_TITLE"] = config.title
        try:
            duration = get_audio_duration(resolved_audio_path)
            if duration <= 0:
                raise RuntimeError("다운로드한 오디오 길이를 읽지 못했습니다.")
            await parse_lrc_and_translate(lrc_path, lyrics_json_path, duration=duration)
        finally:
            os.environ.pop("CURRENT_ARTIST", None)
            os.environ.pop("CURRENT_TITLE", None)

        self._ensure_required_files(resolved_audio_path, image_path, lyrics_json_path)

        if config.output_mode == "premiere_xml":
            self.update_progress("Premiere XML 내보내는 중...", 90)
            return export_premiere_xml(
                audio_path=resolved_audio_path,
                album_art_path=image_path,
                lyrics_json_path=lyrics_json_path,
                output_xml_path=premiere_xml_path,
            )

        self.update_progress("영상 렌더링 중...", 90)
        make_lyric_video(
            audio_path=resolved_audio_path,
            album_art_path=image_path,
            lyrics_json_path=lyrics_json_path,
            output_path=output_path,
        )

        self.update_progress("완료.", 100)
        return output_path

    def process(self, config: ProcessConfig) -> str:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self.process_async(config))
        finally:
            loop.close()

    def validate_config(self, config: ProcessConfig) -> Optional[str]:
        if not config.title.strip() or not config.artist.strip():
            return "제목과 아티스트 정보가 필요합니다."
        if not config.youtube_url.strip():
            return "YouTube URL을 입력해야 합니다."
        if config.output_mode not in ("video", "premiere_xml"):
            return "지원하지 않는 출력 형식입니다."
        if not has_openai_api_key():
            return "OPENAI_API_KEY가 설정되어 있지 않습니다."
        return None

    def _prepare_audio(self, config: ProcessConfig, audio_path: str) -> str:
        if validate_audio_file(audio_path):
            return audio_path

        if not config.prefer_youtube:
            spotdl_result = download_audio_simple(
                config.artist,
                config.title,
                os.path.dirname(audio_path),
            )
            if spotdl_result and validate_audio_file(spotdl_result):
                if os.path.abspath(spotdl_result) != os.path.abspath(audio_path):
                    shutil.move(spotdl_result, audio_path)
                return audio_path

        youtube_result = download_youtube_audio(config.youtube_url, audio_path)
        if youtube_result and validate_audio_file(youtube_result):
            if os.path.abspath(youtube_result) != os.path.abspath(audio_path):
                shutil.move(youtube_result, audio_path)
            return audio_path

        raise RuntimeError("spotDL과 YouTube 대체 경로 모두 오디오 다운로드에 실패했습니다.")

    def _resolve_lrc_path(self, config: ProcessConfig, filename: str) -> Optional[str]:
        if config.lrc_path and os.path.exists(config.lrc_path):
            return config.lrc_path

        search_dirs = [LYRICS_DIR]
        if os.path.isdir(LEGACY_LYRICS_DIR):
            search_dirs.append(LEGACY_LYRICS_DIR)

        preferred_names = {
            f"{filename}.lrc",
            f"{filename}.txt",
        }

        for lyrics_dir in search_dirs:
            for preferred_name in preferred_names:
                candidate = os.path.join(lyrics_dir, preferred_name)
                if os.path.exists(candidate):
                    return candidate

        normalized_artist = self._normalize_search_token(config.artist)
        normalized_title = self._normalize_search_token(config.title)
        matching_candidates = []

        for lyrics_dir in search_dirs:
            if not os.path.isdir(lyrics_dir):
                continue
            for entry in os.listdir(lyrics_dir):
                if not entry.lower().endswith((".lrc", ".txt")):
                    continue
                normalized_name = self._normalize_search_token(entry)
                score = 0
                if normalized_artist and normalized_artist in normalized_name:
                    score += 1
                if normalized_title and normalized_title in normalized_name:
                    score += 1
                if score:
                    matching_candidates.append((score, os.path.join(lyrics_dir, entry)))

        if matching_candidates:
            matching_candidates.sort(
                key=lambda item: (
                    item[0],
                    os.path.getmtime(item[1]),
                ),
                reverse=True,
            )
            return matching_candidates[0][1]

        fetched_lyrics = get_best_lyrics(
            title=config.title,
            artist=config.artist,
        )
        if fetched_lyrics:
            target_path = os.path.join(LYRICS_DIR, f"{filename}.lrc")
            with open(target_path, "w", encoding="utf-8") as lyric_file:
                lyric_file.write(fetched_lyrics.strip() + "\n")
            return target_path

        return None

    @staticmethod
    def _ensure_required_files(*paths: str) -> None:
        for path in paths:
            if not path or not os.path.exists(path):
                raise FileNotFoundError(f"필수 파일이 없습니다: {path}")

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", filename)

    @staticmethod
    def _normalize_search_token(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    @staticmethod
    def _build_run_folder_name(filename: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"{timestamp}__{filename}"

    @classmethod
    def _build_available_filename(
        cls,
        filename: str,
        target_dir: str,
        output_dir: str,
    ) -> str:
        if not cls._filename_exists(filename, target_dir, output_dir):
            return filename

        counter = 2
        while cls._filename_exists(f"{filename}_{counter}", target_dir, output_dir):
            counter += 1
        return f"{filename}_{counter}"

    @staticmethod
    def _filename_exists(filename: str, target_dir: str, output_dir: str) -> bool:
        candidates = (
            os.path.join(target_dir, f"{filename}.mp3"),
            os.path.join(target_dir, f"{filename}.jpg"),
            os.path.join(output_dir, f"{filename}.mp4"),
            os.path.join(output_dir, f"{filename}.xml"),
            os.path.join(output_dir, f"{filename}.lrc"),
            os.path.join(output_dir, f"{filename}_lyrics.json"),
        )
        return any(os.path.exists(candidate) for candidate in candidates)
