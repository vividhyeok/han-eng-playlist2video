"""Main window for playlist review and batch rendering."""

from __future__ import annotations

import os
import shutil
from copy import deepcopy
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSplitter, QTextEdit, QVBoxLayout, QWidget

from app.config.config_manager import get_config
from app.config.paths import TEMP_DIR, TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import OPENAI_MODELS, resolve_model
from app.pipeline.playlist_importer import PlaylistImportReport, PlaylistReviewTrack, import_playlist, save_review_track_lyrics
from app.pipeline.process_manager import OutputMode, ProcessConfig, ProcessManager
from app.sources.channel_index import ChannelVideoIndex, build_channel_video_index, load_channel_video_index, normalize_channel_url, save_channel_video_index
from app.sources.genie_handler import lyrics_are_synced
from app.sources.youtube_handler import sanitize_youtube_url
from app.ui.styles import MODERN_STYLESHEET
from app.ui.track_review_dialog import TrackReviewDialog

FIXED_CHANNEL_URL = "https://www.youtube.com/@korbittherabbit/videos"


class WorkerThread(QThread):
    progress = pyqtSignal(str, int)
    result_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, config: ProcessConfig):
        super().__init__()
        self.config = deepcopy(config)
        self.manager = ProcessManager(self.progress.emit)

    def run(self) -> None:
        validation_error = self.manager.validate_config(self.config)
        if validation_error:
            self.error_occurred.emit(validation_error)
            return
        try:
            output_path = self.manager.process(self.config)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return
        self.result_ready.emit(output_path)


class PlaylistImportWorker(QThread):
    progress = pyqtSignal(str)
    report_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, playlist_url: str, output_mode: str, channel_index: Optional[ChannelVideoIndex] = None):
        super().__init__()
        self.playlist_url = playlist_url
        self.output_mode = output_mode
        self.channel_index = deepcopy(channel_index)

    def run(self) -> None:
        try:
            report = import_playlist(
                self.playlist_url,
                output_mode=self.output_mode,
                channel_index=self.channel_index,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return
        self.report_ready.emit(report)


class ChannelIndexWorker(QThread):
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, channel_url: str):
        super().__init__()
        self.channel_url = channel_url

    def run(self) -> None:
        try:
            index = build_channel_video_index(self.channel_url, progress_callback=self.progress.emit)
            save_channel_video_index(index)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return
        self.result_ready.emit(index)


class PlaylistPipelineWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_data_dirs()
        self.setWindowTitle("플레이리스트 리릭비디오 파이프라인")
        self.setMinimumSize(1500, 960)
        self.setStyleSheet(MODERN_STYLESHEET)
        self.config_manager = get_config()
        self.output_mode: OutputMode = self.config_manager.get("output_mode", "video")
        self.review_tracks: list[PlaylistReviewTrack] = []
        self.render_queue: list[PlaylistReviewTrack] = []
        self.current_track_index = -1
        self.current_queue_index = 0
        self.current_queue_batch_name: Optional[str] = None
        self.current_playlist_title = ""
        self.worker: Optional[WorkerThread] = None
        self.import_worker: Optional[PlaylistImportWorker] = None
        self.channel_worker: Optional[ChannelIndexWorker] = None
        self.channel_index: Optional[ChannelVideoIndex] = load_channel_video_index()
        self.worker_result_path: Optional[str] = None
        self.worker_error_message: Optional[str] = None
        self.processing_mode: Optional[str] = None
        self.pending_import_url: Optional[str] = None
        self.pending_import_output_mode: Optional[str] = None
        self.last_duplicate_skip_count = 0
        self.last_progress_message = ""
        self._loading_track_details = False
        self._updating_track_list = False
        self._build_ui()
        self._load_saved_state()
        self._refresh_pipeline_state()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.addWidget(self._build_header_card())
        root.addWidget(self._build_setup_card())
        root.addWidget(self._build_summary_card())
        splitter = QSplitter()
        splitter.addWidget(self._build_track_list_card())
        splitter.addWidget(self._build_detail_card())
        root.addWidget(splitter, stretch=1)
        root.addWidget(self._build_progress_card(), stretch=1)

    def _build_header_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("플레이리스트 리릭비디오 파이프라인"); title.setObjectName("title"); layout.addWidget(title)
        note = QLabel("플레이리스트를 먼저 불러오고, 필요한 곡만 가사 확인이나 싱크를 손본 뒤 바로 렌더하면 됩니다."); note.setWordWrap(True); layout.addWidget(note)
        return frame

    def _build_setup_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        header = QLabel("분석 설정"); header.setObjectName("subtitle"); layout.addWidget(header)
        form = QGridLayout()
        form.addWidget(QLabel("플레이리스트 URL"), 0, 0)
        self.playlist_url_input = QLineEdit(); self.playlist_url_input.setPlaceholderText("https://music.youtube.com/playlist?list=... 또는 https://www.youtube.com/playlist?list=..."); self.playlist_url_input.textChanged.connect(self._persist_playlist_settings); form.addWidget(self.playlist_url_input, 0, 1, 1, 3)
        self.channel_url_input = QLineEdit(); self.channel_url_input.setText(FIXED_CHANNEL_URL); self.channel_url_input.setReadOnly(True); self.channel_url_input.hide()
        form.addWidget(QLabel("출력 형식"), 1, 0)
        self.output_mode_combo = QComboBox(); self.output_mode_combo.addItem("영상", "video"); self.output_mode_combo.addItem("Premiere XML", "premiere_xml"); self.output_mode_combo.currentIndexChanged.connect(self.on_output_mode_changed); form.addWidget(self.output_mode_combo, 1, 1)
        form.addWidget(QLabel("번역 모델"), 1, 2)
        self.model_combo = QComboBox(); [self.model_combo.addItem(label, model_id) for model_id, label in OPENAI_MODELS.items()]; self.model_combo.currentIndexChanged.connect(self.on_model_changed); form.addWidget(self.model_combo, 1, 3)
        self.api_key_status = QLabel(""); self.api_key_status.setObjectName("hint"); self.api_key_status.setWordWrap(True); form.addWidget(self.api_key_status, 2, 0, 1, 4)
        self.channel_cache_status = QLabel(""); self.channel_cache_status.setObjectName("hint"); self.channel_cache_status.setWordWrap(True); self.channel_cache_status.hide()
        layout.addLayout(form)
        row = QHBoxLayout()
        self.analyze_button = QPushButton("플레이리스트 분석"); self.analyze_button.clicked.connect(self.start_playlist_import); row.addWidget(self.analyze_button)
        self.channel_sync_button = QPushButton("채널 영상 동기화"); self.channel_sync_button.clicked.connect(self.start_channel_sync); self.channel_sync_button.hide()
        self.start_queue_button = QPushButton("체크한 곡 렌더 시작"); self.start_queue_button.clicked.connect(self.start_batch_processing); row.addWidget(self.start_queue_button)
        self.clear_button = QPushButton("분석 결과 비우기"); self.clear_button.clicked.connect(self.clear_analysis); row.addWidget(self.clear_button)
        self.clean_button = QPushButton("임시 파일 정리"); self.clean_button.clicked.connect(self.clean_temp_files); row.addWidget(self.clean_button)
        layout.addLayout(row); return frame

    def _build_summary_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QGridLayout(frame)
        layout.addWidget(QLabel("현재 플레이리스트"), 0, 0)
        self.playlist_title_value = QLabel("아직 분석한 플레이리스트가 없습니다."); layout.addWidget(self.playlist_title_value, 0, 1, 1, 6)
        self.total_count_value = QLabel("전체: 0"); layout.addWidget(self.total_count_value, 1, 0)
        self.included_count_value = QLabel("렌더 준비: 0"); layout.addWidget(self.included_count_value, 1, 1)
        self.selected_count_value = QLabel("체크 렌더: 0"); layout.addWidget(self.selected_count_value, 1, 2)
        self.missing_lyrics_count_value = QLabel("가사 없음: 0"); layout.addWidget(self.missing_lyrics_count_value, 1, 3)
        self.review_needed_count_value = QLabel("싱크 필요: 0"); layout.addWidget(self.review_needed_count_value, 1, 4)
        self.duplicate_count_value = QLabel("중복 제외: 0"); layout.addWidget(self.duplicate_count_value, 1, 5)
        self.ready_status_value = QLabel("대기 중"); layout.addWidget(self.ready_status_value, 2, 0, 1, 7); return frame

    def _build_track_list_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("트랙 목록"); title.setObjectName("subtitle"); layout.addWidget(title)
        hint = QLabel("체크된 곡만 배치 렌더에 들어갑니다. 자동 정보가 이상하면 곡 검토에서 고친 뒤 다시 렌더할 수 있습니다."); hint.setObjectName("hint"); hint.setWordWrap(True); layout.addWidget(hint)
        selection_row = QHBoxLayout()
        self.select_all_tracks_button = QPushButton("전체 체크"); self.select_all_tracks_button.clicked.connect(lambda: self._set_batch_selection("all")); selection_row.addWidget(self.select_all_tracks_button)
        self.select_ready_tracks_button = QPushButton("준비 곡만 체크"); self.select_ready_tracks_button.clicked.connect(lambda: self._set_batch_selection("ready")); selection_row.addWidget(self.select_ready_tracks_button)
        self.clear_track_selection_button = QPushButton("전체 해제"); self.clear_track_selection_button.clicked.connect(lambda: self._set_batch_selection("none")); selection_row.addWidget(self.clear_track_selection_button)
        layout.addLayout(selection_row)
        self.track_list = QListWidget(); self.track_list.currentRowChanged.connect(self.handle_track_selection_changed); self.track_list.itemChanged.connect(self.handle_track_item_changed); layout.addWidget(self.track_list, stretch=1); return frame

    def _build_detail_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("선택한 곡"); title.setObjectName("subtitle"); layout.addWidget(title)
        self.selected_track_source_label = QLabel("곡을 선택하면 상세 정보가 보입니다."); self.selected_track_source_label.setWordWrap(True); layout.addWidget(self.selected_track_source_label)
        self.selected_track_status_label = QLabel("상태: -"); layout.addWidget(self.selected_track_status_label)
        self.selected_track_note_label = QLabel("메모: -"); self.selected_track_note_label.setObjectName("hint"); self.selected_track_note_label.setWordWrap(True); layout.addWidget(self.selected_track_note_label)
        self.selected_track_meta_label = QLabel("기본 정보: -"); self.selected_track_meta_label.setWordWrap(True); layout.addWidget(self.selected_track_meta_label)
        self.selected_track_media_label = QLabel("미디어 정보: -"); self.selected_track_media_label.setWordWrap(True); layout.addWidget(self.selected_track_media_label)
        self.selected_track_lyrics_label = QLabel("가사 상태: -"); self.selected_track_lyrics_label.setWordWrap(True); layout.addWidget(self.selected_track_lyrics_label)
        self.selected_track_batch_label = QLabel("배치 선택: -"); self.selected_track_batch_label.setWordWrap(True); layout.addWidget(self.selected_track_batch_label)
        summary_hint = QLabel("세부 수정과 수동 싱크 매핑은 팝업에서 진행합니다. 저장 뒤 바로 한 곡만 다시 렌더할 수도 있습니다."); summary_hint.setObjectName("hint"); summary_hint.setWordWrap(True); layout.addWidget(summary_hint)
        self.selected_track_preview = QTextEdit(); self.selected_track_preview.setReadOnly(True); self.selected_track_preview.setPlaceholderText("가사 미리보기"); layout.addWidget(self.selected_track_preview, stretch=1)
        row = QHBoxLayout()
        self.review_popup_button = QPushButton("곡 정보/가사 수정"); self.review_popup_button.clicked.connect(self.open_selected_track_review); row.addWidget(self.review_popup_button)
        self.render_selected_button = QPushButton("선택 곡 바로 렌더"); self.render_selected_button.clicked.connect(self.start_selected_track_processing); row.addWidget(self.render_selected_button)
        self.clear_lrc_button = QPushButton("가사 초기화"); self.clear_lrc_button.clicked.connect(self.clear_selected_track_lyrics); row.addWidget(self.clear_lrc_button)
        layout.addLayout(row); return frame

    def _build_progress_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("진행 상황"); title.setObjectName("subtitle"); layout.addWidget(title)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100); layout.addWidget(self.progress_bar)
        self.progress_log = QTextEdit(); self.progress_log.setReadOnly(True); layout.addWidget(self.progress_log, stretch=1); return frame

    def _load_saved_state(self) -> None:
        self.playlist_url_input.setText(self.config_manager.get("last_playlist_url", ""))
        self.channel_url_input.setText(FIXED_CHANNEL_URL)
        index = self.output_mode_combo.findData(self.output_mode)
        if index >= 0: self.output_mode_combo.setCurrentIndex(index)
        model_id = resolve_model(self.config_manager.get_translation_model())
        index = self.model_combo.findData(model_id)
        if index >= 0: self.model_combo.setCurrentIndex(index)
        self.api_key_status.setText("OPENAI_API_KEY가 설정되어 있습니다." if os.getenv("OPENAI_API_KEY") else "배치를 실행하기 전에 OPENAI_API_KEY를 설정하세요.")
        self._refresh_channel_cache_status()

    def _persist_playlist_settings(self) -> None:
        self.config_manager.set("last_playlist_url", self.playlist_url_input.text().strip())
        self.config_manager.set("last_channel_url", FIXED_CHANNEL_URL)
        self._refresh_channel_cache_status()

    def append_progress_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_log.append(f"[{timestamp}] {message}")
        bar = self.progress_log.verticalScrollBar(); bar.setValue(bar.maximum()); self.last_progress_message = message

    def update_progress_ui(self, message: str, value: int) -> None:
        self.progress_bar.setValue(max(0, min(100, value)))
        if message and message != self.last_progress_message: self.append_progress_message(message)

    def _is_busy(self) -> bool:
        return any(thread is not None for thread in (self.worker, self.import_worker, self.channel_worker))

    @staticmethod
    def _is_thread_running(thread: Optional[QThread]) -> bool:
        return thread is not None and thread.isRunning()

    def _resolve_active_channel_index(self) -> Optional[ChannelVideoIndex]:
        cached_index = load_channel_video_index(FIXED_CHANNEL_URL)
        if cached_index is not None:
            self.channel_index = cached_index
            return cached_index
        return None

    def _refresh_channel_cache_status(self) -> None:
        if not hasattr(self, "channel_cache_status"):
            return
        cached_index = self.channel_index or load_channel_video_index()
        if cached_index is not None:
            self.channel_index = cached_index
        if cached_index is None:
            self.channel_cache_status.setText("고정 채널 캐시 없음 · 분석 시작 시 자동 동기화 후 중복 제외를 진행합니다.")
            return

        fetched_at = cached_index.fetched_at
        try:
            fetched_at = datetime.fromisoformat(cached_index.fetched_at).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

        base_text = f"채널 캐시: {cached_index.channel_title} · 영상 {len(cached_index.videos)}개 · 동기화 {fetched_at}"
        if normalize_channel_url(cached_index.channel_url) == normalize_channel_url(FIXED_CHANNEL_URL):
            self.channel_cache_status.setText(base_text)
            return
        self.channel_cache_status.setText(f"{base_text} · 고정 채널과 달라 자동 중복 제외에는 사용하지 않습니다.")

    def start_channel_sync(self) -> None:
        if self._is_busy():
            return
        self._persist_playlist_settings()
        self.progress_log.clear()
        self.progress_bar.setRange(0, 0)
        self.ready_status_value.setText("채널 영상 목록 동기화 중...")
        self.append_progress_message("채널 영상 동기화를 시작합니다...")
        self.set_processing_state(True)
        self.channel_worker = ChannelIndexWorker(FIXED_CHANNEL_URL)
        self.channel_worker.progress.connect(self.append_progress_message)
        self.channel_worker.result_ready.connect(self.on_channel_sync_finished)
        self.channel_worker.error_occurred.connect(self.on_channel_sync_error)
        self.channel_worker.finished.connect(self._finalize_channel_sync)
        self.channel_worker.finished.connect(self.channel_worker.deleteLater)
        self.channel_worker.start()

    def on_channel_sync_finished(self, channel_index: ChannelVideoIndex) -> None:
        self.channel_index = channel_index
        self._refresh_channel_cache_status()
        self.ready_status_value.setText("채널 캐시 준비 완료")
        self.append_progress_message(f"채널 영상 캐시를 저장했습니다: {channel_index.channel_title} / {len(channel_index.videos)}개")
        if self.pending_import_url:
            self._start_pending_playlist_import()

    def on_channel_sync_error(self, error_message: str) -> None:
        self.ready_status_value.setText("채널 동기화 실패")
        self.append_progress_message(f"채널 동기화 실패: {error_message}")
        if self.pending_import_url:
            fallback_index = self._resolve_active_channel_index()
            if fallback_index is not None:
                self.append_progress_message("기존 채널 캐시로 중복 비교를 계속 진행합니다.")
                self._start_pending_playlist_import()
                return
            self.pending_import_url = None
            self.pending_import_output_mode = None
            self.playlist_title_value.setText("플레이리스트 분석 실패")
            self.append_progress_message("채널 캐시가 없어 분석을 계속할 수 없습니다.")
        QMessageBox.critical(self, "채널 동기화 실패", error_message)

    def _finalize_channel_sync(self) -> None:
        self.channel_worker = None
        if self.import_worker is None and self.worker is None:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.set_processing_state(False)
        self._refresh_channel_cache_status()
        self._refresh_pipeline_state()

    def start_playlist_import(self) -> None:
        if self._is_busy(): return
        url = self.playlist_url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "플레이리스트 URL 없음", "먼저 플레이리스트 URL을 입력하세요."); return
        if "list=" not in url:
            QMessageBox.warning(self, "잘못된 플레이리스트 URL", "list 파라미터가 포함된 플레이리스트 URL을 입력하세요."); return
        if self.review_tracks:
            reply = QMessageBox.question(self, "현재 분석 결과 교체", "새 플레이리스트를 분석하면서 현재 목록을 덮어쓸까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes: return
        self._persist_playlist_settings(); self._reset_analysis_views(); self.progress_log.clear(); self.progress_bar.setRange(0, 0)
        self.playlist_title_value.setText("플레이리스트 분석 중..."); self.ready_status_value.setText("채널 동기화 후 트랙을 불러오는 중..."); self.append_progress_message("플레이리스트 분석을 시작합니다..."); self.set_processing_state(True)
        self.pending_import_url = url
        self.pending_import_output_mode = self.output_mode_combo.currentData()
        self.append_progress_message("고정 채널 영상 목록을 최신 상태로 동기화합니다...")
        self.channel_worker = ChannelIndexWorker(FIXED_CHANNEL_URL)
        self.channel_worker.progress.connect(self.append_progress_message)
        self.channel_worker.result_ready.connect(self.on_channel_sync_finished)
        self.channel_worker.error_occurred.connect(self.on_channel_sync_error)
        self.channel_worker.finished.connect(self._finalize_channel_sync)
        self.channel_worker.finished.connect(self.channel_worker.deleteLater)
        self.channel_worker.start()

    def _start_pending_playlist_import(self) -> None:
        url = self.pending_import_url
        output_mode = self.pending_import_output_mode
        self.pending_import_url = None
        self.pending_import_output_mode = None
        if not url or not output_mode:
            return
        channel_index = self._resolve_active_channel_index()
        if channel_index is None:
            self.on_playlist_import_error("고정 채널 캐시를 불러오지 못해 중복 비교를 진행할 수 없습니다.")
            return
        self.import_worker = PlaylistImportWorker(url, output_mode, channel_index=channel_index)
        self.import_worker.progress.connect(self.append_progress_message)
        self.import_worker.report_ready.connect(self.on_playlist_import_finished)
        self.import_worker.error_occurred.connect(self.on_playlist_import_error)
        self.import_worker.finished.connect(self._finalize_playlist_import)
        self.import_worker.finished.connect(self.import_worker.deleteLater)
        self.import_worker.start()

    def on_playlist_import_finished(self, report: PlaylistImportReport) -> None:
        self.current_playlist_title = report.playlist_title
        self.review_tracks = report.tracks
        self.last_duplicate_skip_count = report.duplicate_skip_count
        self.playlist_title_value.setText(report.playlist_title)
        self._refresh_track_list()
        if self.review_tracks: self.track_list.setCurrentRow(0)
        else: self._clear_track_detail_inputs()
        self._refresh_pipeline_state()
        summary = f"플레이리스트 분석 완료. 원본 {report.source_track_count}곡 중 {len(self.review_tracks)}곡을 불러왔습니다."
        if report.duplicate_skip_count:
            summary = f"{summary} 채널 중복 {report.duplicate_skip_count}곡은 제외했습니다."
        self.append_progress_message(summary)

    def on_playlist_import_error(self, error_message: str) -> None:
        self.playlist_title_value.setText("플레이리스트 분석 실패"); self.ready_status_value.setText("불러오기 실패")
        self.append_progress_message(f"플레이리스트 분석 실패: {error_message}")
        QMessageBox.critical(self, "플레이리스트 분석 실패", error_message)

    def handle_track_selection_changed(self, row: int) -> None:
        self.on_track_selected(row)

    def handle_track_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating_track_list:
            return
        row = self.track_list.row(item)
        if row < 0 or row >= len(self.review_tracks):
            return
        self.review_tracks[row].include_in_batch = item.checkState() == Qt.CheckState.Checked
        if row == self.current_track_index:
            self.on_track_selected(row)
        self._refresh_pipeline_state()

    def on_track_selected(self, row: int) -> None:
        self.current_track_index = row
        if row < 0 or row >= len(self.review_tracks):
            self._clear_track_detail_inputs(); return
        track = self.review_tracks[row]
        self._loading_track_details = True
        self.selected_track_source_label.setText(f"원본 {track.source.index}번: {track.source.label}")
        self.selected_track_status_label.setText(f"상태: {self._status_text(track)}")
        self.selected_track_note_label.setText(f"메모: {track.note or '-'}")
        self.selected_track_meta_label.setText(
            f"기본 정보: {track.title or '제목 없음'} / {track.artist or '아티스트 없음'} / {track.album or '앨범 없음'}"
        )
        self.selected_track_media_label.setText(
            f"미디어 정보: 길이 {self._format_duration(track.duration)} | YouTube {'있음' if track.youtube_url.strip() else '없음'} | 가사 파일 {track.lrc_path or '없음'}"
        )
        lyrics_lines = [line for line in track.lyrics_text.splitlines() if line.strip()]
        self.selected_track_lyrics_label.setText(
            f"가사 상태: {self._lyrics_mode_text(track.lyrics_mode)} | 렌더 조건 {'충족' if track.can_render() else '미충족'} | 줄 수 {len(lyrics_lines)}"
        )
        self.selected_track_batch_label.setText(
            f"배치 선택: {'체크됨' if track.include_in_batch else '해제됨'} | 바로 렌더 {'가능' if track.can_render() else '불가'}"
        )
        preview_lines = lyrics_lines[:18]
        if len(lyrics_lines) > 18:
            preview_lines += ["", "..."]
        self.selected_track_preview.setPlainText("\n".join(preview_lines))
        self._loading_track_details = False
        self._refresh_detail_buttons()

    def open_selected_track_review(self) -> None:
        track = self._get_selected_track()
        if track is None:
            return
        row = self.current_track_index
        dialog = TrackReviewDialog(track, self)
        dialog.track_preview_updated.connect(lambda updated_track, row=row: self._apply_reviewed_track(row, updated_track, add_log=False))
        if not dialog.exec():
            return
        self._apply_reviewed_track(row, dialog.get_review_track(), add_log=True)

    def clear_selected_track_lyrics(self) -> None:
        track = self._get_selected_track()
        if track is None:
            return
        track.lrc_path = None
        track.lyrics_text = ""
        track.lyrics_mode = "missing"
        track.lyrics_source = "none"
        self._update_track_status(track, auto_include=False)
        self._refresh_track_list(preserve_selection=True)
        self.on_track_selected(self.current_track_index)
        self._refresh_pipeline_state()
        self.append_progress_message(f"가사 초기화: {track.label}")

    def start_selected_track_processing(self) -> None:
        if self._is_busy():
            return
        track = self._get_selected_track()
        if track is None:
            return
        if not track.can_render():
            QMessageBox.warning(self, "렌더 불가", "선택한 곡은 아직 렌더할 준비가 되지 않았습니다. 메타 정보나 가사를 먼저 확인해 주세요.")
            return
        self.processing_mode = "single"
        self.current_queue_batch_name = None
        self.render_queue = []
        self.append_progress_message(f"선택 곡 렌더를 시작합니다: {track.label}")
        self.ready_status_value.setText("선택 곡 렌더링 중...")
        self._start_worker(track.to_process_config(self.output_mode))

    def start_batch_processing(self) -> None:
        if self._is_busy(): return
        self._save_current_track_silently()
        selected_tracks = [track for track in self.review_tracks if track.include_in_batch]
        self.render_queue = [track for track in selected_tracks if track.can_render()]
        skipped_tracks = [track for track in selected_tracks if not track.can_render()]
        if not self.render_queue:
            QMessageBox.warning(self, "렌더 대상 없음", "체크된 곡 중에서 가사와 기본 정보가 준비된 곡이 없습니다."); return
        self.processing_mode = "queue"; self.current_queue_index = 0; self.current_queue_batch_name = self._build_queue_batch_name()
        self.progress_log.clear(); self.append_progress_message(f"{len(self.render_queue)}곡 배치 렌더를 시작합니다. 배치 폴더: {self.current_queue_batch_name}")
        if skipped_tracks:
            self.append_progress_message(f"체크했지만 아직 준비되지 않은 {len(skipped_tracks)}곡은 이번 배치에서 건너뜁니다.")
        self.ready_status_value.setText("큐 렌더링 중..."); self._start_next_queue_item()

    def _start_next_queue_item(self) -> None:
        if self.current_queue_index >= len(self.render_queue):
            self.processing_mode = None; self.current_queue_batch_name = None; self.render_queue = []; self.set_processing_state(False)
            self.ready_status_value.setText("배치 렌더 완료"); self.append_progress_message("배치 렌더가 완료되었습니다."); QMessageBox.information(self, "큐 완료", "배치 렌더가 모두 끝났습니다."); return
        track = self.render_queue[self.current_queue_index]
        self.track_list.setCurrentRow(self.review_tracks.index(track)); self.append_progress_message(f"렌더링 {self.current_queue_index + 1}/{len(self.render_queue)}: {track.label}")
        config = track.to_process_config(self.output_mode); config.batch_name = self.current_queue_batch_name; self._start_worker(config)

    def _start_worker(self, config: ProcessConfig) -> None:
        validation_error = ProcessManager(lambda *_: None).validate_config(config)
        if validation_error:
            self.processing_mode = None; self.current_queue_batch_name = None; self.render_queue = []; self.ready_status_value.setText("설정 검증 실패")
            QMessageBox.warning(self, "잘못된 설정", validation_error); return
        self.set_processing_state(True); self.progress_bar.setRange(0, 100); self.progress_bar.setValue(0); self._reset_worker_state()
        self.worker = WorkerThread(config)
        self.worker.progress.connect(self.update_progress_ui); self.worker.result_ready.connect(self._capture_worker_result); self.worker.error_occurred.connect(self._capture_worker_error)
        self.worker.finished.connect(self._finalize_worker); self.worker.finished.connect(self.worker.deleteLater); self.worker.start()

    def on_process_finished(self, output_path: str) -> None:
        self.append_progress_message(f"완료: {output_path}")
        if self.processing_mode == "queue":
            self.current_queue_index += 1; self._start_next_queue_item(); return
        if self.processing_mode == "single":
            self.processing_mode = None; self.set_processing_state(False); self._refresh_pipeline_state(); self.ready_status_value.setText("선택 곡 렌더 완료"); return
        self.processing_mode = None; self.set_processing_state(False); self.ready_status_value.setText("대기 중")

    def on_process_error(self, error_message: str) -> None:
        self.append_progress_message(f"오류: {error_message}")
        if self.processing_mode == "queue":
            reply = QMessageBox.question(self, "큐 작업 실패", f"{error_message}\n\n다음 큐 작업을 계속 진행할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.current_queue_index += 1; self._start_next_queue_item(); return
        self.processing_mode = None; self.current_queue_batch_name = None; self.render_queue = []; self.set_processing_state(False)
        self._refresh_pipeline_state(); self.ready_status_value.setText("렌더 중단"); QMessageBox.critical(self, "처리 실패", error_message)

    def clear_analysis(self) -> None:
        if self._is_busy() or (not self.review_tracks and not self.current_playlist_title and not self.last_duplicate_skip_count): return
        reply = QMessageBox.question(self, "분석 결과 비우기", "현재 목록과 수정 내용을 화면에서 제거할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        self._reset_analysis_views(); self.ready_status_value.setText("대기 중"); self.append_progress_message("현재 분석 결과를 비웠습니다.")

    def clean_temp_files(self) -> None:
        reply = QMessageBox.question(self, "임시 파일 정리", "data/temp 파일과 번역 캐시를 삭제할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        deleted = 0
        if os.path.isdir(TEMP_DIR):
            for entry in os.listdir(TEMP_DIR):
                path = os.path.join(TEMP_DIR, entry)
                try:
                    if os.path.isdir(path): shutil.rmtree(path)
                    elif os.path.isfile(path): os.remove(path)
                    deleted += 1
                except OSError as exc:
                    self.append_progress_message(f"삭제 실패: {path} ({exc})")
        if os.path.exists(TRANSLATION_CACHE_PATH):
            try: os.remove(TRANSLATION_CACHE_PATH)
            except OSError as exc: self.append_progress_message(f"캐시 삭제 실패: {exc}")
        self.append_progress_message(f"임시 파일 {deleted}개를 정리했습니다."); QMessageBox.information(self, "정리 완료", "임시 파일 정리가 끝났습니다.")
    def on_model_changed(self, index: int) -> None:
        model_id = self.model_combo.itemData(index)
        if model_id: self.config_manager.set_translation_model(resolve_model(model_id))

    def on_output_mode_changed(self, index: int) -> None:
        output_mode = self.output_mode_combo.itemData(index)
        if output_mode: self.output_mode = output_mode; self.config_manager.set("output_mode", output_mode)

    def set_processing_state(self, processing: bool) -> None:
        controls = [
            self.playlist_url_input,
            self.channel_url_input,
            self.output_mode_combo,
            self.model_combo,
            self.analyze_button,
            self.channel_sync_button,
            self.start_queue_button,
            self.clear_button,
            self.clean_button,
            self.track_list,
            self.select_all_tracks_button,
            self.select_ready_tracks_button,
            self.clear_track_selection_button,
            self.review_popup_button,
            self.render_selected_button,
            self.clear_lrc_button,
        ]
        for control in controls:
            control.setDisabled(processing)

    def _refresh_pipeline_state(self) -> None:
        total = len(self.review_tracks)
        included = sum(1 for track in self.review_tracks if track.can_render())
        selected_ready = sum(1 for track in self.review_tracks if track.include_in_batch and track.can_render())
        selected_total = sum(1 for track in self.review_tracks if track.include_in_batch)
        missing = sum(1 for track in self.review_tracks if track.status == "missing_lyrics")
        plain_needed = sum(1 for track in self.review_tracks if track.status == "plain_lyrics")
        self.total_count_value.setText(f"전체: {total}"); self.included_count_value.setText(f"렌더 준비: {included}")
        self.selected_count_value.setText(f"체크 렌더: {selected_ready}/{selected_total}")
        self.missing_lyrics_count_value.setText(f"가사 없음: {missing}"); self.review_needed_count_value.setText(f"싱크 필요: {plain_needed}")
        self.duplicate_count_value.setText(f"중복 제외: {self.last_duplicate_skip_count}")
        if not self._is_busy():
            if selected_ready: self.ready_status_value.setText(f"렌더 시작 가능 · 체크된 준비 곡 {selected_ready}곡")
            elif included: self.ready_status_value.setText("렌더할 곡을 체크하세요")
            elif plain_needed: self.ready_status_value.setText("일반 가사만 있는 곡은 싱크 작업 후 렌더할 수 있습니다")
            elif self.current_playlist_title and self.last_duplicate_skip_count and not total: self.ready_status_value.setText("플레이리스트 곡이 모두 채널 중복으로 제외되었습니다")
            elif self.current_playlist_title: self.ready_status_value.setText("가사와 기본 정보가 준비되면 자동으로 렌더 대상이 됩니다")
            else: self.ready_status_value.setText("대기 중")
        has_targets = selected_ready > 0
        self.start_queue_button.setEnabled(has_targets and not self._is_busy())
        self.clear_button.setEnabled((bool(self.review_tracks) or bool(self.current_playlist_title) or bool(self.last_duplicate_skip_count)) and not self._is_busy())
        self._refresh_detail_buttons()

    def _refresh_track_list(self, preserve_selection: bool = False) -> None:
        selected_row = self.track_list.currentRow() if preserve_selection else -1
        self._updating_track_list = True
        self.track_list.blockSignals(True)
        try:
            self.track_list.clear()
            for track in self.review_tracks:
                item = QListWidgetItem(self._format_track_list_text(track))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if track.include_in_batch else Qt.CheckState.Unchecked)
                self.track_list.addItem(item)
            if preserve_selection and 0 <= selected_row < len(self.review_tracks): self.track_list.setCurrentRow(selected_row)
        finally:
            self.track_list.blockSignals(False)
            self._updating_track_list = False

    def _refresh_detail_buttons(self) -> None:
        track = self._get_selected_track(); has_track = track is not None
        self.review_popup_button.setEnabled(has_track and not self._is_busy())
        self.select_all_tracks_button.setEnabled(bool(self.review_tracks) and not self._is_busy())
        self.select_ready_tracks_button.setEnabled(bool(self.review_tracks) and not self._is_busy())
        self.clear_track_selection_button.setEnabled(bool(self.review_tracks) and not self._is_busy())
        if not has_track:
            self.render_selected_button.setEnabled(False); self.clear_lrc_button.setEnabled(False); return
        self.render_selected_button.setEnabled(track.can_render() and not self._is_busy())
        self.clear_lrc_button.setEnabled((bool(track.lrc_path) or bool(track.lyrics_text.strip())) and not self._is_busy())

    def _reset_analysis_views(self) -> None:
        self.review_tracks.clear(); self.render_queue = []; self.current_track_index = -1; self.current_playlist_title = ""; self.last_duplicate_skip_count = 0; self.track_list.clear()
        self.playlist_title_value.setText("아직 분석한 플레이리스트가 없습니다."); self._clear_track_detail_inputs(); self._refresh_pipeline_state()

    def _clear_track_detail_inputs(self) -> None:
        self.selected_track_source_label.setText("곡을 선택하면 상세 정보가 보입니다."); self.selected_track_status_label.setText("상태: -"); self.selected_track_note_label.setText("메모: -")
        self.selected_track_meta_label.setText("기본 정보: -")
        self.selected_track_media_label.setText("미디어 정보: -")
        self.selected_track_lyrics_label.setText("가사 상태: -")
        self.selected_track_batch_label.setText("배치 선택: -")
        self.selected_track_preview.clear()
        self._refresh_detail_buttons()

    def _get_selected_track(self) -> Optional[PlaylistReviewTrack]:
        return self.review_tracks[self.current_track_index] if 0 <= self.current_track_index < len(self.review_tracks) else None

    def _update_track_status(self, track: PlaylistReviewTrack, *, auto_include: bool) -> None:
        if not track.title.strip() or not track.artist.strip():
            track.status = "incomplete"; track.note = "제목과 아티스트를 입력해야 합니다."; track.include_in_batch = False; return
        if not track.youtube_url.strip():
            track.status = "incomplete"; track.note = "YouTube URL이 없습니다."; track.include_in_batch = False; return
        if not track.lrc_path or not os.path.exists(track.lrc_path):
            track.status = "missing_lyrics"; track.note = "가사가 없습니다. 검토 팝업에서 입력하거나 자동으로 찾으면 바로 저장됩니다."; track.include_in_batch = False; track.lyrics_mode = "missing"; return
        if track.lyrics_mode != "synced":
            track.status = "plain_lyrics"
            if track.lyrics_source == "manual_sync":
                track.note = "수동 작업 중이지만 아직 타임스탬프가 없는 일반 가사입니다."
            elif track.lyrics_source == "auto_fetch":
                track.note = "자동으로 가져온 일반 가사입니다. 싱크 작업 전에는 렌더하지 않습니다."
            else:
                track.note = "일반 가사만 준비되어 있습니다. 싱크 작업 전에는 렌더하지 않습니다."
            track.include_in_batch = False
            return
        if track.lyrics_source == "manual_sync":
            track.note = "팝업에서 직접 싱크 매핑한 가사입니다."
        elif track.lyrics_source == "auto_fetch":
            track.note = "추가 소스에서 자동으로 가져온 가사입니다."
        else:
            track.note = "싱크 가사가 준비되었습니다." if track.lyrics_mode == "synced" else "일반 가사가 준비되었습니다."
        track.status = "ready"
        if auto_include:
            track.include_in_batch = True

    def _status_text(self, track: PlaylistReviewTrack) -> str:
        if track.status == "ready": return "렌더 준비됨"
        if track.status == "plain_lyrics": return "싱크 필요"
        if track.status == "missing_lyrics": return "가사 없음"
        if track.status == "incomplete": return "필수 정보 부족"
        if track.status == "error": return "자동 해석 실패"
        return track.status

    def _format_track_list_text(self, track: PlaylistReviewTrack) -> str:
        prefix = "[준비]" if track.status == "ready" else "[싱크 필요]" if track.status == "plain_lyrics" else "[가사 없음]" if track.status == "missing_lyrics" else "[정보 부족]" if track.status == "incomplete" else "[확인 필요]"
        lyrics_text = self._lyrics_mode_text(track.lyrics_mode)
        return f"{prefix} {track.source.index:02d}. {track.label}\n가사: {lyrics_text}"

    @staticmethod
    def _lyrics_mode_text(lyrics_mode: str) -> str:
        return {
            "synced": "싱크 있음",
            "plain": "일반 가사",
            "missing": "없음",
        }.get(lyrics_mode, lyrics_mode)

    def _format_duration(self, duration: Optional[int]) -> str:
        return "없음" if not isinstance(duration, int) else f"{duration // 60:02d}:{duration % 60:02d}"

    def _save_current_track_silently(self) -> None:
        return

    def _set_batch_selection(self, mode: str) -> None:
        if not self.review_tracks:
            return
        for track in self.review_tracks:
            if mode == "all":
                track.include_in_batch = True
            elif mode == "ready":
                track.include_in_batch = track.can_render()
            else:
                track.include_in_batch = False
        self._refresh_track_list(preserve_selection=True)
        if self.current_track_index >= 0:
            self.on_track_selected(self.current_track_index)
        self._refresh_pipeline_state()

    def _apply_reviewed_track(self, row: int, updated_track: PlaylistReviewTrack, *, add_log: bool) -> None:
        if row < 0 or row >= len(self.review_tracks):
            return
        previous_track = self.review_tracks[row]
        previously_renderable = previous_track.can_render()
        previous_selection = previous_track.include_in_batch
        track = updated_track
        track.youtube_url = sanitize_youtube_url(track.youtube_url.strip()) or track.youtube_url.strip()
        if track.lyrics_text.strip():
            track.lrc_path, track.lyrics_text = save_review_track_lyrics(
                artist=track.artist or track.source.artist,
                title=track.title or track.source.title,
                lyrics_text=track.lyrics_text,
                existing_path=track.lrc_path,
            )
            track.lyrics_mode = "synced" if lyrics_are_synced(track.lyrics_text) else "plain"
            if track.lyrics_source not in {"manual_sync", "auto_fetch"}:
                track.lyrics_source = "generated"
        else:
            track.lrc_path = None
            track.lyrics_text = ""
            track.lyrics_mode = "missing"
            track.lyrics_source = "none"

        self._update_track_status(track, auto_include=not previously_renderable)
        if previously_renderable and track.can_render():
            track.include_in_batch = previous_selection
        self.review_tracks[row] = track
        self._refresh_track_list(preserve_selection=True)
        self.on_track_selected(row)
        self._refresh_pipeline_state()
        if add_log:
            self.append_progress_message(f"저장 완료: {track.label}")

    def _capture_worker_result(self, output_path: str) -> None:
        self.worker_result_path = output_path

    def _capture_worker_error(self, error_message: str) -> None:
        self.worker_error_message = error_message

    def _finalize_worker(self) -> None:
        self.worker = None
        if self.worker_error_message:
            error_message = self.worker_error_message; self._reset_worker_state(); self.on_process_error(error_message); return
        if self.worker_result_path:
            output_path = self.worker_result_path; self._reset_worker_state(); self.on_process_finished(output_path); return
        self._reset_worker_state()
        if self.processing_mode is not None: self.on_process_error("작업 스레드가 결과 없이 종료되었습니다.")

    def _finalize_playlist_import(self) -> None:
        self.import_worker = None; self.progress_bar.setRange(0, 100); self.progress_bar.setValue(0); self.set_processing_state(False); self._refresh_pipeline_state()

    def _reset_worker_state(self) -> None:
        self.worker_result_path = None; self.worker_error_message = None

    @staticmethod
    def _build_queue_batch_name() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f__queue")

    def closeEvent(self, event) -> None:
        if self._is_thread_running(self.worker) or self._is_thread_running(self.import_worker) or self._is_thread_running(self.channel_worker):
            QMessageBox.warning(self, "작업 진행 중", "현재 작업이 끝난 뒤에 창을 닫아 주세요."); event.ignore(); return
        super().closeEvent(event)


MainWindow = PlaylistPipelineWindow
