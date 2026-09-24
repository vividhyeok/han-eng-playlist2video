"""Main PyQt window for the playlist-first lyric video pipeline."""

from __future__ import annotations

import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QDialog,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config.config_manager import get_config
from app.config.paths import BASE_DIR, OUTPUT_DIR, TEMP_DIR, TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import OPENAI_MODELS, resolve_model
from app.pipeline.playlist_importer import (
    PlaylistImportReport, PlaylistSkippedTrack, import_playlist,
    prepare_manual_lyrics_track,
)
from app.pipeline.process_manager import (
    ProcessConfig, ProcessManager, TimingReviewRequired, TranslationReviewRequired,
)
from app.ui.sync_dialog import ManualSyncDialog, PlainLyricsDialog
from app.ui.translation_dialog import TranslationReviewDialog
from app.ui.styles import MODERN_STYLESHEET


@dataclass
class QueueItem:
    config: ProcessConfig
    label: str
    lyrics_mode: str


class WorkerThread(QThread):
    progress = pyqtSignal(str, int)
    result_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(object)

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
            self.error_occurred.emit(exc)
            return

        self.result_ready.emit(output_path)


class PlaylistImportWorker(QThread):
    progress = pyqtSignal(str)
    report_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, playlist_url: str, lyrics_policy: str, output_mode: str):
        super().__init__()
        self.playlist_url = playlist_url
        self.lyrics_policy = lyrics_policy
        self.output_mode = output_mode

    def run(self) -> None:
        try:
            report = import_playlist(
                self.playlist_url,
                output_mode=self.output_mode,
                lyrics_policy=self.lyrics_policy,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return

        self.report_ready.emit(report)


class PlaylistPipelineWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_data_dirs()

        self.setWindowTitle("Lyric Video Maker")
        self.setMinimumSize(1120, 760)
        self.resize(1280, 850)
        self.setStyleSheet(MODERN_STYLESHEET)

        self.config_manager = get_config()
        self.output_mode = self.config_manager.get("output_mode", "video")

        self.queue_items: list[QueueItem] = []
        self.skipped_tracks: list[PlaylistSkippedTrack] = []
        self.current_queue_index = 0
        self.current_queue_batch_name: Optional[str] = None
        self.current_playlist_title = ""
        self.worker: Optional[WorkerThread] = None
        self.import_worker: Optional[PlaylistImportWorker] = None
        self.worker_result_path: Optional[str] = None
        self.worker_error_message: Optional[object] = None
        self.processing_mode: Optional[str] = None
        self.last_progress_message = ""

        self._build_ui()
        self._load_saved_state()
        self._refresh_pipeline_state()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        root.addWidget(self._build_header_card())
        root.addWidget(self._build_setup_card())
        splitter = QSplitter()
        splitter.addWidget(self._build_queue_card())
        splitter.addWidget(self._build_skipped_card())
        splitter.setSizes([760, 540])
        root.addWidget(splitter, stretch=3)
        root.addWidget(self._build_progress_card(), stretch=2)

    def _build_header_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("hero")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(20, 15, 20, 15)

        copy = QVBoxLayout()
        copy.setSpacing(3)
        eyebrow = QLabel("DESKTOP WORKBENCH  ·  v2.6.0")
        eyebrow.setObjectName("eyebrow")
        copy.addWidget(eyebrow)

        title = QLabel("Lyric Video Maker")
        title.setObjectName("title")
        copy.addWidget(title)

        note = QLabel(
            "플레이리스트를 분석하고, 한글·영문 가사 영상을 한 번에 렌더링하세요."
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        copy.addWidget(note)
        layout.addLayout(copy, stretch=1)

        self.ready_status_value = QLabel("준비됨")
        self.ready_status_value.setObjectName("statusReady")
        layout.addWidget(self.ready_status_value)
        return frame

    def _build_setup_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(10)

        header = QLabel("1  플레이리스트 설정")
        header.setObjectName("subtitle")
        layout.addWidget(header)

        form = QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        form.addWidget(QLabel("플레이리스트 URL"), 0, 0)
        self.playlist_url_input = QLineEdit()
        self.playlist_url_input.setPlaceholderText(
            "https://music.youtube.com/playlist?list=... or https://www.youtube.com/playlist?list=..."
        )
        self.playlist_url_input.textChanged.connect(self._persist_playlist_settings)
        form.addWidget(self.playlist_url_input, 0, 1, 1, 3)

        form.addWidget(QLabel("가사 정책"), 1, 0)
        self.lyrics_policy_combo = QComboBox()
        self.lyrics_policy_combo.addItem(
            "일반 가사 허용 · 직접 타이밍 입력",
            "allow_plain",
        )
        self.lyrics_policy_combo.addItem(
            "처음부터 싱크 가사가 있는 곡만 처리",
            "require_synced",
        )
        self.lyrics_policy_combo.currentIndexChanged.connect(self._persist_playlist_settings)
        form.addWidget(self.lyrics_policy_combo, 1, 1)

        form.addWidget(QLabel("출력 형식"), 1, 2)
        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItem("MP4 영상", "video")
        self.output_mode_combo.addItem("Premiere XML", "premiere_xml")
        self.output_mode_combo.currentIndexChanged.connect(self.on_output_mode_changed)
        form.addWidget(self.output_mode_combo, 1, 3)

        form.addWidget(QLabel("번역 모델"), 2, 0)
        self.model_combo = QComboBox()
        for model_id, label in OPENAI_MODELS.items():
            self.model_combo.addItem(label, model_id)
        self.model_combo.currentIndexChanged.connect(self.on_model_changed)
        form.addWidget(self.model_combo, 2, 1)

        form.addWidget(QLabel("OpenAI API 키"), 2, 2)
        key_row = QHBoxLayout()
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("설정됨 · 변경할 때만 입력" if os.getenv("OPENAI_API_KEY") else "sk-...")
        key_row.addWidget(self.api_key_input, stretch=1)
        self.save_key_button = QPushButton("저장")
        self.save_key_button.setObjectName("secondary")
        self.save_key_button.clicked.connect(self.save_api_key)
        key_row.addWidget(self.save_key_button)
        form.addLayout(key_row, 2, 3)

        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.analyze_button = QPushButton("플레이리스트 분석")
        self.analyze_button.clicked.connect(self.start_playlist_import)
        action_row.addWidget(self.analyze_button)

        self.start_queue_button = QPushButton("전체 렌더링 시작")
        self.start_queue_button.clicked.connect(self.start_batch_processing)
        action_row.addWidget(self.start_queue_button)

        self.manual_sync_button = QPushButton("선택 곡 수동 타이밍")
        self.manual_sync_button.clicked.connect(self.start_selected_manual_sync)
        self.manual_sync_button.setObjectName("secondary")
        action_row.addWidget(self.manual_sync_button)

        self.remove_selected_button = QPushButton("선택 항목 제외")
        self.remove_selected_button.clicked.connect(self.remove_selected_queue_item)
        self.remove_selected_button.setObjectName("secondary")
        action_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("분석 초기화")
        self.clear_button.clicked.connect(self.clear_analysis)
        self.clear_button.setObjectName("danger")
        action_row.addWidget(self.clear_button)

        self.clean_button = QPushButton("캐시 정리")
        self.clean_button.clicked.connect(self.clean_temp_files)
        self.clean_button.setObjectName("secondary")
        action_row.addWidget(self.clean_button)

        self.open_output_button = QPushButton("결과 폴더 열기")
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_output_button.setObjectName("secondary")
        action_row.addWidget(self.open_output_button)

        action_row.addStretch()
        layout.addLayout(action_row)
        return frame

    def _build_queue_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        header = QHBoxLayout()
        title = QLabel("2  렌더링 대기열")
        title.setObjectName("subtitle")
        header.addWidget(title)
        header.addStretch()
        self.queued_count_value = QLabel("0곡")
        self.queued_count_value.setObjectName("hint")
        header.addWidget(self.queued_count_value)
        layout.addLayout(header)

        hint = QLabel(
            "가사와 음원이 준비된 곡입니다. 표시된 순서대로 처리됩니다."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.queue_list = QListWidget()
        self.queue_list.itemSelectionChanged.connect(
            lambda: self.skipped_list.clearSelection()
            if self.queue_list.selectedItems() and hasattr(self, "skipped_list") else None
        )
        layout.addWidget(self.queue_list, stretch=1)
        return frame

    def _build_skipped_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        header = QHBoxLayout()
        title = QLabel("검토 필요 / 제외")
        title.setObjectName("subtitle")
        header.addWidget(title)
        header.addStretch()
        self.skipped_count_value = QLabel("0곡")
        self.skipped_count_value.setObjectName("hint")
        header.addWidget(self.skipped_count_value)
        layout.addLayout(header)

        hint = QLabel(
            "가사를 찾지 못했거나 자동 처리할 수 없는 곡이 표시됩니다."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.skipped_list = QListWidget()
        self.skipped_list.itemSelectionChanged.connect(
            lambda: self.queue_list.clearSelection()
            if self.skipped_list.selectedItems() else None
        )
        layout.addWidget(self.skipped_list, stretch=1)
        return frame

    def _build_progress_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        header = QHBoxLayout()
        title = QLabel("3  작업 진행")
        title.setObjectName("subtitle")
        header.addWidget(title)
        header.addStretch()
        self.playlist_title_value = QLabel("아직 분석한 플레이리스트가 없습니다.")
        self.playlist_title_value.setObjectName("hint")
        header.addWidget(self.playlist_title_value)
        layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        self.progress_log = QTextEdit()
        self.progress_log.setReadOnly(True)
        layout.addWidget(self.progress_log, stretch=1)
        return frame

    def _load_saved_state(self) -> None:
        self.playlist_url_input.setText(self.config_manager.get("last_playlist_url", ""))

        output_mode_index = self.output_mode_combo.findData(self.output_mode)
        if output_mode_index >= 0:
            self.output_mode_combo.setCurrentIndex(output_mode_index)

        lyrics_policy = self.config_manager.get("playlist_lyrics_policy", "allow_plain")
        lyrics_policy_index = self.lyrics_policy_combo.findData(lyrics_policy)
        if lyrics_policy_index >= 0:
            self.lyrics_policy_combo.setCurrentIndex(lyrics_policy_index)

        model_id = resolve_model(self.config_manager.get_translation_model())
        model_index = self.model_combo.findData(model_id)
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)

        self.api_key_input.setPlaceholderText(
            "설정됨 · 변경할 때만 입력" if os.getenv("OPENAI_API_KEY") else "sk-..."
        )

    def _persist_playlist_settings(self) -> None:
        self.config_manager.set("last_playlist_url", self.playlist_url_input.text().strip())
        self.config_manager.set(
            "playlist_lyrics_policy",
            self.lyrics_policy_combo.currentData(),
        )

    def append_progress_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_log.append(f"[{timestamp}] {message}")
        scroll_bar = self.progress_log.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self.last_progress_message = message

    def update_progress_ui(self, message: str, value: int) -> None:
        self.progress_bar.setValue(max(0, min(100, value)))
        if message and message != self.last_progress_message:
            self.append_progress_message(message)

    def start_playlist_import(self) -> None:
        if self.worker is not None or self.import_worker is not None:
            return

        playlist_url = self.playlist_url_input.text().strip()
        if not playlist_url:
            QMessageBox.warning(self, "플레이리스트 URL 필요", "플레이리스트 URL을 먼저 입력해 주세요.")
            return

        if "list=" not in playlist_url:
            QMessageBox.warning(
                self,
                "URL 확인",
                "list 매개변수가 포함된 YouTube 또는 YouTube Music 플레이리스트 URL을 입력해 주세요.",
            )
            return

        if self.queue_items or self.skipped_list.count():
            reply = QMessageBox.question(
                self,
                "현재 분석 교체",
                "현재 대기열과 제외 목록을 지우고 새 플레이리스트를 분석할까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._persist_playlist_settings()
        self._reset_analysis_views()
        self.progress_log.clear()
        self.progress_bar.setRange(0, 0)
        self.playlist_title_value.setText("플레이리스트 분석 중...")
        self.ready_status_value.setText("곡 정보와 가사를 가져오는 중...")
        self.append_progress_message("플레이리스트 분석을 시작합니다.")
        self.set_processing_state(True)

        self.import_worker = PlaylistImportWorker(
            playlist_url=playlist_url,
            lyrics_policy=self.lyrics_policy_combo.currentData(),
            output_mode=self.output_mode_combo.currentData(),
        )
        self.import_worker.progress.connect(self.append_progress_message)
        self.import_worker.report_ready.connect(self.on_playlist_import_finished)
        self.import_worker.error_occurred.connect(self.on_playlist_import_error)
        self.import_worker.finished.connect(self._finalize_playlist_import)
        self.import_worker.finished.connect(self.import_worker.deleteLater)
        self.import_worker.start()

    def on_playlist_import_finished(self, report: PlaylistImportReport) -> None:
        self.current_playlist_title = report.playlist_title
        self.playlist_title_value.setText(report.playlist_title)

        for prepared_track in report.prepared_tracks:
            queue_item = QueueItem(
                config=prepared_track.config,
                label=prepared_track.label,
                lyrics_mode=prepared_track.lyrics_mode,
            )
            self.queue_items.append(queue_item)
            self._add_queue_row(queue_item)

        for skipped_track in report.skipped_tracks:
            self.skipped_tracks.append(skipped_track)
            self._add_skipped_row(skipped_track)

        self._refresh_pipeline_state()

        synced_count = sum(1 for item in self.queue_items if item.lyrics_mode == "synced")
        plain_count = sum(1 for item in self.queue_items if item.lyrics_mode == "plain")
        self.append_progress_message(
            f"Playlist analysis complete. Queued {len(self.queue_items)} track(s), skipped {self.skipped_list.count()}."
        )
        QMessageBox.information(
            self,
            "Playlist analysis complete",
            "\n".join(
                [
                    f"Playlist: {report.playlist_title}",
                    f"Queued: {len(self.queue_items)}",
                    f"Synced lyrics: {synced_count}",
                    f"직접 타이밍 입력 필요: {plain_count}",
                    f"Skipped: {self.skipped_list.count()}",
                ]
            ),
        )

    def on_playlist_import_error(self, error_message: str) -> None:
        self.playlist_title_value.setText("플레이리스트 분석 실패")
        self.ready_status_value.setText("분석 실패")
        self.append_progress_message(f"플레이리스트 분석 실패: {error_message}")
        QMessageBox.critical(self, "플레이리스트 분석 실패", error_message)

    def start_batch_processing(self) -> None:
        if self.worker is not None or self.import_worker is not None or not self.queue_items:
            return

        self.processing_mode = "queue"
        self.current_queue_index = 0
        self.current_queue_batch_name = self._build_queue_batch_name()
        self.progress_log.clear()
        self.append_progress_message(
            f"Starting batch render for {len(self.queue_items)} track(s). "
            f"Batch folder: {self.current_queue_batch_name}"
        )
        self.ready_status_value.setText("대기열 렌더링 중...")
        self._start_next_queue_item()

    def start_selected_manual_sync(self) -> None:
        if self.worker is not None or self.import_worker is not None:
            return
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(self.queue_items):
            skipped_row = self.skipped_list.currentRow()
            if skipped_row < 0 or skipped_row >= len(self.skipped_tracks):
                QMessageBox.information(self, "곡 선택", "왼쪽 대기열 또는 오른쪽 제외 목록에서 곡을 먼저 선택하세요.")
                return
            skipped = self.skipped_tracks[skipped_row]
            dialog = PlainLyricsDialog(
                artist=skipped.source.artist,
                title=skipped.source.title,
                parent=self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                prepared = prepare_manual_lyrics_track(
                    skipped.source,
                    dialog.lyrics_text(),
                    output_mode=self.output_mode_combo.currentData(),
                )
            except Exception as exc:
                QMessageBox.critical(self, "가사 준비 실패", str(exc))
                return
            queue_item = QueueItem(config=prepared.config, label=prepared.label, lyrics_mode="plain")
            self.queue_items.append(queue_item)
            self._add_queue_row(queue_item)
            row = len(self.queue_items) - 1
            self.queue_list.setCurrentRow(row)
            self.skipped_tracks.pop(skipped_row)
            self.skipped_list.takeItem(skipped_row)
            self._refresh_pipeline_state()
        self.processing_mode = "single_queue_item"
        self.current_queue_index = row
        self.current_queue_batch_name = self._build_queue_batch_name()
        config = deepcopy(self.queue_items[row].config)
        config.batch_name = self.current_queue_batch_name
        config.force_manual_sync = True
        self.append_progress_message(f"수동 타이밍 준비: {self.queue_items[row].label}")
        self._start_worker(config)

    def _start_next_queue_item(self) -> None:
        if self.current_queue_index >= len(self.queue_items):
            self.processing_mode = None
            self.current_queue_batch_name = None
            self.set_processing_state(False)
            self.ready_status_value.setText("전체 렌더링 완료")
            self.append_progress_message("대기열 렌더링이 완료되었습니다.")
            QMessageBox.information(self, "완료", "대기열의 모든 곡 처리가 끝났습니다.")
            return

        queue_item = self.queue_items[self.current_queue_index]
        self.queue_list.setCurrentRow(self.current_queue_index)
        self.append_progress_message(
            f"Rendering {self.current_queue_index + 1}/{len(self.queue_items)}: {queue_item.label}"
        )
        config = deepcopy(queue_item.config)
        config.batch_name = self.current_queue_batch_name
        self._start_worker(config)

    def _start_worker(self, config: ProcessConfig) -> None:
        validation_error = ProcessManager(lambda *_: None).validate_config(config)
        if validation_error:
            self.processing_mode = None
            self.current_queue_batch_name = None
            self.ready_status_value.setText("설정 확인 필요")
            QMessageBox.warning(self, "설정 확인", validation_error)
            return

        self.set_processing_state(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._reset_worker_state()
        self.worker = WorkerThread(config)
        self.worker.progress.connect(self.update_progress_ui)
        self.worker.result_ready.connect(self._capture_worker_result)
        self.worker.error_occurred.connect(self._capture_worker_error)
        self.worker.finished.connect(self._finalize_worker)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def on_process_finished(self, output_path: str) -> None:
        self.append_progress_message(f"Finished: {output_path}")
        if self.processing_mode == "queue":
            self.current_queue_index += 1
            self._start_next_queue_item()
            return

        self.processing_mode = None
        self.current_queue_batch_name = None
        self.set_processing_state(False)
        self.ready_status_value.setText("준비됨")

    def on_process_error(self, error: object) -> None:
        error_message = str(error)
        self.append_progress_message(f"Error: {error_message}")
        if isinstance(error, TimingReviewRequired) and error.audio_path:
            self.set_processing_state(False)
            dialog = ManualSyncDialog(
                audio_path=error.audio_path,
                lrc_path=error.lrc_path,
                low_indexes=error.low_indexes,
                parent=self,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                queue_item = self.queue_items[self.current_queue_index]
                queue_item.config.lrc_path = error.lrc_path
                queue_item.lyrics_mode = "synced"
                self._refresh_queue_row(self.current_queue_index)
                self.append_progress_message("수동 싱크를 저장했습니다. 현재 곡을 다시 처리합니다.")
                config = deepcopy(queue_item.config)
                config.batch_name = self.current_queue_batch_name
                config.resume_existing = True
                self._start_worker(config)
                return
        if isinstance(error, TranslationReviewRequired):
            self.set_processing_state(False)
            dialog = TranslationReviewDialog(
                json_path=error.json_path,
                issues=error.issues,
                artist=self.queue_items[self.current_queue_index].config.artist,
                title=self.queue_items[self.current_queue_index].config.title,
                parent=self,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                queue_item = self.queue_items[self.current_queue_index]
                queue_item.config.pretranslated_json_path = error.json_path
                self.append_progress_message(
                    f"애매한 번역 {len(error.issues)}개를 직접 확정했습니다. 현재 곡을 다시 처리합니다."
                )
                config = deepcopy(queue_item.config)
                config.batch_name = self.current_queue_batch_name
                config.resume_existing = True
                self._start_worker(config)
                return
        if self.processing_mode == "queue":
            prompt = QMessageBox(self)
            prompt.setIcon(QMessageBox.Icon.Warning)
            prompt.setWindowTitle("현재 곡 처리 실패")
            prompt.setText(error_message)
            prompt.setInformativeText("이 곡을 건너뛰고 다음 곡을 계속 처리할까요?")
            skip_button = prompt.addButton("다음 곡 계속", QMessageBox.ButtonRole.AcceptRole)
            prompt.addButton("전체 작업 중단", QMessageBox.ButtonRole.RejectRole)
            prompt.exec()
            if prompt.clickedButton() is skip_button:
                self.current_queue_index += 1
                self._start_next_queue_item()
                return

        self.processing_mode = None
        self.current_queue_batch_name = None
        self.set_processing_state(False)
        self.ready_status_value.setText("작업 중단됨")
        QMessageBox.critical(self, "처리 실패", error_message)

    def remove_selected_queue_item(self) -> None:
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(self.queue_items):
            return

        removed = self.queue_items.pop(row)
        self.queue_list.takeItem(row)
        self.append_progress_message(f"Removed from queue: {removed.label}")
        self._refresh_pipeline_state()

    def clear_analysis(self) -> None:
        if self.worker is not None or self.import_worker is not None:
            return

        if not self.queue_items and self.skipped_list.count() == 0:
            return

        reply = QMessageBox.question(
            self,
            "Clear analysis",
            "Remove the current queue and skipped list from the UI?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._reset_analysis_views()
        self.ready_status_value.setText("준비됨")
        self.append_progress_message("Cleared the current analysis.")

    def clean_temp_files(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clean temporary files",
            "Delete files in data/temp and remove the translation cache?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        if os.path.isdir(TEMP_DIR):
            for entry in os.listdir(TEMP_DIR):
                path = os.path.join(TEMP_DIR, entry)
                if os.path.isdir(path):
                    try:
                        shutil.rmtree(path)
                        deleted += 1
                    except OSError as exc:
                        self.append_progress_message(f"Failed to delete {path}: {exc}")
                elif os.path.isfile(path):
                    try:
                        os.remove(path)
                        deleted += 1
                    except OSError as exc:
                        self.append_progress_message(f"Failed to delete {path}: {exc}")

        if os.path.exists(TRANSLATION_CACHE_PATH):
            try:
                os.remove(TRANSLATION_CACHE_PATH)
            except OSError as exc:
                self.append_progress_message(f"Failed to remove cache: {exc}")

        self.append_progress_message(f"Cleaned {deleted} temp file(s).")
        QMessageBox.information(self, "정리 완료", "임시 파일을 삭제했습니다.")

    def save_api_key(self) -> None:
        key = self.api_key_input.text().strip()
        if not key:
            QMessageBox.information(self, "API 키", "변경할 API 키를 입력해 주세요.")
            return
        if not key.startswith("sk-"):
            QMessageBox.warning(self, "API 키 확인", "OpenAI API 키 형식을 확인해 주세요.")
            return

        env_path = os.path.join(BASE_DIR, ".env")
        existing: list[str] = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as env_file:
                existing = [
                    line.rstrip("\n")
                    for line in env_file
                    if not line.startswith("OPENAI_API_KEY=")
                ]
        existing.append(f"OPENAI_API_KEY={key}")
        with open(env_path, "w", encoding="utf-8") as env_file:
            env_file.write("\n".join(existing).strip() + "\n")

        os.environ["OPENAI_API_KEY"] = key
        self.api_key_input.clear()
        self.api_key_input.setPlaceholderText("설정됨 · 변경할 때만 입력")
        self.append_progress_message("OpenAI API 키를 저장했습니다.")
        QMessageBox.information(self, "저장 완료", "API 키가 이 PC에 저장되었습니다.")

    def open_output_folder(self) -> None:
        ensure_data_dirs()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(OUTPUT_DIR)):
            QMessageBox.warning(self, "폴더 열기 실패", OUTPUT_DIR)

    def on_model_changed(self, index: int) -> None:
        model_id = self.model_combo.itemData(index)
        if model_id:
            self.config_manager.set_translation_model(resolve_model(model_id))

    def on_output_mode_changed(self, index: int) -> None:
        output_mode = self.output_mode_combo.itemData(index)
        if output_mode:
            self.output_mode = output_mode
            self.config_manager.set("output_mode", output_mode)

    def set_processing_state(self, processing: bool) -> None:
        for control in [
            self.playlist_url_input,
            self.lyrics_policy_combo,
            self.output_mode_combo,
            self.model_combo,
            self.analyze_button,
            self.start_queue_button,
            self.manual_sync_button,
            self.remove_selected_button,
            self.clear_button,
            self.clean_button,
            self.api_key_input,
            self.save_key_button,
        ]:
            control.setDisabled(processing)

    def _refresh_pipeline_state(self) -> None:
        queued_count = len(self.queue_items)
        skipped_count = self.skipped_list.count()

        self.queued_count_value.setText(f"{queued_count}곡")
        self.skipped_count_value.setText(f"{skipped_count}곡")

        if self.worker is None and self.import_worker is None:
            if queued_count:
                self.ready_status_value.setText("렌더링 준비됨")
            elif self.current_playlist_title:
                self.ready_status_value.setText("처리 가능한 곡 없음")
            else:
                self.ready_status_value.setText("준비됨")

        self.start_queue_button.setEnabled(
            bool(self.queue_items)
            and self.worker is None
            and self.import_worker is None
        )
        self.remove_selected_button.setEnabled(
            bool(self.queue_items)
            and self.worker is None
            and self.import_worker is None
        )
        self.manual_sync_button.setEnabled(
            (bool(self.queue_items) or skipped_count > 0)
            and self.worker is None
            and self.import_worker is None
        )
        self.clear_button.setEnabled(
            (bool(self.queue_items) or skipped_count > 0)
            and self.worker is None
            and self.import_worker is None
        )

    def _reset_analysis_views(self) -> None:
        self.queue_items.clear()
        self.queue_list.clear()
        self.skipped_list.clear()
        self.skipped_tracks.clear()
        self.current_playlist_title = ""
        self.playlist_title_value.setText("아직 분석한 플레이리스트가 없습니다.")
        self._refresh_pipeline_state()

    def _format_queue_text(self, queue_item: QueueItem) -> str:
        lyrics_label = "타이밍 등록됨" if queue_item.lyrics_mode == "synced" else "가사 등록됨 · 타이밍 입력 필요"
        return f"{queue_item.label}\n{lyrics_label}"

    def _add_queue_row(self, queue_item: QueueItem) -> None:
        item = QListWidgetItem()
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 5, 5, 5)
        label = QLabel(self._format_queue_text(queue_item))
        label.setWordWrap(True)
        layout.addWidget(label, stretch=1)
        button = QPushButton("⏱ 타이밍")
        button.setObjectName("secondary")
        button.setToolTip("이 곡의 가사 타이밍을 직접 편집합니다")
        button.clicked.connect(lambda _checked=False, row_item=item: self._start_queue_row_timing(row_item))
        layout.addWidget(button)
        item.setSizeHint(widget.sizeHint())
        self.queue_list.addItem(item)
        self.queue_list.setItemWidget(item, widget)

    def _add_skipped_row(self, skipped_track: PlaylistSkippedTrack) -> None:
        item = QListWidgetItem()
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 5, 5, 5)
        label = QLabel(f"{skipped_track.source.label}\n가사를 직접 등록할 수 있습니다")
        label.setWordWrap(True)
        layout.addWidget(label, stretch=1)
        button = QPushButton("✎ 가사 등록")
        button.setObjectName("secondary")
        button.setToolTip("일반 가사를 붙여 넣고 수동 타이밍 작업을 시작합니다")
        button.clicked.connect(lambda _checked=False, row_item=item: self._start_skipped_row_registration(row_item))
        layout.addWidget(button)
        item.setSizeHint(widget.sizeHint())
        self.skipped_list.addItem(item)
        self.skipped_list.setItemWidget(item, widget)

    def _start_queue_row_timing(self, item: QListWidgetItem) -> None:
        self.queue_list.setCurrentItem(item)
        self.start_selected_manual_sync()

    def _start_skipped_row_registration(self, item: QListWidgetItem) -> None:
        self.skipped_list.setCurrentItem(item)
        self.start_selected_manual_sync()

    def _refresh_queue_row(self, row: int) -> None:
        item = self.queue_list.item(row)
        widget = self.queue_list.itemWidget(item) if item else None
        if widget:
            label = widget.findChild(QLabel)
            if label:
                label.setText(self._format_queue_text(self.queue_items[row]))

    def _capture_worker_result(self, output_path: str) -> None:
        self.worker_result_path = output_path

    def _capture_worker_error(self, error: object) -> None:
        self.worker_error_message = error

    def _finalize_worker(self) -> None:
        self.worker = None

        if self.worker_error_message:
            error_message = self.worker_error_message
            self._reset_worker_state()
            self.on_process_error(error_message)
            return

        if self.worker_result_path:
            output_path = self.worker_result_path
            self._reset_worker_state()
            self.on_process_finished(output_path)
            return

        self._reset_worker_state()
        if self.processing_mode is not None:
            self.on_process_error("Worker thread exited without returning a result.")

    def _finalize_playlist_import(self) -> None:
        self.import_worker = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.set_processing_state(False)
        self._refresh_pipeline_state()

    def _reset_worker_state(self) -> None:
        self.worker_result_path = None
        self.worker_error_message = None

    @staticmethod
    def _build_queue_batch_name() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f__queue")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if (
            self.worker is not None and self.worker.isRunning()
        ) or (
            self.import_worker is not None and self.import_worker.isRunning()
        ):
            QMessageBox.warning(
                self,
                "Processing in progress",
                "Wait for the current playlist import or batch render to finish before closing the app.",
            )
            event.ignore()
            return

        super().closeEvent(event)


MainWindow = PlaylistPipelineWindow
