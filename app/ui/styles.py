"""Calm, high-contrast desktop theme for Lyric Video Maker."""

MODERN_STYLESHEET = """
QMainWindow, QWidget#appRoot { background-color: #0b0f17; color: #e7eaf0; }
QWidget { font-family: "Segoe UI", "Malgun Gothic"; font-size: 13px; }
QFrame#hero { background-color: #121824; border: 1px solid #222b3b; border-radius: 14px; }
QFrame#card { background-color: #111722; border: 1px solid #222b3b; border-radius: 12px; }
QLabel { color: #e7eaf0; background: transparent; }
QLabel#title { color: #f7f8fa; font-size: 25px; font-weight: 700; }
QLabel#eyebrow { color: #7c8aa5; font-size: 11px; font-weight: 700; }
QLabel#subtitle { color: #f0f2f6; font-size: 15px; font-weight: 650; }
QLabel#hint { color: #8f9bb0; font-size: 12px; }
QLabel#statusReady { color: #86efac; background-color: #10271b; border: 1px solid #1e4c32; border-radius: 9px; padding: 5px 10px; }
QLineEdit, QComboBox, QListWidget, QTextEdit { background-color: #0b1019; color: #e7eaf0; border: 1px solid #2a3548; border-radius: 8px; selection-background-color: #365cf5; selection-color: white; }
QLineEdit, QComboBox { min-height: 22px; padding: 8px 10px; }
QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTextEdit:focus { border: 1px solid #6682ff; }
QLineEdit:disabled, QComboBox:disabled { color: #667085; background: #10141c; }
QComboBox::drop-down { border: 0; width: 28px; }
QComboBox QAbstractItemView { background-color: #151c29; color: #e7eaf0; border: 1px solid #303b50; selection-background-color: #365cf5; padding: 4px; }
QListWidget, QTextEdit { padding: 8px; }
QListWidget::item { padding: 9px 8px; border-bottom: 1px solid #1d2533; }
QListWidget::item:selected { background-color: #243969; border-radius: 6px; }
QPushButton { min-height: 20px; color: white; background-color: #4f6df5; border: 1px solid #617cf6; border-radius: 8px; padding: 8px 15px; font-weight: 600; }
QPushButton:hover { background-color: #607bf7; }
QPushButton:pressed { background-color: #4059ce; }
QPushButton:disabled { color: #657086; background-color: #1b2230; border-color: #252e3e; }
QPushButton#secondary { background-color: #171e2b; border-color: #303b50; color: #d5dae4; }
QPushButton#secondary:hover { background-color: #202a3a; border-color: #46546d; }
QPushButton#danger { background-color: transparent; border-color: #51313a; color: #f2a5b3; }
QPushButton#danger:hover { background-color: #2b1820; }
QProgressBar { min-height: 10px; max-height: 10px; color: transparent; background-color: #202838; border: 0; border-radius: 5px; }
QProgressBar::chunk { background-color: #5d78f6; border-radius: 5px; }
QSplitter::handle { background-color: #0b0f17; width: 8px; }
QScrollBar:vertical { width: 9px; background: transparent; margin: 2px; }
QScrollBar::handle:vertical { min-height: 28px; background: #354158; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { color: #f7f8fa; background: #1b2331; border: 1px solid #3a465d; padding: 5px; }
"""
