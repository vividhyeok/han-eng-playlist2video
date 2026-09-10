"""Batch-safe orchestration for lyric video generation."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Optional

from app.config.paths import LEGACY_LYRICS_DIR, LYRICS_DIR, OUTPUT_DIR, TEMP_DIR, ensure_data_dirs
from app.export.premiere_exporter import export_premiere_xml
from app.lyrics.ai_models import has_openai_api_key
from app.lyrics.exception_policy import assess_timing, classify_lyrics
from app.lyrics.translator_v2 import get_translation_review_issues, parse_lrc_and_translate, parse_lyrics_for_review
from app.media.video_maker import get_audio_duration, make_lyric_video
from app.sources.album_art_finder import download_album_art
from app.sources.genie_handler import get_best_lyrics, lyrics_are_synced
from app.sources.spotdl_handler import download_audio_simple
from app.sources.youtube_handler import download_youtube_audio, validate_audio_file

OutputMode = Literal["video", "premiere_xml"]


class ReviewRequired(RuntimeError):
    """Base class for a track that needs human attention but is not a failed job."""


class TimingReviewRequired(ReviewRequired):
    def __init__(self, message: str, *, lrc_path: str, score: int = 0):
        super().__init__(message)
        self.lrc_path = lrc_path
        self.score = score


class TranslationReviewRequired(ReviewRequired):
    def __init__(self, message: str, *, json_path: str, issues: list[dict]):
        super().__init__(message)
        self.json_path = json_path
        self.issues = issues


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
    allow_lyricless: bool = True


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
        duration = get_audio_duration(resolved_audio)
        if duration <= 0:
            raise RuntimeError("음원 길이를 읽지 못했습니다.")

        self.update_progress("앨범아트 준비", 26)
        if not download_album_art(config.album_art_url, image_path, artist=config.artist, title=config.title):
            raise RuntimeError("앨범아트를 가져오지 못했습니다.")

        self.update_progress("가사 확인", 40)
        lrc_path = self._resolve_lrc_path(config, filename)

        if not lrc_path:
            if not config.allow_lyricless:
                raise RuntimeError("사용 가능한 가사를 찾지 못했습니다.")
            # Missing lyrics are a valid production state. No OpenAI translation call.
            self.update_progress("가사 없음 · 리릭리스 영상 준비", 64)
            with open(lyrics_json_path, "w", encoding="utf-8") as file:
                json.dump([], file)
        else:
            shutil.copyfile(lrc_path, copied_lrc_path)
            with open(lrc_path, "r", encoding="utf-8") as file:
                lyric_text = file.read()

            # Plain lyrics must be synced before rendering. Do not silently distribute them.
            if not lyrics_are_synced(lyric_text):
                raise TimingReviewRequired(
                    "Plain lyric이므로 타이밍 확인이 필요합니다.",
                    lrc_path=lrc_path,
                    score=0,
                )

            parsed_for_qa = parse_lyrics_for_review(lyric_text, duration=duration)
            timing_qa = assess_timing(parsed_for_qa, duration)
            if timing_qa.suspicious:
                raise TimingReviewRequired(
                    "가사 타이밍이 의심됩니다: " + ", ".join(timing_qa.reasons),
                    lrc_path=lrc_path,
                    score=timing_qa.score,
                )

            originals = [str(item.get("original", "")) for item in parsed_for_qa]
            language_policy = classify_lyrics(originals, title=config.title)
            if language_policy.translation_required and not has_openai_api_key():
                raise RuntimeError("한국어 번역이 필요한 곡인데 OpenAI API 키가 설정되지 않았습니다.")

            self.update_progress(
                "영어 가사 · 번역 생략" if not language_policy.translation_required else "문맥 기반 한영 번역",
                62,
            )
            await parse_lrc_and_translate(
                lrc_path,
                lyrics_json_path,
                duration=duration,
                artist=config.artist,
                title=config.title,
            )
            issues = get_translation_review_issues(lyrics_json_path)
            if issues:
                raise TranslationReviewRequired(
                    f"AI가 {len(issues)}개 구절을 끝까지 확정하지 못했습니다.",
                    json_path=lyrics_json_path,
                    issues=issues,
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
