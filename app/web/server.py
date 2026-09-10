"""Local-browser workbench for high-volume lyric video production."""
from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from app.config.paths import BASE_DIR, LYRICS_DIR, OUTPUT_DIR, REVIEW_DIR, ensure_data_dirs
from app.lyrics.exception_policy import classify_lyrics
from app.lyrics.translator_v2 import apply_human_translation_hints, parse_lyrics_for_review
from app.pipeline.playlist_importer import PlaylistSourceTrack, _prepare_track, import_playlist
from app.pipeline.process_manager import (
    ProcessConfig,
    ProcessManager,
    TimingReviewRequired,
    TranslationReviewRequired,
)
from app.sources.genie_handler import get_best_lyrics, parse_genie_extra_info, search_genie_songs
from app.sources.youtube_handler import download_youtube_audio, youtube_search

load_dotenv(os.path.join(BASE_DIR, ".env"))
ensure_data_dirs()
STATIC_DIR = Path(__file__).resolve().parent / "static"
app = FastAPI(title="Lyric Video Maker Workbench")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
SERVER: Optional[uvicorn.Server] = None
MAX_PARALLEL_TRACKS = 2


@dataclass
class WorkItem:
    id: str
    label: str
    config: ProcessConfig
    lyrics_mode: str
    status: str
    language_mode: str = "unknown"
    reason: str = ""
    progress: int = 0
    progress_text: str = ""
    result_path: str = ""
    review_audio_path: str = ""
    translation_json_path: str = ""
    translation_issues: List[dict] = field(default_factory=list)
    timing_score: int = 100

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "title": self.config.title,
            "artist": self.config.artist,
            "lyrics_mode": self.lyrics_mode,
            "language_mode": self.language_mode,
            "status": self.status,
            "reason": self.reason,
            "progress": self.progress,
            "progress_text": self.progress_text,
            "result_path": self.result_path,
            "translation_issue_count": len(self.translation_issues),
            "timing_score": self.timing_score,
        }


@dataclass
class WorkbenchState:
    items: List[WorkItem] = field(default_factory=list)
    analysis_busy: bool = False
    render_busy: bool = False
    activity: str = "대기 중"
    last_error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)

    def snapshot(self) -> dict:
        with self.lock:
            counts: Dict[str, int] = {}
            for item in self.items:
                counts[item.status] = counts.get(item.status, 0) + 1
            return {
                "items": [item.public() for item in self.items],
                "counts": counts,
                "analysis_busy": self.analysis_busy,
                "render_busy": self.render_busy,
                "activity": self.activity,
                "last_error": self.last_error,
                "api_key_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
                "model_routing": {
                    "translation": "GPT-5.6 Luna",
                    "difficult_translation": "GPT-5.6 Terra",
                    "final_ambiguity": "GPT-5.6 Sol",
                    "auto_sync_transcription": "GPT-Transcribe",
                    "auto_sync_alignment": "GPT-5.6 Luna",
                },
                "output_dir": OUTPUT_DIR,
            }

    def get(self, item_id: str) -> WorkItem:
        with self.lock:
            for item in self.items:
                if item.id == item_id:
                    return item
        raise KeyError(item_id)


STATE = WorkbenchState()


class AnalyzeRequest(BaseModel):
    input: str
    output_mode: str = "video"


class LyricsRequest(BaseModel):
    text: str


class SyncPoint(BaseModel):
    time: float
    text: str


class SyncSaveRequest(BaseModel):
    points: List[SyncPoint]


class TranslationHintRequest(BaseModel):
    hints: Dict[int, str] = Field(default_factory=dict)


class SettingsRequest(BaseModel):
    api_key: Optional[str] = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def state() -> dict:
    return STATE.snapshot()


def _sanitize(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text).strip() or "lyrics"


def _language_mode(config: ProcessConfig) -> str:
    if not config.lrc_path or not os.path.exists(config.lrc_path):
        return "lyricless"
    try:
        with open(config.lrc_path, "r", encoding="utf-8") as file:
            entries = parse_lyrics_for_review(file.read(), duration=0.0)
        policy = classify_lyrics([str(item.get("original", "")) for item in entries], title=config.title)
        return policy.language_mode
    except Exception:
        return "unknown"


