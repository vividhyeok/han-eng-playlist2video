"""Manual and external-AI review for ambiguous lyric translations."""
from __future__ import annotations

import json
import re

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

def apply_manual_translations(json_path: str, translations: dict[int, str]) -> None:
    with open(json_path, "r", encoding="utf-8") as file:
        entries = json.load(file)
    if not isinstance(entries, list):
        raise ValueError("번역 결과 파일 형식이 올바르지 않습니다.")
    for index, translated in translations.items():
        if not 0 <= index < len(entries) or not isinstance(entries[index], dict):
            raise IndexError(f"가사 줄 번호가 올바르지 않습니다: {index}")
        value = str(translated).strip()
        if not value:
            raise ValueError(f"{index}번 줄의 번역이 비어 있습니다.")
        entries[index]["english"] = value
        entries[index].pop("translation_review", None)
        entries[index]["translation_meta"] = {
            "translated": value, "confidence": 1.0, "needs_review": False,
            "ambiguity_question": "", "model": "manual",
        }
    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)

def parse_external_translation_json(text: str, allowed_indexes: set[int]) -> dict[int, str]:
    candidate = text.strip()
    fenced = FENCED_JSON.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    data = json.loads(candidate)
    if isinstance(data, dict):
        data = data.get("translations", data.get("lines", data))
    result: dict[int, str] = {}
    if isinstance(data, dict):
        iterator = [{"index": key, "english": value} for key, value in data.items()]
    elif isinstance(data, list):
        iterator = data
    else:
        raise ValueError("JSON은 translations 배열 또는 index: 번역 객체여야 합니다.")
    for item in iterator:
        if not isinstance(item, dict):
            raise ValueError("각 번역 항목은 JSON 객체여야 합니다.")
        index = int(item.get("index"))
        english = str(item.get("english", item.get("translation", item.get("translated", "")))).strip()
        if index not in allowed_indexes:
            raise ValueError(f"요청하지 않은 index가 포함되었습니다: {index}")
        if index in result:
            raise ValueError(f"중복 index가 포함되었습니다: {index}")
        if not english:
            raise ValueError(f"{index}번 번역이 비어 있습니다.")
        result[index] = english
    missing = sorted(allowed_indexes - set(result))
    if missing:
        raise ValueError(f"빠진 index가 있습니다: {missing}")
    return result

def build_external_review_prompt(json_path: str, issues: list[dict], artist: str, title: str) -> str:
    with open(json_path, "r", encoding="utf-8") as file:
        entries = json.load(file)
    requested = []
    for issue in issues:
        index = int(issue["index"])
        start, end = max(0, index - 4), min(len(entries), index + 5)
        requested.append({
            "index": index,
            "source": issue.get("source", entries[index].get("original", "")),
            "current_translation": issue.get("translated", entries[index].get("english", "")),
            "question": issue.get("question", ""),
            "nearby_context": [
                {"index": i, "korean": entries[i].get("original", ""), "current_english": entries[i].get("english", "")}
                for i in range(start, end)
            ],
        })
    payload = json.dumps({"artist": artist, "title": title, "review_lines": requested}, ensure_ascii=False, indent=2)
    return f"""당신은 한국 힙합 가사를 영어 자막으로 번역하는 검수자입니다.

아티스트: {artist}
곡 제목: {title}

아래 문제 구절의 의미를 확정해 주세요. 웹 검색이 가능하면 반드시 곡 제목, 아티스트, 공식/신뢰 가능한 가사, 인터뷰, 고유명사와 슬랭을 검색하여 확인하세요. 검색으로 확인되지 않는 내용을 지어내지 마세요.

각 줄을 따로 해석하지 말고 nearby_context의 연속된 여러 줄에서 먼저 한 문장/절을 묶으세요. 한국 랩의 도치, 뒤늦게 나오는 서술어, 앞줄의 목적어, 생략된 주어·조사, 다음 줄로 이어지는 원인·비교·부정 관계를 정상 한국어 어순으로 복원한 뒤 영어 의미를 원래 index 순서에 다시 분배하세요. 두 줄에 같은 뜻을 반복하거나 한 줄의 수식어를 버리지 마세요. 구체적인 이미지와 행동을 근거 없이 something/things/it 같은 말로 뭉개지 마세요. 기존 영어·브랜드·인명·크루명·말장난·욕설의 강도를 보존하고, 연속해서 읽었을 때 완전한 생각이 되는 짧은 영어 자막으로 번역하세요.

반환 규칙:
- 설명이나 출처 목록을 출력하지 마세요.
- 아래 요청된 index를 정확히 한 번씩 모두 반환하세요.
- index 숫자를 바꾸거나 줄을 합치지 마세요.
- 반드시 다음 JSON 형식 하나만 코드블록으로 출력하세요.

```json
{{
  "translations": [
    {{"index": 0, "english": "Final English subtitle"}}
  ]
}}
```

검수 데이터:
{payload}
"""

