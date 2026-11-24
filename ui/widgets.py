"""Reusable widgets used by the main window and tabs."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import ERROR, MUTED, SUCCESS, WARNING


class SectionHeader(QWidget):
    """Small title with an optional explanatory line."""

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 2)
        layout.setSpacing(2)
        label = QLabel(title)
        label.setObjectName("panelTitle")
        layout.addWidget(label)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("sectionHint")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)


class LogPanel(QWidget):
    """Coloured, timestamped run log."""

    LEVELS = {
        "system": ("СИСТ", "#6f42c1"),
        "info": ("ИНФО", "#1f6feb"),
        "success": ("OK", SUCCESS),
        "warning": ("ВНИМ", WARNING),
        "error": ("ОШИБКА", ERROR),
        "debug": ("ОТЛ", "#7a8390"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.setSpacing(4)
        title = QLabel("Журнал выполнения")
        title.setObjectName("statusStrong")
        header.addWidget(title)
        header.addStretch(1)

        copy_button = QPushButton("Копировать")
        copy_button.setProperty("variant", "flat")
        copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_button.clicked.connect(self.copy_all)
        header.addWidget(copy_button)

        clear_button = QPushButton("Очистить")
        clear_button.setProperty("variant", "flat")
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.clicked.connect(self.clear_log)
        header.addWidget(clear_button)
        layout.addLayout(header)

        self.view = QTextEdit(self)
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)
        self.view.setAcceptRichText(True)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.view.setMinimumHeight(110)
        self.view.document().setMaximumBlockCount(5000)
        layout.addWidget(self.view, 1)

        self._count = 0

    def append(self, level: str, message: str) -> None:
        level_key = level.lower().strip()
        label, color = self.LEVELS.get(level_key, self.LEVELS["info"])
        from PySide6.QtCore import QTime

        timestamp = QTime.currentTime().toString("HH:mm:ss")
        safe_message = html.escape(str(message)).replace("\n", "<br>")
        scrollbar = self.view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        row = (
            f'<span style="color:#9aa3af;">[{timestamp}]</span> '
            f'<span style="color:{color}; font-weight:700;">{label:6}</span> '
            f'<span style="color:#2d333b;">{safe_message}</span>'
        )
        self.view.append(row)
        self._count += 1
        if at_bottom:
            self.view.moveCursor(QTextCursor.MoveOperation.End)
            scrollbar.setValue(scrollbar.maximum())

    def log(self, message: str, level: str = "info") -> None:
        self.append(level, message)

    def clear_log(self) -> None:
        self.view.clear()
        self._count = 0

    def copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.view.toPlainText())

    def last_message(self) -> str:
        return self.view.toPlainText().strip().splitlines()[-1] if self._count else ""


class PathEdit(QWidget):
    """Line edit with a browse button for files and directories."""

    path_changed = Signal(str)

    def __init__(
        self,
        mode: str = "save_file",
        file_filter: str = "Все файлы (*)",
        default_suffix: str = "",
        base_dir: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.file_filter = file_filter
        self.default_suffix = default_suffix.lstrip(".")
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.edit = QLineEdit(self)
        self.edit.setClearButtonEnabled(True)
        layout.addWidget(self.edit, 1)
        self.button = QToolButton(self)
        self.button.setText("Обзор…")
        self.button.setObjectName("smallButton")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.browse)
        layout.addWidget(self.button)
        self.edit.textChanged.connect(self.path_changed.emit)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def set_placeholder(self, value: str) -> None:
        self.edit.setPlaceholderText(value)

    def _start_path(self) -> str:
        raw = Path(self.text()).expanduser() if self.text() else self.base_dir
        if not raw.is_absolute():
            raw = self.base_dir / raw
        if raw.suffix and self.mode == "save_file":
            return str(raw.parent)
        if raw.is_file():
            return str(raw.parent)
        return str(raw)

    def browse(self) -> None:
        start = self._start_path()
        selected = ""
        if self.mode == "directory":
            selected = QFileDialog.getExistingDirectory(self, "Выберите папку", start)
        elif self.mode == "open_file":
            selected, _ = QFileDialog.getOpenFileName(self, "Выберите файл", start, self.file_filter)
        else:
            selected, _ = QFileDialog.getSaveFileName(self, "Сохранить файл", start, self.file_filter)
        if not selected:
            return
        path = Path(selected)
        if self.default_suffix and not path.suffix:
            path = path.with_suffix(f".{self.default_suffix}")
        self.edit.setText(str(path))


class FormLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setMinimumWidth(150)


class EmptyState(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setStyleSheet(f"color: {MUTED}; padding: 24px; font-size: 10pt;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


def make_text_button(text: str, callback: Callable[[], None], variant: str = "flat") -> QPushButton:
    button = QPushButton(text)
    button.setProperty("variant", variant)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(lambda: callback())
    return button


def compact_button(text: str, tooltip: str = "") -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setObjectName("smallButton")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(tooltip)
    button.setAutoRaise(False)
    return button
