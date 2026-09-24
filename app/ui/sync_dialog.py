"""Keyboard-first manual lyric timing editor."""
from __future__ import annotations

import re
from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (QDialog, QDoubleSpinBox, QHBoxLayout, QLabel, QListWidget,
    QMessageBox, QPushButton, QSlider, QTextEdit, QVBoxLayout)

TIMESTAMP = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]")

class PlainLyricsDialog(QDialog):
    def __init__(self, *, artist: str, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("일반 가사 붙여 넣기")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        heading = QLabel(f"{artist} - {title}"); heading.setObjectName("subtitle"); layout.addWidget(heading)
        note = QLabel("가사를 한 줄에 한 구절씩 붙여 넣으세요. 저장 후 음원을 준비하고 수동 타이밍 매핑을 시작합니다.")
        note.setObjectName("hint"); note.setWordWrap(True); layout.addWidget(note)
        self.editor = QTextEdit(); self.editor.setPlaceholderText("첫 번째 가사 줄\n두 번째 가사 줄\n세 번째 가사 줄")
        layout.addWidget(self.editor, stretch=1)
        actions = QHBoxLayout(); actions.addStretch()
        cancel = QPushButton("취소"); cancel.setObjectName("secondary"); cancel.clicked.connect(self.reject); actions.addWidget(cancel)
        save = QPushButton("가사 저장 후 수동 타이밍 시작"); save.clicked.connect(self._accept_if_valid); actions.addWidget(save)
        layout.addLayout(actions)

    def _accept_if_valid(self):
        if not self.lyrics_text():
            QMessageBox.warning(self, "가사 확인", "가사를 한 줄 이상 입력하세요."); return
        self.accept()

    def lyrics_text(self) -> str:
        return self.editor.toPlainText().strip()

def _load_points(path: str) -> list[dict[str, float | str]]:
    points = []
    with open(path, "r", encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if not line:
                continue
            match = TIMESTAMP.search(line)
            seconds = int(match.group(1)) * 60 + float(match.group(2)) if match else 0.0
            text = TIMESTAMP.sub("", line).strip()
            if text:
                points.append({"time": seconds, "text": text, "assigned": bool(match)})
    return points

def _format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"

class ManualSyncDialog(QDialog):
    def __init__(self, *, audio_path: str, lrc_path: str, low_indexes: tuple[int, ...] = (), parent=None):
        super().__init__(parent)
        self.audio_path, self.lrc_path = audio_path, lrc_path
        self.points = _load_points(lrc_path)
        self.low_indexes = set(low_indexes)
        self.current_index = min(self.low_indexes) if self.low_indexes else 0
        self.history: list[tuple[int, float, bool]] = []
        self.setWindowTitle("수동 가사 타이밍 매핑")
        self.resize(1040, 760)
        self.setModal(True)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setSource(QUrl.fromLocalFile(audio_path))

        root = QVBoxLayout(self)
        title = QLabel("음악을 재생하고 각 가사가 시작되는 순간 ‘현재 줄 기록’을 누르세요.")
        title.setObjectName("subtitle")
        root.addWidget(title)
        help_text = QLabel("Space 기록 · Ctrl+Space 재생/일시정지 · ↑/↓ 줄 이동 · ←/→ 0.1초 보정 · Shift+←/→ 5초 이동 · Backspace 실행 취소 · Enter 선택 타임으로 이동")
        help_text.setObjectName("hint")
        help_text.setWordWrap(True)
        root.addWidget(help_text)

        self.position_label = QLabel("00:00.00 / 00:00.00")
        root.addWidget(self.position_label)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(self.player.setPosition)
        root.addWidget(self.seek)

        playback = QHBoxLayout()
        self.play_button = QPushButton("재생")
        self.play_button.clicked.connect(self.toggle_playback)
        playback.addWidget(self.play_button)
        for label, delta in (("-5초", -5000), ("-1초", -1000), ("+1초", 1000), ("+5초", 5000)):
            button = QPushButton(label); button.setObjectName("secondary")
            button.clicked.connect(lambda _checked=False, value=delta: self.seek_relative(value))
            playback.addWidget(button)
        self.tap_button = QPushButton("현재 줄 기록  ·  Space")
        self.tap_button.clicked.connect(self.tap_current)
        playback.addWidget(self.tap_button, stretch=1)
        root.addLayout(playback)

        editing = QHBoxLayout()
        for label, callback in (("이전 줄", lambda: self.move_line(-1)), ("다음 줄", lambda: self.move_line(1)), ("선택 타임으로 이동", self.seek_to_current_line)):
            button = QPushButton(label); button.setObjectName("secondary"); button.clicked.connect(callback); editing.addWidget(button)
        for label, delta in (("-0.1초", -0.1), ("+0.1초", 0.1)):
            button = QPushButton(label); button.setObjectName("secondary")
            button.clicked.connect(lambda _checked=False, value=delta: self.nudge_current(value)); editing.addWidget(button)
        self.time_editor = QDoubleSpinBox(); self.time_editor.setRange(0, 86400); self.time_editor.setDecimals(2); self.time_editor.setSingleStep(0.1); self.time_editor.setSuffix(" 초")
        editing.addWidget(self.time_editor)
        direct = QPushButton("시간 직접 적용"); direct.setObjectName("secondary"); direct.clicked.connect(self.apply_time_editor); editing.addWidget(direct)
        undo = QPushButton("실행 취소"); undo.setObjectName("secondary"); undo.clicked.connect(self.undo_previous); editing.addWidget(undo)
        root.addLayout(editing)

        self.lines = QListWidget()
        self.lines.currentRowChanged.connect(self.select_row)
        self.lines.itemDoubleClicked.connect(lambda _item: self.seek_to_current_line())
        root.addWidget(self.lines, stretch=1)
        actions = QHBoxLayout(); actions.addStretch()
        cancel = QPushButton("취소"); cancel.setObjectName("secondary"); cancel.clicked.connect(self.reject); actions.addWidget(cancel)
        save = QPushButton("타이밍 저장 후 현재 곡 계속"); save.clicked.connect(self.save_and_accept); actions.addWidget(save)
        root.addLayout(actions)

        self.player.durationChanged.connect(lambda duration: self.seek.setRange(0, max(0, duration)))
        self.player.positionChanged.connect(self._position_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.timer = QTimer(self); self.timer.timeout.connect(self._refresh_position); self.timer.start(100)
        self._refresh_lines()

    def toggle_playback(self):
        self.player.pause() if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else self.player.play()

    def seek_relative(self, milliseconds: int):
        self.player.setPosition(min(max(0, self.player.duration()), max(0, self.player.position() + milliseconds)))

    def _remember(self):
        self.history.append((self.current_index, float(self.points[self.current_index]["time"]), bool(self.points[self.current_index].get("assigned"))))

    def tap_current(self):
        if not self.points: return
        self._remember(); self.points[self.current_index]["time"] = self.player.position() / 1000.0
        self.points[self.current_index]["assigned"] = True
        if self.current_index < len(self.points) - 1: self.current_index += 1
        self._refresh_lines()

    def select_row(self, row: int):
        if 0 <= row < len(self.points):
            self.current_index = row; self.time_editor.setValue(float(self.points[row]["time"]))

    def move_line(self, delta: int):
        if self.points:
            self.current_index = min(len(self.points) - 1, max(0, self.current_index + delta)); self._refresh_lines()

    def seek_to_current_line(self):
        if self.points: self.player.setPosition(int(float(self.points[self.current_index]["time"]) * 1000))

    def nudge_current(self, delta: float):
        if self.points:
            self._remember(); self.points[self.current_index]["time"] = max(0.0, float(self.points[self.current_index]["time"]) + delta); self.points[self.current_index]["assigned"] = True; self._refresh_lines()

    def apply_time_editor(self):
        if self.points:
            self._remember(); self.points[self.current_index]["time"] = self.time_editor.value(); self.points[self.current_index]["assigned"] = True; self._refresh_lines()

    def undo_previous(self):
        if self.history:
            index, old, assigned = self.history.pop(); self.points[index]["time"] = old; self.points[index]["assigned"] = assigned; self.current_index = index; self._refresh_lines()

    def _refresh_lines(self):
        self.lines.blockSignals(True); self.lines.clear()
        for index, point in enumerate(self.points):
            marker = "▶" if index == self.current_index else " "
            warning = "  ⚠ 확인 권장" if index in self.low_indexes else ""
            self.lines.addItem(f"{marker}  {_format_time(float(point['time']))}    {point['text']}{warning}")
        if self.points:
            self.lines.setCurrentRow(self.current_index); self.lines.scrollToItem(self.lines.currentItem()); self.time_editor.setValue(float(self.points[self.current_index]["time"]))
        self.lines.blockSignals(False)

    def _position_changed(self, position: int):
        if not self.seek.isSliderDown(): self.seek.setValue(position)

    def _refresh_position(self):
        self.position_label.setText(f"{_format_time(self.player.position()/1000)} / {_format_time(self.player.duration()/1000)}")

    def _playback_changed(self, state):
        self.play_button.setText("일시정지" if state == QMediaPlayer.PlaybackState.PlayingState else "재생")

    def save_and_accept(self):
        if not self.points:
            QMessageBox.warning(self, "가사 확인", "매핑할 가사 줄이 없습니다."); return
        unset = [i + 1 for i, point in enumerate(self.points) if not bool(point.get("assigned"))]
        if unset:
            QMessageBox.warning(self, "기록 확인", f"아직 시간이 없는 줄이 있습니다: {unset[:12]}"); return
        times = [float(point["time"]) for point in self.points]
        if any(a > b for a, b in zip(times, times[1:])):
            QMessageBox.warning(self, "순서 확인", "앞 줄보다 빠른 시간이 지정된 가사가 있습니다."); return
        with open(self.lrc_path, "w", encoding="utf-8") as file:
            for point in self.points: file.write(f"[{_format_time(float(point['time']))}] {point['text']}\n")
        self.player.stop(); self.accept()

    def keyPressEvent(self, event):  # noqa: N802
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key.Key_Space and mods & Qt.KeyboardModifier.ControlModifier: self.toggle_playback()
        elif key == Qt.Key.Key_Space: self.tap_current()
        elif key == Qt.Key.Key_Up: self.move_line(-1)
        elif key == Qt.Key.Key_Down: self.move_line(1)
        elif key == Qt.Key.Key_Left and mods & Qt.KeyboardModifier.ShiftModifier: self.seek_relative(-5000)
        elif key == Qt.Key.Key_Right and mods & Qt.KeyboardModifier.ShiftModifier: self.seek_relative(5000)
        elif key == Qt.Key.Key_Left: self.nudge_current(-0.1)
        elif key == Qt.Key.Key_Right: self.nudge_current(0.1)
        elif key == Qt.Key.Key_Backspace: self.undo_previous()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter): self.seek_to_current_line()
        else: super().keyPressEvent(event)

    def closeEvent(self, event):  # noqa: N802
        self.player.stop(); super().closeEvent(event)
