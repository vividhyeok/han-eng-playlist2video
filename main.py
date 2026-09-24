"""Desktop entry point for Lyric Video Maker."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from app.config.paths import BASE_DIR
from app.ui.main_window import MainWindow


def run() -> int:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Lyric Video Maker")
    app.setOrganizationName("vividhyeok")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