def _make_item(config: ProcessConfig, label: str, lyrics_mode: str, *, reason: str = "") -> WorkItem:
    if lyrics_mode in {"missing", "lyricless"}:
        config.allow_lyricless = True
        return WorkItem(
            id=uuid.uuid4().hex[:12],
            label=label,
            config=config,
            lyrics_mode="lyricless",
            language_mode="lyricless",
            status="ready",
            reason=reason or "가사 없음 · 가사 없이 그대로 렌더링",
        )

    language = _language_mode(config)
    auto_note = "Plain lyric · 렌더 시 AI 자동 싱크" if lyrics_mode == "plain" else reason
    return WorkItem(
        id=uuid.uuid4().hex[:12],
        label=label,
        config=config,
        lyrics_mode=lyrics_mode,
        language_mode=language,
        status="ready",
        reason=auto_note,
    )


def _single_from_query(query: str, output_mode: str) -> WorkItem:
    genie_results = search_genie_songs(query, limit=6)
    if not genie_results:
        raise RuntimeError("곡 검색 결과를 찾지 못했습니다. YouTube URL을 직접 넣어보세요.")
    title, song_id, extra, art_url, duration = genie_results[0]
    artist, album = parse_genie_extra_info(extra)
    yt_results = youtube_search(f"{artist} {title}", target_duration=duration)
    if not yt_results:
        raise RuntimeError("일치하는 YouTube 음원을 찾지 못했습니다.")
    youtube_url = yt_results[0]["link"]
    lyrics = get_best_lyrics(song_id=song_id, title=title, artist=artist, album=album, duration=duration)
    config = ProcessConfig(
        title=title,
        artist=artist or "Unknown artist",
        album_art_url=art_url or yt_results[0].get("thumbnail", ""),
        youtube_url=youtube_url,
        output_mode=output_mode,
        prefer_youtube=True,
    )
    if not lyrics:
        return _make_item(config, f"{config.artist} - {title}", "lyricless", reason="자동 가사 검색 결과 없음")
    path = os.path.join(LYRICS_DIR, f"{_sanitize(config.artist + ' - ' + title)}.lrc")
    with open(path, "w", encoding="utf-8") as file:
        file.write(lyrics.strip() + "\n")
    config.lrc_path = path
    synced = any(line.strip().startswith("[") and "]" in line for line in lyrics.splitlines())
    return _make_item(config, f"{config.artist} - {title}", "synced" if synced else "plain")


def _single_from_url(url: str, output_mode: str) -> WorkItem:
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("YouTube 메타데이터를 읽지 못했습니다.")
    title = str(info.get("track") or info.get("title") or "Unknown title").strip()
    artist = str(info.get("artist") or info.get("uploader") or info.get("channel") or "Unknown artist").strip()
    source = PlaylistSourceTrack(
        index=1,
        title=title,
        artist=artist.replace(" - Topic", ""),
        album=str(info.get("album") or "").strip(),
        youtube_url=str(info.get("webpage_url") or url),
        duration=int(info.get("duration")) if info.get("duration") else None,
        thumbnail_url=str(info.get("thumbnail") or ""),
        playlist_title="Single track",
    )
    prepared = _prepare_track(source, output_mode=output_mode, lyrics_policy="allow_plain")
    if prepared:
        return _make_item(prepared.config, prepared.label, prepared.lyrics_mode)
    config = ProcessConfig(
        title=source.title,
        artist=source.artist,
        album_art_url=source.thumbnail_url,
        youtube_url=source.youtube_url,
        output_mode=output_mode,
        prefer_youtube=True,
        allow_lyricless=True,
    )
    return _make_item(config, source.label, "lyricless", reason="자동 가사 검색 결과 없음")


def _set_activity(message: str) -> None:
    with STATE.lock:
        STATE.activity = message


