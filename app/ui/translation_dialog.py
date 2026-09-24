"""Manual review dialog for ambiguous lyric translations."""
from __future__ import annotations

import json

from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)


def apply_manual_translations(json_path: str, translations: dict[int, str]) -> None:
    with open(json_path, "r", encoding="utf-8") as file:
        entries = json.load(file)
    if not isinstance(entries, list):
        raise ValueError("번역 결과 파일 형식이 올바르지 않습니다.")
    for index, translated in translations.items():
        if not 0 <= index < len(entries) or not isinstance(entries[index], dict):
            raise IndexError(f"가사 줄 번호가 올바르지 않습니다: {index}")
        entries[index]["english"] = translated.strip()
        entries[index].pop("translation_review", None)
        entries[index]["translation_meta"] = {
            "translated": translated.strip(),
            "confidence": 1.0,
            "needs_review": False,
            "ambiguity_question": "",
            "model": "manual",
        }
    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)


class TranslationReviewDialog(QDialog):
    def __init__(self, *, json_path: str, issues: list[dict], parent=None):
        super().__init__(parent)
        self.json_path = json_path
        self.issues = issues
        self.inputs: dict[int, QLineEdit] = {}

        self.setWindowTitle("번역 구절 직접 확인")
        self.resize(900, min(820, 260 + len(issues) * 150))
        self.setModal(True)

        root = QVBoxLayout(self)
        title = QLabel(f"AI가 의미를 확정하지 못한 {len(issues)}개 구절을 직접 확인해 주세요.")
        title.setObjectName("subtitle")
        root.addWidget(title)
        note = QLabel("영문 번역을 원하는 표현으로 고친 뒤 저장하면 현재 곡 렌더링을 이어갑니다.")
        note.setObjectName("hint")
        root.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)

        for number, issue in enumerate(issues, start=1):
            index = int(issue.get("index", number - 1))
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            source = QLabel(f"{number}. {issue.get('source', '')}")
            source.setObjectName("subtitle")
            source.setWordWrap(True)
            layout.addWidget(source)
            question_text = str(issue.get("question", "")).strip()
            if question_text:
                question = QLabel(f"확인할 점: {question_text}")
                question.setObjectName("hint")
                question.setWordWrap(True)
                layout.addWidget(question)
            editor = QLineEdit(str(issue.get("translated", "")))
            editor.setPlaceholderText("최종 영문 번역을 입력하세요")
            layout.addWidget(editor)
            self.inputs[index] = editor
            content_layout.addWidget(card)
        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("수정 저장 후 다시 렌더")
        save.clicked.connect(self.save_and_accept)
        actions.addWidget(save)
        root.addLayout(actions)

    def save_and_accept(self) -> None:
        translations = {
            index: editor.text().strip()
            for index, editor in self.inputs.items()
        }
        missing = [index + 1 for index, text in translations.items() if not text]
        if missing:
            QMessageBox.warning(
                self,
                "번역 확인",
                "비어 있는 번역이 있습니다: " + ", ".join(map(str, missing)),
            )
            return
        try:
            apply_manual_translations(self.json_path, translations)
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))
            return
        self.accept()
