"""Keyboard-first manual lyric timing editor."""
from __future__ import annotations

import re

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton,
    QSlider, QVBoxLayout,
)

TIMESTAMP = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]")


def _load_points(path: str) -> list[dict[str, float | str]]:
    points: list[dict[str, float | str]] = []
    with open(path, "r", encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if not line:
                continue
            match = TIMESTAMP.search(line)
            seconds = 0.0
            if match:
                seconds = int(match.group(1)) * 60 + float(match.group(2))
            text = TIMESTAMP.sub("", line).strip()
            if text:
                points.append({"time": seconds, "text": text})
    return points


def _format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


class ManualSyncDialog(QDialog):
    def __init__(self, *, audio_path: str, lrc_path: str, parent=None):
        super().__init__(parent)
        self.audio_path = audio_path
        self.lrc_path = lrc_path
        self.points = _load_points(lrc_path)
        self.current_index = 0

        self.setWindowTitle("수동 탭 싱크")
        self.resize(900, 680)
        self.setModal(True)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setSource(QUrl.fromLocalFile(audio_path))

        root = QVBoxLayout(self)
        title = QLabel("음악을 재생하고 각 가사가 시작되는 순간 Space를 누르세요.")
        title.setObjectName("subtitle")
        root.addWidget(title)
        help_text = QLabel("Space 기록/다음 줄 · ↑↓ 줄 이동 · ←→ 0.1초 보정 · Backspace 이전 기록 취소")
        help_text.setObjectName("hint")
        root.addWidget(help_text)

        self.position_label = QLabel("00:00.00 / 00:00.00")
        root.addWidget(self.position_label)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(self.player.setPosition)
        root.addWidget(self.seek)

        controls = QHBoxLayout()
        self.play_button = QPushButton("재생")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        for label, delta in (("-5초", -5000), ("-0.1초", -100), ("+0.1초", 100), ("+5초", 5000)):
            button = QPushButton(label)
            button.setObjectName("secondary")
            button.clicked.connect(lambda _checked=False, value=delta: self.seek_relative(value))
            controls.addWidget(button)
        self.tap_button = QPushButton("현재 줄 기록 · Space")
        self.tap_button.clicked.connect(self.tap_current)
        controls.addWidget(self.tap_button)
        root.addLayout(controls)

        self.lines = QListWidget()
        self.lines.currentRowChanged.connect(self.select_row)
        root.addWidget(self.lines, stretch=1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("싱크 저장 후 다시 렌더")
        save.clicked.connect(self.save_and_accept)
        actions.addWidget(save)
        root.addLayout(actions)

        self.player.durationChanged.connect(self._duration_changed)
        self.player.positionChanged.connect(self._position_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_position)
        self.timer.start(100)
        self._refresh_lines()

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek_relative(self, milliseconds: int) -> None:
        self.player.setPosition(max(0, self.player.position() + milliseconds))

    def tap_current(self) -> None:
        if not self.points:
            return
        self.points[self.current_index]["time"] = self.player.position() / 1000.0
        if self.current_index < len(self.points) - 1:
            self.current_index += 1
        self._refresh_lines()

    def select_row(self, row: int) -> None:
        if 0 <= row < len(self.points):
            self.current_index = row

    def nudge_current(self, delta: float) -> None:
        if self.points:
            current = float(self.points[self.current_index]["time"])
            self.points[self.current_index]["time"] = max(0.0, current + delta)
            self._refresh_lines()

    def undo_previous(self) -> None:
        if not self.points:
            return
        self.current_index = max(0, self.current_index - 1)
        self.points[self.current_index]["time"] = 0.0
        self._refresh_lines()

    def _refresh_lines(self) -> None:
        self.lines.blockSignals(True)
        self.lines.clear()
        for index, point in enumerate(self.points):
            marker = "▶" if index == self.current_index else " "
            self.lines.addItem(f"{marker}  {_format_time(float(point['time']))}    {point['text']}")
        self.lines.setCurrentRow(self.current_index)
        self.lines.scrollToItem(self.lines.currentItem())
        self.lines.blockSignals(False)

    def _duration_changed(self, duration: int) -> None:
        self.seek.setRange(0, max(0, duration))

    def _position_changed(self, position: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(position)

    def _refresh_position(self) -> None:
        self.position_label.setText(
            f"{_format_time(self.player.position() / 1000)} / "
            f"{_format_time(self.player.duration() / 1000)}"
        )

    def _playback_changed(self, state) -> None:
        self.play_button.setText("일시정지" if state == QMediaPlayer.PlaybackState.PlayingState else "재생")

    def save_and_accept(self) -> None:
        if not self.points or any(float(point["time"]) <= 0 for point in self.points[1:]):
            QMessageBox.warning(self, "기록 확인", "두 번째 줄부터 모든 가사의 시작 시점을 기록해 주세요.")
            return
        times = [float(point["time"]) for point in self.points]
        if any(a > b for a, b in zip(times, times[1:])):
            QMessageBox.warning(self, "순서 확인", "가사 시작 시점이 앞줄보다 빠른 항목이 있습니다.")
            return
        with open(self.lrc_path, "w", encoding="utf-8") as file:
            for point in self.points:
                file.write(f"[{_format_time(float(point['time']))}] {point['text']}\n")
        self.player.stop()
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space:
            self.tap_current()
        elif event.key() == Qt.Key.Key_Up:
            self.current_index = max(0, self.current_index - 1)
            self._refresh_lines()
        elif event.key() == Qt.Key.Key_Down:
            self.current_index = min(len(self.points) - 1, self.current_index + 1)
            self._refresh_lines()
        elif event.key() == Qt.Key.Key_Left:
            self.nudge_current(-0.1)
        elif event.key() == Qt.Key.Key_Right:
            self.nudge_current(0.1)
        elif event.key() == Qt.Key.Key_Backspace:
            self.undo_previous()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.player.stop()
        super().closeEvent(event)