def _analyze_worker(raw_input: str, output_mode: str) -> None:
    try:
        with STATE.lock:
            STATE.activity = "입력 분석 중"
            STATE.last_error = ""
            STATE.items.clear()
        if "list=" in raw_input and ("youtube.com" in raw_input or "youtu.be" in raw_input):
            report = import_playlist(
                raw_input,
                output_mode=output_mode,
                lyrics_policy="allow_plain",
                progress_callback=_set_activity,
            )
            items: List[WorkItem] = [
                _make_item(track.config, track.label, track.lyrics_mode)
                for track in report.prepared_tracks
            ]
            for skipped in report.skipped_tracks:
                source = skipped.source
                config = ProcessConfig(
                    title=source.title,
                    artist=source.artist or "Unknown artist",
                    album_art_url=source.thumbnail_url,
                    youtube_url=source.youtube_url,
                    output_mode=output_mode,
                    prefer_youtube=True,
                    allow_lyricless=True,
                )
                items.append(_make_item(config, source.label, "lyricless", reason=skipped.reason))
        elif raw_input.startswith("http://") or raw_input.startswith("https://"):
            items = [_single_from_url(raw_input, output_mode)]
        else:
            items = [_single_from_query(raw_input, output_mode)]

        with STATE.lock:
            STATE.items = items
            lyricless = sum(item.lyrics_mode == "lyricless" for item in items)
            plain = sum(item.lyrics_mode == "plain" for item in items)
            STATE.activity = f"분석 완료 · READY {len(items)} · 자동 싱크 예정 {plain} · 가사 없음 {lyricless}"
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.activity = "분석 실패"
    finally:
        with STATE.lock:
            STATE.analysis_busy = False


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest) -> dict:
    raw_input = request.input.strip()
    if not raw_input:
        raise HTTPException(400, "곡명, YouTube URL, 앨범 또는 플레이리스트 URL을 입력하세요.")
    with STATE.lock:
        if STATE.analysis_busy or STATE.render_busy:
            raise HTTPException(409, "이미 작업이 진행 중입니다.")
        STATE.analysis_busy = True
    threading.Thread(target=_analyze_worker, args=(raw_input, request.output_mode), daemon=True).start()
    return {"started": True}


@app.post("/api/items/{item_id}/exclude")
def exclude(item_id: str) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    with STATE.lock:
        item.status = "excluded"
    return item.public()


@app.post("/api/items/{item_id}/include")
def include(item_id: str) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    with STATE.lock:
        item.status = "ready"
        item.reason = "가사 없음 · 그대로 렌더링" if item.lyrics_mode == "lyricless" else item.reason
    return item.public()


@app.get("/api/items/{item_id}/lyrics")
def get_lyrics(item_id: str) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    text = ""
    if item.config.lrc_path and os.path.exists(item.config.lrc_path):
        with open(item.config.lrc_path, "r", encoding="utf-8") as file:
            text = file.read()
    return {"text": text, "mode": item.lyrics_mode, "language_mode": item.language_mode}


@app.post("/api/items/{item_id}/lyrics")
def set_lyrics(item_id: str, request: LyricsRequest) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    text = request.text.strip()
    if not text:
        raise HTTPException(400, "가사를 입력하세요.")
    path = os.path.join(LYRICS_DIR, f"{_sanitize(item.config.artist + ' - ' + item.config.title)}.lrc")
    with open(path, "w", encoding="utf-8") as file:
        file.write(text + "\n")
    synced = any(line.strip().startswith("[") and "]" in line for line in text.splitlines())
    with STATE.lock:
        item.config.lrc_path = path
        item.config.pretranslated_json_path = None
        item.lyrics_mode = "synced" if synced else "plain"
        item.language_mode = _language_mode(item.config)
        item.status = "ready"
        item.reason = "" if synced else "Plain lyric · 렌더 시 AI 자동 싱크"
        item.translation_json_path = ""
        item.translation_issues.clear()
    return item.public()


