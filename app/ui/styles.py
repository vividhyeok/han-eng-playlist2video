"""
Modern Styles for Lyric Video Maker GUI
Dark theme with glassmorphism and smooth animations
"""

MODERN_STYLESHEET = """
/* Main Window */
QMainWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 #1a1a2e, stop:1 #16213e);
}

/* Scroll Area */
QScrollArea {
    border: none;
    background: transparent;
}

/* Card Style Frames */
QFrame#card {
    background: rgba(255, 255, 255, 0.05);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    padding: 15px;
}

QFrame#card:hover {
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.2);
}

/* Input Fields */
QLineEdit {
    background: rgba(255, 255, 255, 0.1);
    border: 2px solid rgba(255, 255, 255, 0.2);
    border-radius: 8px;
    padding: 10px 15px;
    color: #ffffff;
    font-size: 14px;
    selection-background-color: #0f3460;
}

QLineEdit:focus {
    border: 2px solid #00d4ff;
    background: rgba(255, 255, 255, 0.15);
}

QLineEdit:hover {
    background: rgba(255, 255, 255, 0.12);
}

/* Buttons */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #00d4ff, stop:1 #0099cc);
    border: none;
    border-radius: 8px;
    padding: 12px 24px;
    color: white;
    font-size: 14px;
    font-weight: bold;
}

QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #00e5ff, stop:1 #00aadd);
}

QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #00b3cc, stop:1 #008899);
}

QPushButton:disabled {
    background: rgba(255, 255, 255, 0.1);
    color: rgba(255, 255, 255, 0.3);
}

/* Secondary Button */
QPushButton#secondary {
    background: rgba(255, 255, 255, 0.1);
    border: 2px solid rgba(255, 255, 255, 0.2);
}

QPushButton#secondary:hover {
    background: rgba(255, 255, 255, 0.15);
    border: 2px solid rgba(255, 255, 255, 0.3);
}

/* Danger Button */
QPushButton#danger {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #ff4757, stop:1 #cc3a47);
}

QPushButton#danger:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #ff5868, stop:1 #dd4b58);
}

/* Labels */
QLabel {
    color: #ffffff;
    font-size: 14px;
}

QLabel#title {
    font-size: 24px;
    font-weight: bold;
    color: #00d4ff;
}

QLabel#subtitle {
    font-size: 16px;
    font-weight: bold;
    color: #ffffff;
}

QLabel#hint {
    font-size: 12px;
    color: rgba(255, 255, 255, 0.6);
}

/* ComboBox */
QComboBox {
    background: rgba(255, 255, 255, 0.1);
    border: 2px solid rgba(255, 255, 255, 0.2);
    border-radius: 8px;
    padding: 10px 15px;
    color: #ffffff;
    font-size: 14px;
}

QComboBox:hover {
    background: rgba(255, 255, 255, 0.15);
    border: 2px solid rgba(255, 255, 255, 0.3);
}

QComboBox:focus {
    border: 2px solid #00d4ff;
}

QComboBox::drop-down {
    border: none;
    width: 30px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 5px solid #ffffff;
    margin-right: 10px;
}

QComboBox QAbstractItemView {
    background: #1a1a2e;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 8px;
    selection-background-color: #0f3460;
    color: #ffffff;
    padding: 5px;
}

/* Radio Buttons */
QRadioButton {
    color: #ffffff;
    font-size: 14px;
    spacing: 8px;
}

QRadioButton::indicator {
    width: 20px;
    height: 20px;
    border-radius: 10px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    background: rgba(255, 255, 255, 0.1);
}

QRadioButton::indicator:checked {
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                                fx:0.5, fy:0.5,
                                stop:0 #00d4ff, stop:0.5 #00d4ff, stop:0.6 transparent);
    border: 2px solid #00d4ff;
}

QRadioButton::indicator:hover {
    border: 2px solid #00d4ff;
}

/* CheckBox */
QCheckBox {
    color: #ffffff;
    font-size: 14px;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 20px;
    height: 20px;
    border-radius: 4px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    background: rgba(255, 255, 255, 0.1);
}

QCheckBox::indicator:checked {
    background: #00d4ff;
    border: 2px solid #00d4ff;
}

QCheckBox::indicator:hover {
    border: 2px solid #00d4ff;
}

/* Scroll Bar */
QScrollBar:vertical {
    background: rgba(255, 255, 255, 0.05);
    width: 12px;
    border-radius: 6px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 0.2);
    border-radius: 6px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: rgba(255, 255, 255, 0.3);
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    background: rgba(255, 255, 255, 0.05);
    height: 12px;
    border-radius: 6px;
    margin: 0px;
}

QScrollBar::handle:horizontal {
    background: rgba(255, 255, 255, 0.2);
    border-radius: 6px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background: rgba(255, 255, 255, 0.3);
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* Progress Bar */
QProgressBar {
    background: rgba(255, 255, 255, 0.1);
    border: none;
    border-radius: 8px;
    text-align: center;
    color: #ffffff;
    font-weight: bold;
}

QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #00d4ff, stop:1 #0099cc);
    border-radius: 8px;
}

/* Menu Bar */
QMenuBar {
    background: rgba(255, 255, 255, 0.05);
    color: #ffffff;
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

QMenuBar::item {
    padding: 8px 16px;
    background: transparent;
}

QMenuBar::item:selected {
    background: rgba(255, 255, 255, 0.1);
}

QMenu {
    background: #1a1a2e;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 8px;
    padding: 5px;
}

QMenu::item {
    padding: 8px 24px;
    color: #ffffff;
    border-radius: 4px;
}

QMenu::item:selected {
    background: rgba(0, 212, 255, 0.2);
}

/* Tooltips */
QToolTip {
    background: #1a1a2e;
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 4px;
    padding: 5px;
}
"""
