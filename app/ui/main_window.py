"""Main PyQt window for the playlist-first lyric video pipeline."""

from __future__ import annotations

import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
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
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config.config_manager import get_config
from app.config.paths import TEMP_DIR, TRANSLATION_CACHE_PATH, ensure_data_dirs
from app.lyrics.ai_models import OPENAI_MODELS, resolve_model
from app.pipeline.playlist_importer import PlaylistImportReport, import_playlist
from app.pipeline.process_manager import ProcessConfig, ProcessManager
from app.ui.styles import MODERN_STYLESHEET


@dataclass
class QueueItem:
    config: ProcessConfig
    label: str
    lyrics_mode: str


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

        self.setWindowTitle("Playlist Lyric Video Pipeline")
        self.setMinimumSize(1360, 920)
        self.setStyleSheet(MODERN_STYLESHEET)

        self.config_manager = get_config()
        self.output_mode = self.config_manager.get("output_mode", "video")

        self.queue_items: list[QueueItem] = []
        self.current_queue_index = 0
        self.current_queue_batch_name: Optional[str] = None
        self.current_playlist_title = ""
        self.worker: Optional[WorkerThread] = None
        self.import_worker: Optional[PlaylistImportWorker] = None
        self.worker_result_path: Optional[str] = None
        self.worker_error_message: Optional[str] = None
        self.processing_mode: Optional[str] = None
        self.last_progress_message = ""

        self._build_ui()
        self._load_saved_state()
        self._refresh_pipeline_state()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(18)

        root.addWidget(self._build_header_card())
        root.addWidget(self._build_setup_card())
        root.addWidget(self._build_summary_card())

        splitter = QSplitter()
        splitter.addWidget(self._build_queue_card())
        splitter.addWidget(self._build_skipped_card())
        splitter.setSizes([760, 540])
        root.addWidget(splitter, stretch=1)

        root.addWidget(self._build_progress_card(), stretch=1)

    def _build_header_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        title = QLabel("Playlist Lyric Video Pipeline")
        title.setObjectName("title")
        layout.addWidget(title)

        note = QLabel(
            "Analyze a YouTube or YouTube Music playlist, build the render queue automatically, "
            "then run the batch in one pass."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return frame

    def _build_setup_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        header = QLabel("Pipeline Setup")
        header.setObjectName("subtitle")
        layout.addWidget(header)

        form = QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        form.addWidget(QLabel("Playlist URL"), 0, 0)
        self.playlist_url_input = QLineEdit()
        self.playlist_url_input.setPlaceholderText(
            "https://music.youtube.com/playlist?list=... or https://www.youtube.com/playlist?list=..."
        )
        self.playlist_url_input.textChanged.connect(self._persist_playlist_settings)
        form.addWidget(self.playlist_url_input, 0, 1, 1, 3)

        form.addWidget(QLabel("Lyrics policy"), 1, 0)
        self.lyrics_policy_combo = QComboBox()
        self.lyrics_policy_combo.addItem(
            "Allow plain lyrics if synced lyrics are missing",
            "allow_plain",
        )
        self.lyrics_policy_combo.addItem(
            "Queue only tracks with synced lyrics",
            "require_synced",
        )
        self.lyrics_policy_combo.currentIndexChanged.connect(self._persist_playlist_settings)
        form.addWidget(self.lyrics_policy_combo, 1, 1)

        form.addWidget(QLabel("Output mode"), 1, 2)
        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItem("Video", "video")
        self.output_mode_combo.addItem("Premiere XML", "premiere_xml")
        self.output_mode_combo.currentIndexChanged.connect(self.on_output_mode_changed)
        form.addWidget(self.output_mode_combo, 1, 3)

        form.addWidget(QLabel("Translation model"), 2, 0)
        self.model_combo = QComboBox()
        for model_id, label in OPENAI_MODELS.items():
            self.model_combo.addItem(label, model_id)
        self.model_combo.currentIndexChanged.connect(self.on_model_changed)
        form.addWidget(self.model_combo, 2, 1)

        self.api_key_status = QLabel("")
        self.api_key_status.setObjectName("hint")
        self.api_key_status.setWordWrap(True)
        form.addWidget(self.api_key_status, 2, 2, 1, 2)

        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.analyze_button = QPushButton("Analyze Playlist")
        self.analyze_button.clicked.connect(self.start_playlist_import)
        action_row.addWidget(self.analyze_button)

        self.start_queue_button = QPushButton("Start Batch Render")
        self.start_queue_button.clicked.connect(self.start_batch_processing)
        action_row.addWidget(self.start_queue_button)

        self.remove_selected_button = QPushButton("Remove Selected")
        self.remove_selected_button.clicked.connect(self.remove_selected_queue_item)
        self.remove_selected_button.setObjectName("secondary")
        action_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("Clear Analysis")
        self.clear_button.clicked.connect(self.clear_analysis)
        self.clear_button.setObjectName("danger")
        action_row.addWidget(self.clear_button)

        self.clean_button = QPushButton("Clean Temp Files")
        self.clean_button.clicked.connect(self.clean_temp_files)
        self.clean_button.setObjectName("secondary")
        action_row.addWidget(self.clean_button)

        action_row.addStretch()
        layout.addLayout(action_row)
        return frame

    def _build_summary_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QGridLayout(frame)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(10)

        playlist_title_caption = QLabel("Current Playlist")
        playlist_title_caption.setObjectName("subtitle")
        layout.addWidget(playlist_title_caption, 0, 0)

        self.playlist_title_value = QLabel("No playlist analyzed yet.")
        self.playlist_title_value.setWordWrap(True)
        layout.addWidget(self.playlist_title_value, 0, 1, 1, 3)

        self.queued_count_value = QLabel("Queued: 0")
        self.queued_count_value.setObjectName("subtitle")
        layout.addWidget(self.queued_count_value, 1, 0)

        self.skipped_count_value = QLabel("Skipped: 0")
        self.skipped_count_value.setObjectName("subtitle")
        layout.addWidget(self.skipped_count_value, 1, 1)

        self.ready_status_value = QLabel("Idle")
        self.ready_status_value.setObjectName("hint")
        layout.addWidget(self.ready_status_value, 1, 2, 1, 2)
        return frame

    def _build_queue_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        title = QLabel("Render Queue")
        title.setObjectName("subtitle")
        layout.addWidget(title)

        hint = QLabel(
            "These tracks already have a resolved YouTube source and a lyric file. "
            "Batch render will process them in order."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.queue_list = QListWidget()
        layout.addWidget(self.queue_list, stretch=1)
        return frame

    def _build_skipped_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        title = QLabel("Skipped Tracks")
        title.setObjectName("subtitle")
        layout.addWidget(title)

        hint = QLabel(
            "Tracks without usable lyrics or with failed resolution stay here so you can review what was excluded."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.skipped_list = QListWidget()
        layout.addWidget(self.skipped_list, stretch=1)
        return frame

    def _build_progress_card(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)

        title = QLabel("Progress")
        title.setObjectName("subtitle")
        layout.addWidget(title)

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

        self.api_key_status.setText(
            "OPENAI_API_KEY detected."
            if os.getenv("OPENAI_API_KEY")
            else "Set OPENAI_API_KEY in .env or your shell before running the batch."
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
            QMessageBox.warning(self, "Missing playlist URL", "Paste a playlist URL first.")
            return

        if "list=" not in playlist_url:
            QMessageBox.warning(
                self,
                "Invalid playlist URL",
                "Paste a YouTube or YouTube Music playlist URL that includes a list parameter.",
            )
            return

        if self.queue_items or self.skipped_list.count():
            reply = QMessageBox.question(
                self,
                "Replace current analysis",
                "Analyze a new playlist and replace the current queue and skipped list?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._persist_playlist_settings()
        self._reset_analysis_views()
        self.progress_log.clear()
        self.progress_bar.setRange(0, 0)
        self.playlist_title_value.setText("Analyzing playlist...")
        self.ready_status_value.setText("Importing playlist metadata and lyrics...")
        self.append_progress_message("Starting playlist analysis...")
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
            self.queue_list.addItem(
                self._format_queue_text(queue_item)
            )

        for skipped_track in report.skipped_tracks:
            self.skipped_list.addItem(
                f"{skipped_track.source.label}\nReason: {skipped_track.reason}"
            )

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
                    f"Plain lyrics: {plain_count}",
                    f"Skipped: {self.skipped_list.count()}",
                ]
            ),
        )

    def on_playlist_import_error(self, error_message: str) -> None:
        self.playlist_title_value.setText("Playlist analysis failed.")
        self.ready_status_value.setText("Import failed")
        self.append_progress_message(f"Playlist import failed: {error_message}")
        QMessageBox.critical(self, "Playlist import failed", error_message)

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
        self.ready_status_value.setText("Rendering queue...")
        self._start_next_queue_item()

    def _start_next_queue_item(self) -> None:
        if self.current_queue_index >= len(self.queue_items):
            self.processing_mode = None
            self.current_queue_batch_name = None
            self.set_processing_state(False)
            self.ready_status_value.setText("Batch render complete")
            self.append_progress_message("Batch render complete.")
            QMessageBox.information(self, "Queue complete", "All queued jobs finished.")
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
            self.ready_status_value.setText("Validation failed")
            QMessageBox.warning(self, "Invalid configuration", validation_error)
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
        self.set_processing_state(False)
        self.ready_status_value.setText("Idle")

    def on_process_error(self, error_message: str) -> None:
        self.append_progress_message(f"Error: {error_message}")
        if self.processing_mode == "queue":
            reply = QMessageBox.question(
                self,
                "Queue item failed",
                f"{error_message}\n\nContinue with the next queued item?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.current_queue_index += 1
                self._start_next_queue_item()
                return

        self.processing_mode = None
        self.current_queue_batch_name = None
        self.set_processing_state(False)
        self.ready_status_value.setText("Batch render interrupted")
        QMessageBox.critical(self, "Processing failed", error_message)

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
        self.ready_status_value.setText("Idle")
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
        QMessageBox.information(self, "Cleanup complete", "Temporary files were removed.")

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
            self.remove_selected_button,
            self.clear_button,
            self.clean_button,
        ]:
            control.setDisabled(processing)

    def _refresh_pipeline_state(self) -> None:
        queued_count = len(self.queue_items)
        skipped_count = self.skipped_list.count()

        self.queued_count_value.setText(f"Queued: {queued_count}")
        self.skipped_count_value.setText(f"Skipped: {skipped_count}")

        if self.worker is None and self.import_worker is None:
            if queued_count:
                self.ready_status_value.setText("Ready for batch render")
            elif self.current_playlist_title:
                self.ready_status_value.setText("Analysis complete with no renderable tracks")
            else:
                self.ready_status_value.setText("Idle")

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
        self.clear_button.setEnabled(
            (bool(self.queue_items) or skipped_count > 0)
            and self.worker is None
            and self.import_worker is None
        )

    def _reset_analysis_views(self) -> None:
        self.queue_items.clear()
        self.queue_list.clear()
        self.skipped_list.clear()
        self.current_playlist_title = ""
        self.playlist_title_value.setText("No playlist analyzed yet.")
        self._refresh_pipeline_state()

    def _format_queue_text(self, queue_item: QueueItem) -> str:
        return f"{queue_item.label}\nLyrics: {queue_item.lyrics_mode}"

    def _capture_worker_result(self, output_path: str) -> None:
        self.worker_result_path = output_path

    def _capture_worker_error(self, error_message: str) -> None:
        self.worker_error_message = error_message

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