@app.post("/api/items/{item_id}/sync/prepare")
def prepare_sync(item_id: str) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    if not item.config.lrc_path or not os.path.exists(item.config.lrc_path):
        raise HTTPException(400, "먼저 가사를 추가해야 합니다.")
    audio_path = os.path.join(REVIEW_DIR, f"{item.id}.mp3")
    if not os.path.exists(audio_path):
        downloaded = download_youtube_audio(item.config.youtube_url, audio_path)
        if not downloaded:
            raise HTTPException(500, "싱크용 음원을 준비하지 못했습니다.")
        audio_path = downloaded
    from app.media.video_maker import get_audio_duration
    duration = get_audio_duration(audio_path)
    with open(item.config.lrc_path, "r", encoding="utf-8") as file:
        lyric_text = file.read()
    points = parse_lyrics_for_review(lyric_text, duration=duration)
    with STATE.lock:
        item.review_audio_path = audio_path
    return {
        "audio_url": f"/api/items/{item.id}/audio",
        "duration": duration,
        "timing_score": item.timing_score,
        "points": [
            {"time": float(point["start_time"]), "text": str(point["original"])}
            for point in points
        ],
    }


@app.get("/api/items/{item_id}/audio")
def review_audio(item_id: str) -> FileResponse:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    if not item.review_audio_path or not os.path.exists(item.review_audio_path):
        raise HTTPException(404, "먼저 싱크 준비를 실행하세요.")
    return FileResponse(item.review_audio_path, media_type="audio/mpeg", filename=os.path.basename(item.review_audio_path))


@app.post("/api/items/{item_id}/sync/save")
def save_sync(item_id: str, request: SyncSaveRequest) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    points = list(request.points)
    if not points:
        raise HTTPException(400, "저장할 타이밍이 없습니다.")
    path = os.path.join(LYRICS_DIR, f"{_sanitize(item.config.artist + ' - ' + item.config.title)}.lrc")
    last_time = 0.0
    with open(path, "w", encoding="utf-8") as file:
        for point in points:
            current_time = max(last_time, float(point.time))
            minutes = int(current_time // 60)
            seconds = current_time % 60
            file.write(f"[{minutes:02d}:{seconds:05.2f}] {point.text.strip()}\n")
            last_time = current_time
    with STATE.lock:
        item.config.lrc_path = path
        item.config.pretranslated_json_path = None
        item.lyrics_mode = "synced"
        item.language_mode = _language_mode(item.config)
        item.status = "ready"
        item.reason = ""
        item.timing_score = 100
    return item.public()


@app.get("/api/items/{item_id}/translation-review")
def translation_review(item_id: str) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    return {"issues": item.translation_issues, "count": len(item.translation_issues)}


@app.post("/api/items/{item_id}/translation-review")
def resolve_translation_review(item_id: str, request: TranslationHintRequest) -> dict:
    try:
        item = STATE.get(item_id)
    except KeyError:
        raise HTTPException(404, "곡을 찾지 못했습니다.")
    if not item.translation_json_path or not os.path.exists(item.translation_json_path):
        raise HTTPException(400, "검수할 번역 결과 파일이 없습니다.")
    hints = {int(index): text.strip() for index, text in request.hints.items() if text.strip()}
    if not hints:
        raise HTTPException(400, "애매한 구절에 의미 힌트를 한 줄 이상 입력하세요.")
    try:
        remaining = asyncio.run(
            apply_human_translation_hints(
                item.translation_json_path,
                artist=item.config.artist,
                title=item.config.title,
                hints=hints,
            )
        )
    except Exception as exc:
        raise HTTPException(500, f"힌트 기반 재번역 실패: {exc}") from exc
    with STATE.lock:
        item.translation_issues = remaining
        item.config.pretranslated_json_path = item.translation_json_path
        if remaining:
            item.status = "needs_translation_review"
            item.reason = f"아직 {len(remaining)}개 구절이 애매합니다."
        else:
            item.status = "ready"
            item.reason = "사용자 의미 힌트 반영 완료"
    return item.public()


def _process_one(item: WorkItem, batch_name: str) -> None:
    with STATE.lock:
        item.status = "processing"
        item.progress = 0
        item.progress_text = "준비 중"
        item.reason = ""

    def progress(message: str, value: int) -> None:
        with STATE.lock:
            item.progress = value
            item.progress_text = message

    config = deepcopy(item.config)
    config.batch_name = batch_name
    try:
        result = ProcessManager(progress).process(config)
        with STATE.lock:
            item.status = "done"
            item.progress = 100
            item.progress_text = "완료"
            item.result_path = result
            item.reason = ""
    except TimingReviewRequired as exc:
        with STATE.lock:
            item.config.lrc_path = exc.lrc_path
            item.lyrics_mode = "synced" if exc.score > 0 else "plain"
            item.status = "needs_sync"
            item.timing_score = exc.score
            item.reason = str(exc)
            item.progress_text = "싱크 확인 필요"
    except TranslationReviewRequired as exc:
        with STATE.lock:
            item.status = "needs_translation_review"
            item.translation_json_path = exc.json_path
            item.translation_issues = list(exc.issues)
            item.reason = str(exc)
            item.progress_text = "의미 확인 필요"
    except Exception as exc:
        with STATE.lock:
            item.status = "failed"
            item.reason = str(exc)
            item.progress_text = "실패"


def _render_worker() -> None:
    batch_name = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    with STATE.lock:
        targets = [item for item in STATE.items if item.status == "ready"]
        STATE.activity = f"{len(targets)}곡 처리 시작 · 최대 {MAX_PARALLEL_TRACKS}곡 동시"
    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TRACKS, max(1, len(targets)))) as pool:
            futures = {pool.submit(_process_one, item, batch_name): item for item in targets}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    item = futures[future]
                    with STATE.lock:
                        item.status = "failed"
                        item.reason = str(exc)
                completed += 1
                with STATE.lock:
                    review = sum(item.status in {"needs_sync", "needs_translation_review"} for item in STATE.items)
                    failed = sum(item.status == "failed" for item in STATE.items)
                    STATE.activity = f"{completed}/{len(targets)} 처리 · 확인 필요 {review} · 실패 {failed}"
    finally:
        with STATE.lock:
            STATE.render_busy = False
            done = sum(item.status == "done" for item in STATE.items)
            review = sum(item.status in {"needs_sync", "needs_translation_review"} for item in STATE.items)
            failed = sum(item.status == "failed" for item in STATE.items)
            STATE.activity = f"배치 완료 · 성공 {done} · 확인 필요 {review} · 실패 {failed}"


