"""Application theme: Fusion style, light palette and compact stylesheet."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#2f6fed"
ACCENT_DARK = "#1f57c3"
SUCCESS = "#1a7f37"
WARNING = "#b26a00"
ERROR = "#c62828"
MUTED = "#6b7280"


def _pick_font() -> QFont:
    families = set(QFontDatabase.families())
    for candidate in (
        "Inter",
        "Noto Sans",
        "Adwaita Sans",
        "DejaVu Sans",
        "Liberation Sans",
        "FreeSans",
        "Nimbus Sans",
        "Segoe UI",
    ):
        if candidate in families:
            return QFont(candidate, 9)
    font = QApplication.font()
    font.setPointSize(9)
    return font


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(_pick_font())

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#f3f5f8"))
    palette.setColor(QPalette.WindowText, QColor("#202631"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f8f9fb"))
    palette.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipText, QColor("#202631"))
    palette.setColor(QPalette.Text, QColor("#202631"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#202631"))
    palette.setColor(QPalette.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.Link, QColor(ACCENT))
    palette.setColor(QPalette.Highlight, QColor("#dbe7ff"))
    palette.setColor(QPalette.HighlightedText, QColor("#14233b"))
    palette.setColor(QPalette.PlaceholderText, QColor("#8a93a0"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#9aa1ab"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#9aa1ab"))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)


STYLESHEET = """
QMainWindow, QDialog {
    background: #f3f5f8;
}
QWidget {
    color: #202631;
}
QToolBar {
    background: #ffffff;
    border: none;
    border-bottom: 1px solid #dde2e9;
    spacing: 4px;
    padding: 7px 10px;
}
QToolBar::separator {
    background: #e4e8ee;
    width: 1px;
    margin: 4px 7px;
}
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 10px;
    color: #2d333b;
    font-weight: 500;
    font-style: normal;
}
QToolButton:hover {
    background: #eef2f8;
    border-color: #dce3ec;
}
QToolButton:pressed {
    background: #e3e9f3;
}
QToolButton:disabled {
    color: #a4abb5;
}
QToolButton#runAction {
    background: #2f6fed;
    color: #ffffff;
    border: 1px solid #2f6fed;
    font-weight: 600;
    padding: 6px 14px;
}
QToolButton#runAction:hover {
    background: #1f57c3;
    border-color: #1f57c3;
}
QToolButton#runAction:disabled {
    background: #c8d6f4;
    border-color: #c8d6f4;
    color: #f4f7ff;
}
QToolButton#stopAction {
    color: #b42318;
    border: 1px solid #efd2ce;
    background: #fff7f6;
    padding: 6px 12px;
}
QToolButton#stopAction:hover {
    background: #fdecea;
    border-color: #e6aba3;
}
QToolButton#stopAction:disabled {
    color: #b9a5a2;
    background: #fbf7f6;
    border-color: #f0e3e1;
}
QToolButton#pauseAction {
    color: #8a5a00;
    border: 1px solid #f0d9a8;
    background: #fff9ec;
    padding: 6px 12px;
}
QToolButton#pauseAction:hover {
    background: #fdf1d7;
    border-color: #e3c47d;
}
QToolButton#pauseAction:disabled {
    color: #b3a58c;
    background: #fbf9f4;
    border-color: #f0e7d5;
}
QToolButton#smallButton {
    padding: 3px 7px;
    border-radius: 5px;
    color: #5d6572;
}
QToolButton#smallButton:hover {
    background: #eef2f8;
}
QMenuBar {
    background: #ffffff;
    border-bottom: 1px solid #e5e9ef;
    padding: 1px 4px;
}
QMenuBar::item {
    padding: 5px 10px;
    border-radius: 4px;
    background: transparent;
}
QMenuBar::item:selected {
    background: #eef2f8;
}
QMenu {
    background: #ffffff;
    border: 1px solid #d9dfe8;
    border-radius: 7px;
    padding: 5px;
}
QMenu::item {
    padding: 6px 24px 6px 10px;
    border-radius: 5px;
}
QMenu::item:selected {
    background: #eaf0fb;
}
QMenu::separator {
    height: 1px;
    background: #e8ecf1;
    margin: 4px 8px;
}
QStatusBar {
    background: #ffffff;
    border-top: 1px solid #dde2e9;
    color: #5d6572;
}
QStatusBar::item {
    border: none;
}
QWidget#leftPanel {
    background: #ffffff;
    border: 1px solid #dde2e9;
    border-radius: 9px;
}
QWidget#rightPanel {
    background: transparent;
}
QLabel#panelTitle {
    font-size: 12px;
    font-weight: 700;
    color: #1f2733;
}
QLabel#mutedLabel {
    color: #7a8390;
    font-size: 8.5pt;
}
QLabel#sectionHint {
    color: #7a8390;
    font-size: 8.5pt;
    padding-bottom: 2px;
}
QLabel#statusStrong {
    font-weight: 600;
    color: #39414d;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff;
    border: 1px solid #d5dbe4;
    border-radius: 6px;
    padding: 4px 7px;
    min-height: 22px;
    selection-background-color: #cfe0ff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #7dabf7;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
    background: #f6f7f9;
    color: #9aa1ab;
}
QLineEdit#searchEdit {
    background: #f7f9fc;
    padding-left: 8px;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("@CHEVRON_DOWN@");
    width: 10px;
    height: 6px;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 19px;
    border: none;
    background: transparent;
}
QSpinBox::up-arrow {
    image: url("@CHEVRON_UP@");
    width: 8px;
    height: 5px;
}
QSpinBox::down-arrow {
    image: url("@CHEVRON_DOWN@");
    width: 8px;
    height: 5px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d5dbe4;
    selection-background-color: #eaf0fb;
    selection-color: #202631;
    outline: none;
    padding: 3px;
}
QPlainTextEdit, QTextEdit {
    background: #ffffff;
    border: 1px solid #d5dbe4;
    border-radius: 6px;
    padding: 5px;
    selection-background-color: #cfe0ff;
}
QTextEdit#logView {
    background: #fbfcfe;
    border: 1px solid #e2e7ee;
    border-radius: 6px;
    font-family: "DejaVu Sans Mono", "Noto Sans Mono", monospace;
    font-size: 8.5pt;
    padding: 5px;
}
QTableWidget, QTreeWidget, QListWidget {
    background: #ffffff;
    border: 1px solid #e0e5ec;
    border-radius: 7px;
    outline: none;
    alternate-background-color: #fafbfd;
    gridline-color: #edf0f4;
    selection-background-color: #e7efff;
    selection-color: #172234;
}
QTableWidget::item, QTreeWidget::item, QListWidget::item {
    padding: 3px 5px;
    border-bottom: 1px solid #f1f3f6;
}
QTreeWidget::item {
    padding: 4px 5px;
    border-bottom: none;
}
QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected {
    background: #e7efff;
    color: #172234;
}
QTreeWidget::branch {
    background: transparent;
}
QHeaderView::section {
    background: #f4f6f9;
    color: #4d5663;
    border: none;
    border-bottom: 1px solid #dde2e9;
    border-right: 1px solid #eaedf2;
    padding: 5px 7px;
    font-weight: 600;
    font-size: 8.5pt;
}
QTableCornerButton::section {
    background: #f4f6f9;
    border: none;
}
QTabWidget::pane {
    border: 1px solid #dde2e9;
    border-radius: 9px;
    background: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: #66707d;
    padding: 8px 15px;
    margin-right: 3px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}
