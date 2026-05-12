"""수동 가사 싱크 매핑 다이얼로그."""

from __future__ import annotations

import re

from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox, QPushButton, QStackedWidget, QTextEdit, QVBoxLayout, QWidget

from app.lyrics.lyric_text_utils import normalize_lyric_text, prepare_lyric_text_for_subtitles, summarize_lyric_text

TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}:\d{2}(?:[.:]\d{1,3})?)\]")


class LyricSyncDialog(QDialog):
    def __init__(self, audio_path: str, lyrics_text: str, parent=None) -> None:
        super().__init__(parent)
        self.audio_path = audio_path
        self.initial_text = lyrics_text
        self.raw_lyrics: list[str] = []
        self.timestamps: list[str | None] = []
        self.current_line_index = 0
        self.lrc_content: str | None = None
        self.original_edit_text = lyrics_text
        self.last_marked_index: int | None = None

        self.setWindowTitle("가사 싱크 매핑")
        self.resize(860, 920)

        self._build_ui()
        self._build_player()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QLabel(
            "<b>가사 싱크 매핑</b><br>"
            "Enter: 재생/일시정지, Space: 현재 줄 시점 찍기"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self.edit_page = QWidget()
        edit_layout = QVBoxLayout(self.edit_page)
        edit_layout.addWidget(
            QLabel(
                "1단계. 가사 텍스트를 정리합니다. 한 줄이 하나의 싱크 단위가 됩니다. "
                "줄바꿈 정리나 긴 줄 분할을 먼저 해두고 매핑을 시작하세요."
            )
        )
        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(self.initial_text)
        self.text_edit.textChanged.connect(self._update_lyric_stats)
        edit_layout.addWidget(self.text_edit)

        tools = QHBoxLayout()
        normalize_button = QPushButton("줄바꿈 정리")
        normalize_button.clicked.connect(self.normalize_lyrics)
        tools.addWidget(normalize_button)

        split_button = QPushButton("긴 줄 나누기")
        split_button.clicked.connect(self.split_long_lines)
        tools.addWidget(split_button)

        restore_button = QPushButton("원문 복원")
        restore_button.clicked.connect(self.restore_original_text)
        tools.addWidget(restore_button)
        edit_layout.addLayout(tools)

        self.lyric_stats_label = QLabel("")
        self.lyric_stats_label.setWordWrap(True)
        edit_layout.addWidget(self.lyric_stats_label)

        start_button = QPushButton("싱크 매핑 시작")
        start_button.clicked.connect(self.start_sync_mode)
        edit_layout.addWidget(start_button)
        self.stack.addWidget(self.edit_page)

        self.sync_page = QWidget()
        sync_layout = QVBoxLayout(self.sync_page)
        sync_layout.addWidget(
            QLabel(
                "2단계. 목록에 포커스를 둔 상태에서 키보드를 사용합니다.\n"
                "Enter: 재생/일시정지, Space: 시점 찍기, 좌/우: 이동, Delete: 시점 삭제"
            )
        )

        self.progress_label = QLabel("")
        self.progress_label.setWordWrap(True)
        sync_layout.addWidget(self.progress_label)

        self.current_line_label = QLabel("")
        self.current_line_label.setWordWrap(True)
        sync_layout.addWidget(self.current_line_label)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self.on_row_changed)
        self.list_widget.installEventFilter(self)
        sync_layout.addWidget(self.list_widget)

        self.sync_status_label = QLabel("")
        self.sync_status_label.setWordWrap(True)
        sync_layout.addWidget(self.sync_status_label)

        controls = QHBoxLayout()
        self.back_button = QPushButton("-5초")
        self.back_button.clicked.connect(lambda: self.seek_relative(-5000))
        controls.addWidget(self.back_button)

        self.play_button = QPushButton("재생")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)

        self.forward_button = QPushButton("+5초")
        self.forward_button.clicked.connect(lambda: self.seek_relative(5000))
        controls.addWidget(self.forward_button)

        self.time_label = QLabel("00:00.00")
        controls.addWidget(self.time_label)
        sync_layout.addLayout(controls)

        edit_controls = QHBoxLayout()
        prev_row_button = QPushButton("이전 줄")
        prev_row_button.clicked.connect(lambda: self._move_row(-1))
        edit_controls.addWidget(prev_row_button)

        next_row_button = QPushButton("다음 줄")
        next_row_button.clicked.connect(lambda: self._move_row(1))
        edit_controls.addWidget(next_row_button)

        edit_time_button = QPushButton("시점 직접 수정")
        edit_time_button.clicked.connect(self.edit_timestamp)
        edit_controls.addWidget(edit_time_button)

        clear_time_button = QPushButton("시점 삭제")
        clear_time_button.clicked.connect(self.clear_timestamp)
        edit_controls.addWidget(clear_time_button)

        undo_button = QPushButton("마지막 찍기 취소")
        undo_button.clicked.connect(self.undo_last_mark)
        edit_controls.addWidget(undo_button)
        sync_layout.addLayout(edit_controls)

        preview_title = QLabel("생성될 LRC 미리보기")
        preview_title.setObjectName("subtitle")
        sync_layout.addWidget(preview_title)

        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        sync_layout.addWidget(self.preview_text, stretch=1)

        self.save_button = QPushButton("LRC 저장")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_lrc)
        sync_layout.addWidget(self.save_button)

        self.stack.addWidget(self.sync_page)
        self._update_lyric_stats()

    def _build_player(self) -> None:
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setSource(QUrl.fromLocalFile(self.audio_path))

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_time)
        self.timer.start()

    def start_sync_mode(self) -> None:
        lines, timestamps = self._build_rows_from_editor()
        if not lines:
            QMessageBox.warning(self, "가사 없음", "먼저 한 줄 이상 가사를 넣어 주세요.")
            return

        self.raw_lyrics = lines
        self.timestamps = timestamps
        self.current_line_index = next((index for index, stamp in enumerate(self.timestamps) if stamp is None), 0)
        self.last_marked_index = None

        self.list_widget.clear()
        for row in range(len(lines)):
            self.list_widget.addItem(self._format_row_text(row))
        self.list_widget.setCurrentRow(self.current_line_index)
        self.sync_status_label.setText("목록 순서가 그대로 싱크 순서입니다. 이미 있는 시점은 그대로 유지됩니다.")
        self.stack.setCurrentWidget(self.sync_page)
        self.list_widget.setFocus()
        self._update_sync_progress()
        self._update_preview()

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_button.setText("재생")
        else:
            self.player.play()
            self.play_button.setText("일시정지")

    def update_time(self) -> None:
        self.time_label.setText(self.format_time(self.player.position()))

    def eventFilter(self, source, event):  # noqa: N802 - Qt API name
        if source == self.list_widget and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Space:
                self.mark_timestamp()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.toggle_playback()
                return True
            if event.key() == Qt.Key.Key_Left:
                self.seek_relative(-5000)
                return True
            if event.key() == Qt.Key.Key_Right:
                self.seek_relative(5000)
                return True
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.clear_timestamp()
                return True
        return super().eventFilter(source, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if self.stack.currentWidget() != self.sync_page:
            super().keyPressEvent(event)
            return

        if event.key() == Qt.Key.Key_Space:
            self.mark_timestamp()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_playback()
        elif event.key() == Qt.Key.Key_Left:
            self.seek_relative(-5000)
        elif event.key() == Qt.Key.Key_Right:
            self.seek_relative(5000)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.clear_timestamp()
        else:
            super().keyPressEvent(event)

    def seek_relative(self, milliseconds: int) -> None:
        new_position = max(0, self.player.position() + milliseconds)
        self.player.setPosition(new_position)
        self.time_label.setText(self.format_time(new_position))

    def on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self.raw_lyrics):
            return
        self.current_line_index = row
        timestamp = self.timestamps[row]
        if timestamp:
            self.player.setPosition(self._timestamp_to_milliseconds(timestamp))
        self.list_widget.setFocus()
        self._update_sync_progress()

    def edit_timestamp(self) -> None:
        if not self.raw_lyrics:
            return
        current_timestamp = self.timestamps[self.current_line_index] or "00:00.00"
        new_timestamp, accepted = QInputDialog.getText(self, "시점 수정", "시점 (MM:SS.xx)", text=current_timestamp)
        if not accepted or not new_timestamp:
            return
        try:
            self._timestamp_to_milliseconds(new_timestamp)
        except ValueError:
            QMessageBox.warning(self, "잘못된 시점", "MM:SS.xx 형식으로 입력하세요.")
            return
        self.timestamps[self.current_line_index] = new_timestamp
        self._refresh_row(self.current_line_index)
        self.save_button.setEnabled(any(self.timestamps))
        self._update_sync_progress()
        self._update_preview()

    def clear_timestamp(self) -> None:
        if not self.raw_lyrics:
            return
        self.timestamps[self.current_line_index] = None
        self._refresh_row(self.current_line_index)
        self.save_button.setEnabled(any(self.timestamps))
        self._update_sync_progress()
        self._update_preview()

    def mark_timestamp(self) -> None:
        if self.current_line_index >= len(self.raw_lyrics):
            return
        timestamp = self.format_time(self.player.position())
        self.timestamps[self.current_line_index] = timestamp
        self.last_marked_index = self.current_line_index
        self._refresh_row(self.current_line_index)
        self.save_button.setEnabled(True)
        self._update_sync_progress()
        self._update_preview()

        next_row = self.current_line_index + 1
        if next_row < len(self.raw_lyrics):
            self.list_widget.setCurrentRow(next_row)
            self.list_widget.scrollToItem(self.list_widget.item(next_row))
        else:
            self.save_button.setFocus()

    def save_lrc(self) -> None:
        if not any(self.timestamps):
            QMessageBox.warning(self, "시점 없음", "최소 한 줄 이상 시점을 찍어야 저장할 수 있습니다.")
            return

        lines = []
        for lyric, timestamp in zip(self.raw_lyrics, self.timestamps):
            if timestamp:
                lines.append(f"[{timestamp}]{lyric}")

        self.lrc_content = "\n".join(lines).strip() + "\n"
        self.accept()

    def undo_last_mark(self) -> None:
        if self.last_marked_index is None:
            QMessageBox.information(self, "되돌릴 항목 없음", "최근에 찍은 시점이 없습니다.")
            return
        self.timestamps[self.last_marked_index] = None
        self.list_widget.setCurrentRow(self.last_marked_index)
        self._refresh_row(self.last_marked_index)
        self.save_button.setEnabled(any(self.timestamps))
        self._update_sync_progress()
        self._update_preview()
        self.last_marked_index = None

    def get_lrc_content(self) -> str | None:
        return self.lrc_content

    def normalize_lyrics(self) -> None:
        self.text_edit.setPlainText(normalize_lyric_text(self.text_edit.toPlainText()))

    def split_long_lines(self) -> None:
        self.text_edit.setPlainText(prepare_lyric_text_for_subtitles(self.text_edit.toPlainText()))

    def restore_original_text(self) -> None:
        self.text_edit.setPlainText(self.original_edit_text)

    def _update_lyric_stats(self) -> None:
        summary = summarize_lyric_text(self.text_edit.toPlainText())
        self.lyric_stats_label.setText(
            f"현재 {summary.line_count}줄, 긴 줄 {summary.long_line_count}개, 최대 시각 길이 {summary.max_visual_length:.1f}"
        )

    def _refresh_row(self, row: int) -> None:
        item = self.list_widget.item(row)
        if item is not None:
            item.setText(self._format_row_text(row))

    def _format_row_text(self, row: int) -> str:
        timestamp = self.timestamps[row] or "--:--.--"
        return f"{row + 1:02d}. [{timestamp}] {self.raw_lyrics[row]}"

    def _update_sync_progress(self) -> None:
        if not self.raw_lyrics:
            self.progress_label.setText("")
            self.current_line_label.setText("")
            return
        mapped_count = sum(1 for item in self.timestamps if item)
        total_count = len(self.raw_lyrics)
        remaining_count = total_count - mapped_count
        current_lyric = self.raw_lyrics[self.current_line_index] if 0 <= self.current_line_index < total_count else ""
        current_stamp = self.timestamps[self.current_line_index] if 0 <= self.current_line_index < total_count else None
        self.progress_label.setText(
            f"진행: {mapped_count}/{total_count}줄 완료, 남은 {remaining_count}줄"
        )
        self.current_line_label.setText(
            f"현재 줄 {self.current_line_index + 1}/{total_count}: "
            f"[{current_stamp or '--:--.--'}] {current_lyric}"
        )
        self.save_button.setText(f"LRC 저장 ({mapped_count}줄 완료)")

    def _update_preview(self) -> None:
        preview_lines = []
        for lyric, timestamp in zip(self.raw_lyrics, self.timestamps):
            if timestamp:
                preview_lines.append(f"[{timestamp}]{lyric}")
            else:
                preview_lines.append(f"[--:--.--]{lyric}")
        self.preview_text.setPlainText("\n".join(preview_lines))

    def _move_row(self, delta: int) -> None:
        if not self.raw_lyrics:
            return
        next_row = min(max(self.current_line_index + delta, 0), len(self.raw_lyrics) - 1)
        self.list_widget.setCurrentRow(next_row)

    def _build_rows_from_editor(self) -> tuple[list[str], list[str | None]]:
        raw_text = self.text_edit.toPlainText().strip()
        if not raw_text:
            return [], []

        has_timestamps = bool(TIMESTAMP_PATTERN.search(raw_text))
        if not has_timestamps:
            lines = [line.rstrip() for line in raw_text.splitlines() if line.strip()]
            return lines, [None] * len(lines)

        lines: list[str] = []
        timestamps: list[str | None] = []
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            matches = TIMESTAMP_PATTERN.findall(line)
            lyric = TIMESTAMP_PATTERN.sub("", line).strip()
            if not lyric:
                continue
            lines.append(lyric)
            timestamps.append(matches[-1] if matches else None)
        return lines, timestamps

    @staticmethod
    def format_time(milliseconds: int) -> str:
        seconds = (milliseconds // 1000) % 60
        minutes = milliseconds // 60000
        hundredths = (milliseconds // 10) % 100
        return f"{minutes:02d}:{seconds:02d}.{hundredths:02d}"

    @staticmethod
    def _timestamp_to_milliseconds(timestamp: str) -> int:
        minutes, seconds = timestamp.split(":")
        whole_seconds, hundredths = seconds.split(".")
        return int(minutes) * 60000 + int(whole_seconds) * 1000 + int(hundredths) * 10