@app.post("/api/render")
def render() -> dict:
    with STATE.lock:
        if STATE.render_busy or STATE.analysis_busy:
            raise HTTPException(409, "이미 작업이 진행 중입니다.")
        ready = [item for item in STATE.items if item.status == "ready"]
        if not ready:
            raise HTTPException(400, "렌더 가능한 READY 곡이 없습니다.")
        STATE.render_busy = True
    threading.Thread(target=_render_worker, daemon=True).start()
    return {"started": True, "count": len(ready)}


@app.post("/api/settings")
def settings(request: SettingsRequest) -> dict:
    env_path = os.path.join(BASE_DIR, ".env")
    existing: List[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as file:
            existing = file.read().splitlines()
    if request.api_key is not None and request.api_key.strip():
        key = request.api_key.strip()
        existing = [line for line in existing if not line.startswith("OPENAI_API_KEY=")]
        existing.append(f"OPENAI_API_KEY={key}")
        os.environ["OPENAI_API_KEY"] = key
        with open(env_path, "w", encoding="utf-8") as file:
            file.write("\n".join(existing).rstrip() + "\n")
    return STATE.snapshot()


@app.post("/api/open-output")
def open_output() -> dict:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if os.name == "nt":
        os.startfile(OUTPUT_DIR)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", OUTPUT_DIR])
    else:
        subprocess.Popen(["xdg-open", OUTPUT_DIR])
    return {"opened": True}


@app.post("/api/shutdown")
def shutdown() -> dict:
    def stop() -> None:
        time.sleep(0.25)
        if SERVER is not None:
            SERVER.should_exit = True
    threading.Thread(target=stop, daemon=True).start()
    return {"stopping": True}


def _find_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run() -> None:
    global SERVER
    port = _find_port()
    url = f"http://127.0.0.1:{port}"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    SERVER = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    SERVER.run()