QTabBar::tab:hover {
    color: #2f6fed;
    background: #f2f6fd;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabBar::tab:selected {
    color: #2f6fed;
    border-bottom: 2px solid #2f6fed;
    font-weight: 700;
    background: #ffffff;
}
QGroupBox {
    border: 1px solid #e0e5ec;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 8px 10px;
    background: #ffffff;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 5px;
    color: #39414d;
    background: #ffffff;
}
QCheckBox {
    spacing: 6px;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #d5dbe4;
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 22px;
}
QPushButton:hover {
    background: #f5f8fd;
    border-color: #b9c8dd;
}
QPushButton:pressed {
    background: #eaf0fa;
}
QPushButton:disabled {
    background: #f6f7f9;
    color: #a1a8b2;
    border-color: #e4e8ee;
}
QPushButton[variant="primary"] {
    background: #2f6fed;
    border-color: #2f6fed;
    color: #ffffff;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover {
    background: #1f57c3;
    border-color: #1f57c3;
}
QPushButton[variant="danger"] {
    color: #b42318;
    border-color: #efd2ce;
    background: #fff8f7;
}
QPushButton[variant="danger"]:hover {
    background: #fdecea;
    border-color: #e6aba3;
}
QPushButton[variant="flat"] {
    border-color: transparent;
    background: transparent;
    color: #5d6572;
    padding: 3px 7px;
}
QPushButton[variant="flat"]:hover {
    background: #eef2f8;
}
QProgressBar {
    background: #e9edf3;
    border: none;
    border-radius: 5px;
    height: 11px;
    text-align: center;
    color: #39414d;
    font-size: 8pt;
}
QProgressBar::chunk {
    background: #2f6fed;
    border-radius: 5px;
}
QSplitter::handle {
    background: transparent;
    height: 7px;
    width: 7px;
}
QScrollBar:vertical {
    background: transparent;
    width: 11px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #c8d0db;
    border-radius: 5px;
    min-height: 26px;
}
QScrollBar::handle:vertical:hover {
    background: #adb8c6;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 11px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #c8d0db;
    border-radius: 5px;
    min-width: 26px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QToolTip {
    background: #202631;
    color: #ffffff;
    border: none;
    padding: 5px 8px;
    border-radius: 4px;
}
"""

_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
STYLESHEET = STYLESHEET.replace(
    "@CHEVRON_DOWN@", (_ASSET_DIR / "chevron-down.svg").as_posix()
).replace("@CHEVRON_UP@", (_ASSET_DIR / "chevron-up.svg").as_posix())
