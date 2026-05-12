"""Track review dialog for metadata, lyrics editing, and manual sync."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget

from app.lyrics.lyric_text_utils import normalize_lyric_text, prepare_lyric_text_for_subtitles, summarize_lyric_text
from app.pipeline.playlist_importer import PlaylistReviewTrack
from app.sources.genie_handler import get_best_lyrics_result, lyrics_are_synced
from app.sources.youtube_handler import download_youtube_audio, sanitize_youtube_url
from app.ui.lyric_sync_dialog import LyricSyncDialog
from app.ui.styles import MODERN_STYLESHEET


class TrackReviewDialog(QDialog):
    track_preview_updated = pyqtSignal(object)

    def __init__(self, track: PlaylistReviewTrack, parent=None) -> None:
        super().__init__(parent)
        self.track = deepcopy(track)
        self.original_lyrics_text = track.lyrics_text
        self._dirty = False
        self._accepting = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(700)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._flush_autosave)
        self.setWindowTitle(f"곡 검토: {track.label}")
        self.resize(980, 860)
        self.setStyleSheet(MODERN_STYLESHEET)
        self._build_ui()
        self._load_track()
        self._bind_change_tracking()
        self._refresh_lyrics_summary()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QLabel("곡 검토 팝업")
        header.setObjectName("title")
        layout.addWidget(header)

        self.source_label = QLabel("")
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.note_label = QLabel("")
        self.note_label.setObjectName("hint")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        info_card = QWidget()
        info_layout = QGridLayout(info_card)
        info_layout.addWidget(QLabel("제목"), 0, 0)
        self.title_input = QLineEdit()
        info_layout.addWidget(self.title_input, 0, 1)
        info_layout.addWidget(QLabel("아티스트"), 0, 2)
        self.artist_input = QLineEdit()
        info_layout.addWidget(self.artist_input, 0, 3)
        info_layout.addWidget(QLabel("앨범"), 1, 0)
        self.album_input = QLineEdit()
        info_layout.addWidget(self.album_input, 1, 1)
        info_layout.addWidget(QLabel("길이"), 1, 2)
        self.duration_label = QLabel("-")
        info_layout.addWidget(self.duration_label, 1, 3)
        info_layout.addWidget(QLabel("앨범아트 URL/경로"), 2, 0)
        self.album_art_input = QLineEdit()
        info_layout.addWidget(self.album_art_input, 2, 1, 1, 3)
        info_layout.addWidget(QLabel("YouTube URL"), 3, 0)
        self.youtube_input = QLineEdit()
        info_layout.addWidget(self.youtube_input, 3, 1, 1, 3)
        layout.addWidget(info_card)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=1)

        self.tabs.addTab(self._build_lyrics_tab(), "가사 편집")
        self.tabs.addTab(self._build_sync_tab(), "싱크 작업")

        footer = QHBoxLayout()
        self.autosave_label = QLabel("이 창에서 바꾸는 내용은 자동 저장됩니다.")
        self.autosave_label.setObjectName("hint")
        self.autosave_label.setWordWrap(True)
        footer.addWidget(self.autosave_label, stretch=1)

        self.save_button = QPushButton("닫기")
        self.save_button.clicked.connect(self._accept_dialog)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def _build_lyrics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel("가사를 붙여넣거나 정리하면 자동 저장됩니다. 싱크 LRC가 있으면 그대로 유지됩니다.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.lyrics_text_edit = QTextEdit()
        self.lyrics_text_edit.textChanged.connect(self._refresh_lyrics_summary)
        layout.addWidget(self.lyrics_text_edit, stretch=1)

        tools = QHBoxLayout()
        auto_fetch_button = QPushButton("자동 가사 찾기")
        auto_fetch_button.clicked.connect(self.fetch_lyrics_automatically)
        tools.addWidget(auto_fetch_button)

        normalize_button = QPushButton("줄바꿈 정리")
        normalize_button.clicked.connect(self._normalize_lyrics)
        tools.addWidget(normalize_button)

        split_button = QPushButton("긴 줄 나누기")
        split_button.clicked.connect(self._split_lyrics)
        tools.addWidget(split_button)

        restore_button = QPushButton("원문 복원")
        restore_button.clicked.connect(self._restore_lyrics)
        tools.addWidget(restore_button)

        clear_button = QPushButton("가사 비우기")
        clear_button.clicked.connect(self._clear_lyrics)
        tools.addWidget(clear_button)
        layout.addLayout(tools)

        self.lyrics_summary_label = QLabel("")
        self.lyrics_summary_label.setWordWrap(True)
        layout.addWidget(self.lyrics_summary_label)

        self.fetch_result_label = QLabel("")
        self.fetch_result_label.setObjectName("hint")
        self.fetch_result_label.setWordWrap(True)
        layout.addWidget(self.fetch_result_label)
        return page

    def _build_sync_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "이 탭에서는 현재 가사 텍스트를 들고 선택한 YouTube 오디오 위에 직접 시점을 찍습니다. "
            "가사가 없거나 YouTube URL이 비어 있으면 먼저 위 탭에서 정리하세요."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.sync_ready_label = QLabel("")
        self.sync_ready_label.setWordWrap(True)
        layout.addWidget(self.sync_ready_label)

        self.sync_preview_label = QLabel("")
        self.sync_preview_label.setObjectName("hint")
        self.sync_preview_label.setWordWrap(True)
        layout.addWidget(self.sync_preview_label)

        self.sync_preview_text = QTextEdit()
        self.sync_preview_text.setReadOnly(True)
        layout.addWidget(self.sync_preview_text, stretch=1)

        sync_actions = QHBoxLayout()
        self.start_sync_button = QPushButton("팝업에서 싱크 매핑 시작")
        self.start_sync_button.clicked.connect(self.start_sync_mapping)
        sync_actions.addWidget(self.start_sync_button)
        layout.addLayout(sync_actions)

        return page

    def _load_track(self) -> None:
        self.source_label.setText(f"원본 {self.track.source.index}번: {self.track.source.label}")
        self.status_label.setText(f"현재 상태: {self._status_text(self.track)}")
        self.note_label.setText(f"메모: {self.track.note or '-'}")
        self.title_input.setText(self.track.title)
        self.artist_input.setText(self.track.artist)
        self.album_input.setText(self.track.album)
        self.album_art_input.setText(self.track.album_art_url)
        self.youtube_input.setText(self.track.youtube_url)
        self.duration_label.setText(self._format_duration(self.track.duration))
        self.lyrics_text_edit.setPlainText(self.track.lyrics_text)
        self.fetch_result_label.setText("")
        self.autosave_label.setText("이 창에서 바꾸는 내용은 자동 저장됩니다.")

    def _refresh_lyrics_summary(self) -> None:
        lyrics_text = self.lyrics_text_edit.toPlainText().strip()
        stripped_text = self._strip_lrc_timestamps(lyrics_text)
        summary = summarize_lyric_text(stripped_text)
        mode = "싱크" if lyrics_are_synced(lyrics_text) else "일반" if lyrics_text else "없음"
        path = self.track.lrc_path or "없음"
        self.lyrics_summary_label.setText(
            f"가사 상태: {mode} | 줄 수: {summary.line_count} | 긴 줄: {summary.long_line_count} | 현재 파일: {path}"
        )
        self.sync_ready_label.setText(
            f"YouTube URL {'준비됨' if self.youtube_input.text().strip() else '없음'} | "
            f"가사 줄 수 {summary.line_count}줄 | 렌더 {'가능' if mode == '싱크' else '불가'}"
        )
        preview_lines = lyrics_text.splitlines()
        if len(preview_lines) > 18:
            preview_lines = preview_lines[:18] + ["", "..."]
        self.sync_preview_label.setText("싱크 매핑에 들어갈 현재 가사 미리보기")
        self.sync_preview_text.setPlainText("\n".join(preview_lines))

    def _normalize_lyrics(self) -> None:
        self.lyrics_text_edit.setPlainText(normalize_lyric_text(self.lyrics_text_edit.toPlainText()))

    def _split_lyrics(self) -> None:
        self.lyrics_text_edit.setPlainText(prepare_lyric_text_for_subtitles(self.lyrics_text_edit.toPlainText()))

    def _restore_lyrics(self) -> None:
        self.lyrics_text_edit.setPlainText(self.original_lyrics_text)

    def _clear_lyrics(self) -> None:
        self.lyrics_text_edit.clear()

    def fetch_lyrics_automatically(self) -> None:
        title = self.title_input.text().strip()
        artist = self.artist_input.text().strip()
        youtube_url = sanitize_youtube_url(self.youtube_input.text().strip()) or self.youtube_input.text().strip()
        if not title or not artist:
            QMessageBox.warning(self, "기본 정보 부족", "제목과 아티스트를 먼저 입력하세요.")
            return

        self.fetch_result_label.setText("자동 가사 검색 중...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = get_best_lyrics_result(
                title=title,
                artist=artist,
                album=self.album_input.text().strip(),
                duration=self.track.duration,
                youtube_url=youtube_url,
                allow_fuzzy_match=False,
            )
        finally:
            QApplication.restoreOverrideCursor()

        if not result:
            self.fetch_result_label.setText("자동으로 찾은 가사가 없습니다.")
            QMessageBox.information(self, "가사 없음", "추가 소스까지 확인했지만 가사를 찾지 못했습니다.")
            return

        if result.lyrics_mode == "plain":
            action = self._ask_plain_lyrics_action(result.source)
            if action == "cancel":
                self.fetch_result_label.setText("자동 가사 적용을 취소했습니다.")
                return
            self.lyrics_text_edit.setPlainText(result.text)
            self.track.lyrics_source = "auto_fetch"
            self._apply_live_preview()
            self.fetch_result_label.setText(f"{result.source}에서 일반 가사를 가져왔습니다. 싱크 작업 전에는 렌더되지 않습니다.")
            self.tabs.setCurrentIndex(0)
            if action == "review":
                QMessageBox.information(self, "가사 불러옴", "일반 가사를 편집기에 넣었습니다. 이 곡은 싱크 작업을 마쳐야 렌더할 수 있습니다.")
            else:
                QMessageBox.information(self, "가사 불러옴", "자동으로 찾은 일반 가사를 넣었습니다. 이 곡은 싱크 작업을 마쳐야 렌더할 수 있습니다.")
            return

        self.lyrics_text_edit.setPlainText(result.text)
        self.track.lyrics_source = "auto_fetch"
        self._apply_live_preview()
        self.fetch_result_label.setText(f"{result.source}에서 싱크 가사를 가져왔고 메인 목록에도 바로 반영했습니다.")
        self.tabs.setCurrentIndex(0)
        QMessageBox.information(self, "가사 불러옴", f"{result.source}에서 싱크 가사를 가져왔고 메인 목록에도 바로 반영했습니다.")

    def start_sync_mapping(self) -> None:
        youtube_url = sanitize_youtube_url(self.youtube_input.text().strip())
        if not youtube_url:
            QMessageBox.warning(self, "YouTube URL 없음", "싱크 매핑 전에 YouTube URL을 입력하세요.")
            self.tabs.setCurrentIndex(0)
            return

        lyrics_text = self._strip_lrc_timestamps(self.lyrics_text_edit.toPlainText().strip())
        if not lyrics_text:
            QMessageBox.warning(self, "가사 없음", "싱크 매핑 전에 가사를 먼저 정리하세요.")
            self.tabs.setCurrentIndex(0)
            return

        safe_title = re.sub(r'[\\/*?:"<>|]', "_", self.title_input.text().strip() or "track")
        audio_name = f"manual_sync_{self.track.source.index:02d}_{safe_title}.mp3"
        self.sync_ready_label.setText("오디오 다운로드 중...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            audio_path = download_youtube_audio(youtube_url, audio_name)
        finally:
            QApplication.restoreOverrideCursor()

        if not audio_path or not os.path.exists(audio_path):
            self.sync_ready_label.setText("오디오 다운로드 실패")
            QMessageBox.warning(self, "오디오 다운로드 실패", "싱크 매핑에 사용할 오디오를 받지 못했습니다.")
            return

        dialog = LyricSyncDialog(audio_path, lyrics_text, self)
        if not dialog.exec():
            self.sync_ready_label.setText("싱크 매핑이 취소되었습니다.")
            return

        lrc_content = dialog.get_lrc_content()
        if not lrc_content:
            self.sync_ready_label.setText("싱크 LRC가 생성되지 않았습니다.")
            return

        self.track.lyrics_source = "manual_sync"
        self.lyrics_text_edit.setPlainText(lrc_content)
        self._apply_live_preview()
        self.sync_ready_label.setText("싱크 LRC를 만들었고 메인 목록에도 바로 반영했습니다.")
        self.tabs.setCurrentIndex(0)

    def _accept_dialog(self) -> None:
        if not self.title_input.text().strip():
            QMessageBox.warning(self, "제목 없음", "제목을 입력하세요.")
            return
        if not self.artist_input.text().strip():
            QMessageBox.warning(self, "아티스트 없음", "아티스트를 입력하세요.")
            return
        if self._dirty or self._autosave_timer.isActive():
            self._flush_autosave()
        self._accepting = True
        self.accept()

    def get_review_track(self) -> PlaylistReviewTrack:
        updated = deepcopy(self.track)
        updated.title = self.title_input.text().strip()
        updated.artist = self.artist_input.text().strip()
        updated.album = self.album_input.text().strip()
        updated.album_art_url = self.album_art_input.text().strip()
        updated.youtube_url = sanitize_youtube_url(self.youtube_input.text().strip()) or self.youtube_input.text().strip()
        updated.lyrics_text = self.lyrics_text_edit.toPlainText().strip()
        updated.include_in_batch = self.track.include_in_batch
        if not updated.lyrics_text:
            updated.lyrics_source = "none"
        elif updated.lyrics_source not in {"manual_sync", "auto_fetch"}:
            updated.lyrics_source = "generated"
        return updated

    def _apply_live_preview(self) -> None:
        self._autosave_timer.stop()
        updated = self.get_review_track()
        self.track = deepcopy(updated)
        self.original_lyrics_text = updated.lyrics_text
        self._dirty = False
        self.autosave_label.setText(f"자동 저장됨 · {datetime.now().strftime('%H:%M:%S')}")
        self.track_preview_updated.emit(updated)

    @staticmethod
    def _strip_lrc_timestamps(text: str) -> str:
        return re.sub(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]", "", text or "").strip()

    @staticmethod
    def _format_duration(duration: int | None) -> str:
        if not isinstance(duration, int):
            return "없음"
        return f"{duration // 60:02d}:{duration % 60:02d}"

    @staticmethod
    def _status_text(track: PlaylistReviewTrack) -> str:
        if track.status == "ready":
            return "렌더 준비됨"
        if track.status == "plain_lyrics":
            return "싱크 필요"
        if track.status == "missing_lyrics":
            return "가사 없음"
        if track.status == "incomplete":
            return "필수 정보 부족"
        if track.status == "error":
            return "자동 해석 실패"
        return track.status

    def _bind_change_tracking(self) -> None:
        for widget in [
            self.title_input,
            self.artist_input,
            self.album_input,
            self.album_art_input,
            self.youtube_input,
        ]:
            widget.textChanged.connect(self._mark_dirty)
        self.lyrics_text_edit.textChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args) -> None:
        self._dirty = True
        self.autosave_label.setText("자동 저장 대기 중...")
        self._autosave_timer.start()

    def _flush_autosave(self) -> None:
        if self._dirty:
            self._apply_live_preview()

    def _ask_plain_lyrics_action(self, source_name: str) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("일반 가사 찾음")
        box.setText(f"{source_name}에서 일반 가사를 찾았습니다.")
        box.setInformativeText("그대로 쓸지, 살짝 고칠지만 고르세요. 수정해도 자동 저장됩니다.")
        accept_button = box.addButton("바로 사용", QMessageBox.ButtonRole.AcceptRole)
        review_button = box.addButton("검토 후 수정", QMessageBox.ButtonRole.ActionRole)
        cancel_button = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == accept_button:
            return "accept"
        if clicked == review_button:
            return "review"
        if clicked == cancel_button:
            return "cancel"
        return "cancel"

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if self._accepting:
            super().closeEvent(event)
            return
        if self._dirty or self._autosave_timer.isActive():
            self._flush_autosave()
        event.accept()