class TranslationReviewDialog(QDialog):
    def __init__(self, *, json_path: str, issues: list[dict], artist: str = "", title: str = "", parent=None):
        super().__init__(parent)
        self.json_path, self.issues = json_path, issues
        self.artist, self.title = artist, title
        self.inputs: dict[int, QLineEdit] = {}
        self.allowed_indexes = {int(issue["index"]) for issue in issues}
        self.setWindowTitle("번역 문제 구절 검수")
        self.resize(980, min(860, 360 + len(issues) * 120))
        self.setModal(True)

        root = QVBoxLayout(self)
        title_label = QLabel(f"{artist} - {title} · 확인이 필요한 번역 {len(issues)}개")
        title_label.setObjectName("subtitle"); root.addWidget(title_label)
        tabs = QTabWidget(); root.addWidget(tabs, stretch=1)

        manual = QWidget(); manual_layout = QVBoxLayout(manual)
        note = QLabel("현재 번역을 직접 고친 다음 저장하면 현재 곡 렌더링을 이어갑니다.")
        note.setObjectName("hint"); manual_layout.addWidget(note)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content = QWidget(); content_layout = QVBoxLayout(content); content_layout.setContentsMargins(0, 0, 8, 0)
        for number, issue in enumerate(issues, start=1):
            index = int(issue.get("index", number - 1))
            card = QFrame(); card.setObjectName("card"); layout = QVBoxLayout(card)
            source = QLabel(f"{number}. [{index}] {issue.get('source', '')}"); source.setObjectName("subtitle"); source.setWordWrap(True); layout.addWidget(source)
            if str(issue.get("question", "")).strip():
                question = QLabel(f"확인할 점: {issue['question']}"); question.setObjectName("hint"); question.setWordWrap(True); layout.addWidget(question)
            editor = QLineEdit(str(issue.get("translated", ""))); editor.setPlaceholderText("최종 영어 번역"); layout.addWidget(editor)
            self.inputs[index] = editor; content_layout.addWidget(card)
        content_layout.addStretch(); scroll.setWidget(content); manual_layout.addWidget(scroll, stretch=1)
        tabs.addTab(manual, "직접 수정")

        external = QWidget(); external_layout = QVBoxLayout(external)
        external_note = QLabel("프롬프트를 외부 AI에 붙여 넣고, AI가 반환한 JSON 코드블록 전체를 아래에 붙여 넣으세요.")
        external_note.setObjectName("hint"); external_note.setWordWrap(True); external_layout.addWidget(external_note)
        copy_prompt = QPushButton("검색 검수 프롬프트 전체 복사"); copy_prompt.clicked.connect(self.copy_external_prompt); external_layout.addWidget(copy_prompt)
        self.external_result = QTextEdit(); self.external_result.setPlaceholderText('```json\n{"translations":[{"index": 3, "english": "..."}]}\n```')
        external_layout.addWidget(self.external_result, stretch=1)
        apply_json = QPushButton("붙여 넣은 JSON 검증 및 입력칸에 적용"); apply_json.clicked.connect(self.apply_external_json_to_inputs); external_layout.addWidget(apply_json)
        tabs.addTab(external, "외부 AI 일괄 검수")

        actions = QHBoxLayout(); actions.addStretch()
        cancel = QPushButton("취소"); cancel.setObjectName("secondary"); cancel.clicked.connect(self.reject); actions.addWidget(cancel)
        save = QPushButton("검수 번역 저장 후 현재 곡 계속"); save.clicked.connect(self.save_and_accept); actions.addWidget(save)
        root.addLayout(actions)

    def copy_external_prompt(self):
        try:
            prompt = build_external_review_prompt(self.json_path, self.issues, self.artist, self.title)
            QGuiApplication.clipboard().setText(prompt)
            QMessageBox.information(self, "복사 완료", "외부 AI용 검색 검수 프롬프트를 클립보드에 복사했습니다.")
        except Exception as exc:
            QMessageBox.critical(self, "프롬프트 생성 실패", str(exc))

    def apply_external_json_to_inputs(self):
        try:
            translations = parse_external_translation_json(self.external_result.toPlainText(), self.allowed_indexes)
        except Exception as exc:
            QMessageBox.warning(self, "JSON 확인", str(exc)); return
        for index, value in translations.items():
            self.inputs[index].setText(value)
        QMessageBox.information(self, "적용 완료", f"번역 {len(translations)}개를 입력칸에 적용했습니다. 내용을 확인하고 저장하세요.")

    def save_and_accept(self):
        translations = {index: editor.text().strip() for index, editor in self.inputs.items()}
        missing = [index for index, text in translations.items() if not text]
        if missing:
            QMessageBox.warning(self, "번역 확인", f"비어 있는 번역 index: {missing}"); return
        try:
            apply_manual_translations(self.json_path, translations)
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc)); return
        self.accept()
