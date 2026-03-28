"""Main window for playlist review and batch rendering."""

from __future__ import annotations

import os
import shutil
from copy import deepcopy
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSplitter, QTextEdit, QVBoxLayout, QWidget

from app.config.config_manager import get_config
from app.config.paths import TEMP_DIR, TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import OPENAI_MODELS, resolve_model
from app.lyrics.lyric_text_utils import prepare_lyric_text_for_subtitles
from app.pipeline.playlist_importer import PlaylistImportReport, PlaylistReviewTrack, import_playlist, load_review_track_lyrics_file, save_review_track_lyrics
from app.pipeline.process_manager import OutputMode, ProcessConfig, ProcessManager
from app.sources.genie_handler import lyrics_are_synced
from app.sources.youtube_handler import sanitize_youtube_url
from app.ui.styles import MODERN_STYLESHEET


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

    def __init__(self, playlist_url: str, output_mode: str):
        super().__init__()
        self.playlist_url = playlist_url
        self.output_mode = output_mode

    def run(self) -> None:
        try:
            report = import_playlist(self.playlist_url, output_mode=self.output_mode, progress_callback=self.progress.emit)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return
        self.report_ready.emit(report)


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
        self.worker_result_path: Optional[str] = None
        self.worker_error_message: Optional[str] = None
        self.processing_mode: Optional[str] = None
        self.last_progress_message = ""
        self._loading_track_details = False
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
        note = QLabel("플레이리스트를 먼저 전부 리스트업하고, 곡별로 상태를 검토한 뒤 수정해서 배치 렌더를 실행합니다."); note.setWordWrap(True); layout.addWidget(note)
        return frame

    def _build_setup_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        header = QLabel("분석 설정"); header.setObjectName("subtitle"); layout.addWidget(header)
        form = QGridLayout()
        form.addWidget(QLabel("플레이리스트 URL"), 0, 0)
        self.playlist_url_input = QLineEdit(); self.playlist_url_input.setPlaceholderText("https://music.youtube.com/playlist?list=... 또는 https://www.youtube.com/playlist?list=..."); self.playlist_url_input.textChanged.connect(self._persist_playlist_settings); form.addWidget(self.playlist_url_input, 0, 1, 1, 3)
        form.addWidget(QLabel("출력 형식"), 1, 0)
        self.output_mode_combo = QComboBox(); self.output_mode_combo.addItem("영상", "video"); self.output_mode_combo.addItem("Premiere XML", "premiere_xml"); self.output_mode_combo.currentIndexChanged.connect(self.on_output_mode_changed); form.addWidget(self.output_mode_combo, 1, 1)
        form.addWidget(QLabel("번역 모델"), 1, 2)
        self.model_combo = QComboBox(); [self.model_combo.addItem(label, model_id) for model_id, label in OPENAI_MODELS.items()]; self.model_combo.currentIndexChanged.connect(self.on_model_changed); form.addWidget(self.model_combo, 1, 3)
        self.api_key_status = QLabel(""); self.api_key_status.setObjectName("hint"); self.api_key_status.setWordWrap(True); form.addWidget(self.api_key_status, 2, 0, 1, 4)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.analyze_button = QPushButton("플레이리스트 분석"); self.analyze_button.clicked.connect(self.start_playlist_import); row.addWidget(self.analyze_button)
        self.start_queue_button = QPushButton("배치 렌더 시작"); self.start_queue_button.clicked.connect(self.start_batch_processing); row.addWidget(self.start_queue_button)
        self.clear_button = QPushButton("분석 결과 비우기"); self.clear_button.clicked.connect(self.clear_analysis); row.addWidget(self.clear_button)
        self.clean_button = QPushButton("임시 파일 정리"); self.clean_button.clicked.connect(self.clean_temp_files); row.addWidget(self.clean_button)
        layout.addLayout(row); return frame

    def _build_summary_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QGridLayout(frame)
        layout.addWidget(QLabel("현재 플레이리스트"), 0, 0)
        self.playlist_title_value = QLabel("아직 분석한 플레이리스트가 없습니다."); layout.addWidget(self.playlist_title_value, 0, 1, 1, 5)
        self.total_count_value = QLabel("전체: 0"); layout.addWidget(self.total_count_value, 1, 0)
        self.included_count_value = QLabel("배치 포함: 0"); layout.addWidget(self.included_count_value, 1, 1)
        self.missing_lyrics_count_value = QLabel("가사 없음: 0"); layout.addWidget(self.missing_lyrics_count_value, 1, 2)
        self.review_needed_count_value = QLabel("검토 필요: 0"); layout.addWidget(self.review_needed_count_value, 1, 3)
        self.ready_status_value = QLabel("대기 중"); layout.addWidget(self.ready_status_value, 1, 4, 1, 2); return frame

    def _build_track_list_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("트랙 목록"); title.setObjectName("subtitle"); layout.addWidget(title)
        hint = QLabel("가사가 없거나 자동 해석이 실패한 곡도 그대로 남습니다. 선택해서 직접 수정할 수 있습니다."); hint.setObjectName("hint"); hint.setWordWrap(True); layout.addWidget(hint)
        self.track_list = QListWidget(); self.track_list.currentRowChanged.connect(self.handle_track_selection_changed); layout.addWidget(self.track_list, stretch=1); return frame

    def _build_detail_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("선택한 곡"); title.setObjectName("subtitle"); layout.addWidget(title)
        self.selected_track_source_label = QLabel("곡을 선택하면 상세 정보가 보입니다."); self.selected_track_source_label.setWordWrap(True); layout.addWidget(self.selected_track_source_label)
        self.selected_track_status_label = QLabel("상태: -"); layout.addWidget(self.selected_track_status_label)
        self.selected_track_note_label = QLabel("메모: -"); self.selected_track_note_label.setObjectName("hint"); self.selected_track_note_label.setWordWrap(True); layout.addWidget(self.selected_track_note_label)
        form = QGridLayout()
        form.addWidget(QLabel("제목"), 0, 0); self.track_title_input = QLineEdit(); form.addWidget(self.track_title_input, 0, 1)
        form.addWidget(QLabel("아티스트"), 0, 2); self.track_artist_input = QLineEdit(); form.addWidget(self.track_artist_input, 0, 3)
        form.addWidget(QLabel("앨범"), 1, 0); self.track_album_input = QLineEdit(); form.addWidget(self.track_album_input, 1, 1)
        form.addWidget(QLabel("길이"), 1, 2); self.track_duration_label = QLabel("-"); form.addWidget(self.track_duration_label, 1, 3)
        form.addWidget(QLabel("앨범아트 URL/경로"), 2, 0); self.track_album_art_input = QLineEdit(); form.addWidget(self.track_album_art_input, 2, 1, 1, 3)
        form.addWidget(QLabel("YouTube URL"), 3, 0); self.track_youtube_input = QLineEdit(); form.addWidget(self.track_youtube_input, 3, 1, 1, 3)
        form.addWidget(QLabel("가사 파일"), 4, 0); self.track_lrc_path_label = QLabel("-"); self.track_lrc_path_label.setWordWrap(True); form.addWidget(self.track_lrc_path_label, 4, 1, 1, 3)
        layout.addLayout(form)
        lyrics_title = QLabel("가사"); lyrics_title.setObjectName("subtitle"); layout.addWidget(lyrics_title)
        lyrics_hint = QLabel("가사를 직접 붙여넣거나 기존 .lrc/.txt 파일을 연결할 수 있습니다. 타임스탬프가 있으면 싱크 가사로 인식합니다."); lyrics_hint.setObjectName("hint"); lyrics_hint.setWordWrap(True); layout.addWidget(lyrics_hint)
        self.track_lyrics_input = QTextEdit(); self.track_lyrics_input.setPlaceholderText("가사가 없으면 여기 붙여넣기"); layout.addWidget(self.track_lyrics_input, stretch=1)
        row = QHBoxLayout()
        self.map_lrc_button = QPushButton("LRC 파일 연결"); self.map_lrc_button.clicked.connect(self.map_selected_track_lrc_file); row.addWidget(self.map_lrc_button)
        self.clear_lrc_button = QPushButton("LRC 연결 해제"); self.clear_lrc_button.clicked.connect(self.clear_selected_track_lrc_file); row.addWidget(self.clear_lrc_button)
        self.normalize_lyrics_button = QPushButton("가사 줄 정리"); self.normalize_lyrics_button.clicked.connect(self.normalize_selected_track_lyrics); row.addWidget(self.normalize_lyrics_button)
        self.save_track_button = QPushButton("현재 곡 저장"); self.save_track_button.clicked.connect(self.save_selected_track); row.addWidget(self.save_track_button)
        self.toggle_include_button = QPushButton("배치에 포함"); self.toggle_include_button.clicked.connect(self.toggle_selected_track_inclusion); row.addWidget(self.toggle_include_button)
        layout.addLayout(row); return frame

    def _build_progress_card(self) -> QWidget:
        frame = QFrame(); frame.setObjectName("card"); layout = QVBoxLayout(frame)
        title = QLabel("진행 상황"); title.setObjectName("subtitle"); layout.addWidget(title)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100); layout.addWidget(self.progress_bar)
        self.progress_log = QTextEdit(); self.progress_log.setReadOnly(True); layout.addWidget(self.progress_log, stretch=1); return frame

    def _load_saved_state(self) -> None:
        self.playlist_url_input.setText(self.config_manager.get("last_playlist_url", ""))
        index = self.output_mode_combo.findData(self.output_mode)
        if index >= 0: self.output_mode_combo.setCurrentIndex(index)
        model_id = resolve_model(self.config_manager.get_translation_model())
        index = self.model_combo.findData(model_id)
        if index >= 0: self.model_combo.setCurrentIndex(index)
        self.api_key_status.setText("OPENAI_API_KEY가 설정되어 있습니다." if os.getenv("OPENAI_API_KEY") else "배치를 실행하기 전에 OPENAI_API_KEY를 설정하세요.")

    def _persist_playlist_settings(self) -> None:
        self.config_manager.set("last_playlist_url", self.playlist_url_input.text().strip())

    def append_progress_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_log.append(f"[{timestamp}] {message}")
        bar = self.progress_log.verticalScrollBar(); bar.setValue(bar.maximum()); self.last_progress_message = message

    def update_progress_ui(self, message: str, value: int) -> None:
        self.progress_bar.setValue(max(0, min(100, value)))
        if message and message != self.last_progress_message: self.append_progress_message(message)

    def start_playlist_import(self) -> None:
        if self.worker is not None or self.import_worker is not None: return
        url = self.playlist_url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "플레이리스트 URL 없음", "먼저 플레이리스트 URL을 입력하세요."); return
        if "list=" not in url:
            QMessageBox.warning(self, "잘못된 플레이리스트 URL", "list 파라미터가 포함된 플레이리스트 URL을 입력하세요."); return
        if self.review_tracks:
            reply = QMessageBox.question(self, "현재 분석 결과 교체", "새 플레이리스트를 분석하면서 현재 목록을 덮어쓸까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes: return
        self._persist_playlist_settings(); self._reset_analysis_views(); self.progress_log.clear(); self.progress_bar.setRange(0, 0)
        self.playlist_title_value.setText("플레이리스트 분석 중..."); self.ready_status_value.setText("트랙과 가사를 불러오는 중..."); self.append_progress_message("플레이리스트 분석을 시작합니다..."); self.set_processing_state(True)
        self.import_worker = PlaylistImportWorker(url, self.output_mode_combo.currentData())
        self.import_worker.progress.connect(self.append_progress_message)
        self.import_worker.report_ready.connect(self.on_playlist_import_finished)
        self.import_worker.error_occurred.connect(self.on_playlist_import_error)
        self.import_worker.finished.connect(self._finalize_playlist_import)
        self.import_worker.finished.connect(self.import_worker.deleteLater)
        self.import_worker.start()

    def on_playlist_import_finished(self, report: PlaylistImportReport) -> None:
        self.current_playlist_title = report.playlist_title; self.review_tracks = report.tracks; self.playlist_title_value.setText(report.playlist_title)
        self._refresh_track_list()
        if self.review_tracks: self.track_list.setCurrentRow(0)
        else: self._clear_track_detail_inputs()
        self._refresh_pipeline_state(); self.append_progress_message(f"플레이리스트 분석 완료. 전체 {len(self.review_tracks)}곡을 불러왔습니다.")

    def on_playlist_import_error(self, error_message: str) -> None:
        self.playlist_title_value.setText("플레이리스트 분석 실패"); self.ready_status_value.setText("불러오기 실패")
        self.append_progress_message(f"플레이리스트 분석 실패: {error_message}")
        QMessageBox.critical(self, "플레이리스트 분석 실패", error_message)

    def handle_track_selection_changed(self, row: int) -> None:
        if not self._loading_track_details and self.current_track_index >= 0 and row != self.current_track_index:
            self._save_track_inputs_for_row(self.current_track_index, add_log=False, refresh_detail=False)
        self.on_track_selected(row)

    def on_track_selected(self, row: int) -> None:
        self.current_track_index = row
        if row < 0 or row >= len(self.review_tracks):
            self._clear_track_detail_inputs(); return
        track = self.review_tracks[row]
        self._loading_track_details = True
        self.selected_track_source_label.setText(f"원본 {track.source.index}번: {track.source.label}")
        self.selected_track_status_label.setText(f"상태: {self._status_text(track)}")
        self.selected_track_note_label.setText(f"메모: {track.note or '-'}")
        self.track_title_input.setText(track.title); self.track_artist_input.setText(track.artist); self.track_album_input.setText(track.album)
        self.track_album_art_input.setText(track.album_art_url); self.track_youtube_input.setText(track.youtube_url)
        self.track_duration_label.setText(self._format_duration(track.duration)); self.track_lrc_path_label.setText(track.lrc_path or "없음")
        self.track_lyrics_input.setPlainText(track.lyrics_text)
        self._loading_track_details = False
        self._refresh_detail_buttons()

    def save_selected_track(self) -> None:
        self._save_track_inputs_for_row(self.current_track_index, add_log=True, refresh_detail=True)

    def normalize_selected_track_lyrics(self) -> None:
        self.track_lyrics_input.setPlainText(prepare_lyric_text_for_subtitles(self.track_lyrics_input.toPlainText()))

    def map_selected_track_lrc_file(self) -> None:
        track = self._get_selected_track()
        if track is None:
            return
        start_dir = os.path.dirname(track.lrc_path) if track.lrc_path and os.path.exists(track.lrc_path) else os.getcwd()
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "가사 파일 선택",
            start_dir,
            "가사 파일 (*.lrc *.txt);;모든 파일 (*.*)",
        )
        if not file_path:
            return
        try:
            self._apply_lrc_file_to_track(self.current_track_index, file_path, add_log=True)
        except Exception as exc:
            QMessageBox.warning(self, "가사 파일 연결 실패", str(exc))

    def clear_selected_track_lrc_file(self) -> None:
        track = self._get_selected_track()
        if track is None:
            return
        self._apply_form_metadata_to_track(track)
        track.lrc_path = None
        track.lyrics_text = ""
        track.lyrics_mode = "missing"
        track.lyrics_source = "none"
        self.track_lyrics_input.clear()
        self._update_track_status(track, auto_include=False)
        self._refresh_track_list(preserve_selection=True)
        self.on_track_selected(self.current_track_index)
        self._refresh_pipeline_state()
        self.append_progress_message(f"LRC 연결 해제: {track.label}")

    def toggle_selected_track_inclusion(self) -> None:
        track = self._get_selected_track()
        if track is None: return
        if not track.can_render():
            QMessageBox.warning(self, "배치 포함 불가", "제목, 아티스트, YouTube URL, 가사 파일이 모두 있어야 합니다."); return
        track.include_in_batch = not track.include_in_batch; track.note = "배치에 포함됨" if track.include_in_batch else "수동으로 배치에서 제외됨"
        self._refresh_track_list(preserve_selection=True); self.on_track_selected(self.current_track_index); self._refresh_pipeline_state()

    def start_batch_processing(self) -> None:
        if self.worker is not None or self.import_worker is not None: return
        self._save_current_track_silently()
        self.render_queue = [track for track in self.review_tracks if track.include_in_batch and track.can_render()]
        if not self.render_queue:
            QMessageBox.warning(self, "렌더 대상 없음", "배치에 포함된 렌더 가능한 곡이 없습니다."); return
        self.processing_mode = "queue"; self.current_queue_index = 0; self.current_queue_batch_name = self._build_queue_batch_name()
        self.progress_log.clear(); self.append_progress_message(f"{len(self.render_queue)}곡 배치 렌더를 시작합니다. 배치 폴더: {self.current_queue_batch_name}")
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
        self.processing_mode = None; self.set_processing_state(False); self.ready_status_value.setText("대기 중")

    def on_process_error(self, error_message: str) -> None:
        self.append_progress_message(f"오류: {error_message}")
        if self.processing_mode == "queue":
            reply = QMessageBox.question(self, "큐 작업 실패", f"{error_message}\n\n다음 큐 작업을 계속 진행할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.current_queue_index += 1; self._start_next_queue_item(); return
        self.processing_mode = None; self.current_queue_batch_name = None; self.render_queue = []; self.set_processing_state(False)
        self.ready_status_value.setText("배치 렌더 중단"); QMessageBox.critical(self, "처리 실패", error_message)

    def clear_analysis(self) -> None:
        if self.worker is not None or self.import_worker is not None or not self.review_tracks: return
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
        for control in [self.playlist_url_input, self.output_mode_combo, self.model_combo, self.analyze_button, self.start_queue_button, self.clear_button, self.clean_button, self.track_list, self.track_title_input, self.track_artist_input, self.track_album_input, self.track_album_art_input, self.track_youtube_input, self.track_lyrics_input, self.map_lrc_button, self.clear_lrc_button, self.normalize_lyrics_button, self.save_track_button, self.toggle_include_button]:
            control.setDisabled(processing)

    def _refresh_pipeline_state(self) -> None:
        total = len(self.review_tracks); included = sum(1 for track in self.review_tracks if track.include_in_batch)
        missing = sum(1 for track in self.review_tracks if track.status == "missing_lyrics")
        review_needed = sum(1 for track in self.review_tracks if track.status in {"missing_lyrics", "incomplete", "error"})
        self.total_count_value.setText(f"전체: {total}"); self.included_count_value.setText(f"배치 포함: {included}")
        self.missing_lyrics_count_value.setText(f"가사 없음: {missing}"); self.review_needed_count_value.setText(f"검토 필요: {review_needed}")
        if self.worker is None and self.import_worker is None:
            if included: self.ready_status_value.setText("배치 렌더 준비 완료")
            elif self.current_playlist_title: self.ready_status_value.setText("수정 후 배치에 포함할 곡을 선택하세요")
            else: self.ready_status_value.setText("대기 중")
        has_targets = bool([track for track in self.review_tracks if track.include_in_batch and track.can_render()])
        self.start_queue_button.setEnabled(has_targets and self.worker is None and self.import_worker is None)
        self.clear_button.setEnabled(bool(self.review_tracks) and self.worker is None and self.import_worker is None)
        self._refresh_detail_buttons()

    def _refresh_track_list(self, preserve_selection: bool = False) -> None:
        selected_row = self.track_list.currentRow() if preserve_selection else -1
        self.track_list.blockSignals(True)
        try:
            self.track_list.clear()
            for track in self.review_tracks: self.track_list.addItem(self._format_track_list_text(track))
            if preserve_selection and 0 <= selected_row < len(self.review_tracks): self.track_list.setCurrentRow(selected_row)
        finally:
            self.track_list.blockSignals(False)

    def _refresh_detail_buttons(self) -> None:
        track = self._get_selected_track(); has_track = track is not None
        self.save_track_button.setEnabled(has_track and self.worker is None and self.import_worker is None)
        self.map_lrc_button.setEnabled(has_track and self.worker is None and self.import_worker is None)
        self.normalize_lyrics_button.setEnabled(has_track and self.worker is None and self.import_worker is None)
        if not has_track:
            self.clear_lrc_button.setEnabled(False); self.toggle_include_button.setEnabled(False); self.toggle_include_button.setText("배치에 포함"); return
        self.clear_lrc_button.setEnabled(bool(track.lrc_path) and self.worker is None and self.import_worker is None)
        self.toggle_include_button.setEnabled(self.worker is None and self.import_worker is None)
        if track.include_in_batch: self.toggle_include_button.setText("배치에서 제외")
        elif track.can_render(): self.toggle_include_button.setText("배치에 포함")
        else: self.toggle_include_button.setText("저장 후 배치 포함 가능")

    def _reset_analysis_views(self) -> None:
        self.review_tracks.clear(); self.render_queue = []; self.current_track_index = -1; self.current_playlist_title = ""; self.track_list.clear()
        self.playlist_title_value.setText("아직 분석한 플레이리스트가 없습니다."); self._clear_track_detail_inputs(); self._refresh_pipeline_state()

    def _clear_track_detail_inputs(self) -> None:
        self.selected_track_source_label.setText("곡을 선택하면 상세 정보가 보입니다."); self.selected_track_status_label.setText("상태: -"); self.selected_track_note_label.setText("메모: -")
        self.track_title_input.clear(); self.track_artist_input.clear(); self.track_album_input.clear(); self.track_album_art_input.clear(); self.track_youtube_input.clear()
        self.track_duration_label.setText("-"); self.track_lrc_path_label.setText("-"); self.track_lyrics_input.clear(); self._refresh_detail_buttons()

    def _get_selected_track(self) -> Optional[PlaylistReviewTrack]:
        return self.review_tracks[self.current_track_index] if 0 <= self.current_track_index < len(self.review_tracks) else None

    def _update_track_status(self, track: PlaylistReviewTrack, *, auto_include: bool) -> None:
        if not track.title.strip() or not track.artist.strip():
            track.status = "incomplete"; track.note = "제목과 아티스트를 입력해야 합니다."; track.include_in_batch = False; return
        if not track.youtube_url.strip():
            track.status = "incomplete"; track.note = "YouTube URL이 없습니다."; track.include_in_batch = False; return
        if not track.lrc_path or not os.path.exists(track.lrc_path):
            track.status = "missing_lyrics"; track.note = "가사가 없습니다. 직접 입력하거나 수정한 뒤 저장하세요."; track.include_in_batch = False; track.lyrics_mode = "missing"; return
        if track.lyrics_source == "mapped":
            track.note = "외부 싱크 LRC 파일이 연결되었습니다." if track.lyrics_mode == "synced" else "외부 일반 가사 파일이 연결되었습니다."
        else:
            track.note = "싱크 가사가 준비되었습니다." if track.lyrics_mode == "synced" else "일반 가사가 준비되었습니다."
        track.status = "ready"
        if auto_include: track.include_in_batch = True

    def _status_text(self, track: PlaylistReviewTrack) -> str:
        if track.status == "ready": return "배치 포함 가능" if track.include_in_batch else "렌더 가능하지만 현재 제외됨"
        if track.status == "missing_lyrics": return "가사 없음"
        if track.status == "incomplete": return "필수 정보 부족"
        if track.status == "error": return "자동 해석 실패"
        return track.status

    def _format_track_list_text(self, track: PlaylistReviewTrack) -> str:
        prefix = "[포함]" if track.status == "ready" and track.include_in_batch else "[제외]" if track.status == "ready" else "[가사 없음]" if track.status == "missing_lyrics" else "[정보 부족]" if track.status == "incomplete" else "[확인 필요]"
        lyrics_text = {"synced": "싱크", "plain": "일반", "missing": "없음"}.get(track.lyrics_mode, track.lyrics_mode)
        return f"{prefix} {track.source.index:02d}. {track.label}\n가사: {lyrics_text}"

    def _format_duration(self, duration: Optional[int]) -> str:
        return "없음" if not isinstance(duration, int) else f"{duration // 60:02d}:{duration % 60:02d}"

    def _save_current_track_silently(self) -> None:
        if not self._loading_track_details and self.current_track_index >= 0:
            self._save_track_inputs_for_row(self.current_track_index, add_log=False, refresh_detail=False)

    def _apply_form_metadata_to_track(self, track: PlaylistReviewTrack) -> None:
        track.title = self.track_title_input.text().strip()
        track.artist = self.track_artist_input.text().strip()
        track.album = self.track_album_input.text().strip()
        track.album_art_url = self.track_album_art_input.text().strip()
        raw_youtube_url = self.track_youtube_input.text().strip()
        track.youtube_url = sanitize_youtube_url(raw_youtube_url) or raw_youtube_url

    def _save_track_inputs_for_row(self, row: int, *, add_log: bool, refresh_detail: bool) -> None:
        if row < 0 or row >= len(self.review_tracks):
            return

        track = self.review_tracks[row]
        was_renderable = track.can_render()
        was_included = track.include_in_batch

        self._apply_form_metadata_to_track(track)

        lyrics_text = self.track_lyrics_input.toPlainText().strip()
        if lyrics_text:
            should_preserve_mapped_file = False
            if track.lyrics_source == "mapped" and track.lrc_path and os.path.exists(track.lrc_path):
                try:
                    mapped_text, mapped_mode = load_review_track_lyrics_file(track.lrc_path)
                except Exception:
                    mapped_text, mapped_mode = "", "missing"
                if mapped_text == lyrics_text:
                    track.lyrics_text = mapped_text
                    track.lyrics_mode = mapped_mode
                    should_preserve_mapped_file = True
            if not should_preserve_mapped_file:
                track.lrc_path, track.lyrics_text = save_review_track_lyrics(
                    artist=track.artist or track.source.artist,
                    title=track.title or track.source.title,
                    lyrics_text=lyrics_text,
                )
                track.lyrics_mode = "synced" if lyrics_are_synced(track.lyrics_text) else "plain"
                track.lyrics_source = "generated"
        else:
            track.lrc_path = None
            track.lyrics_text = ""
            track.lyrics_mode = "missing"
            track.lyrics_source = "none"

        auto_include = was_included or not was_renderable
        self._update_track_status(track, auto_include=auto_include)
        self._refresh_track_list(preserve_selection=True)
        self._refresh_pipeline_state()

        if refresh_detail:
            self.on_track_selected(row)
        if add_log:
            self.append_progress_message(f"저장 완료: {track.label}")

    def _apply_lrc_file_to_track(self, row: int, file_path: str, *, add_log: bool) -> None:
        if row < 0 or row >= len(self.review_tracks):
            return
        track = self.review_tracks[row]
        was_renderable = track.can_render()
        self._apply_form_metadata_to_track(track)
        absolute_path = os.path.abspath(file_path)
        lyrics_text, lyrics_mode = load_review_track_lyrics_file(absolute_path)
        track.lrc_path = absolute_path
        track.lyrics_text = lyrics_text
        track.lyrics_mode = lyrics_mode
        track.lyrics_source = "mapped"
        self._update_track_status(track, auto_include=track.include_in_batch or not was_renderable)
        self._refresh_track_list(preserve_selection=True)
        self.on_track_selected(row)
        self._refresh_pipeline_state()
        if add_log:
            self.append_progress_message(f"LRC 파일 연결: {track.label}")

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
        if (self.worker is not None and self.worker.isRunning()) or (self.import_worker is not None and self.import_worker.isRunning()):
            QMessageBox.warning(self, "작업 진행 중", "현재 작업이 끝난 뒤에 창을 닫아 주세요."); event.ignore(); return
        super().closeEvent(event)


MainWindow = PlaylistPipelineWindow
