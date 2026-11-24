"""Modal dialogs that do not depend on the XPath picker browser."""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.icons import app_icon


class CampaignDialog(QDialog):
    """Create a campaign or rename an existing one."""

    def __init__(
        self,
        parent: QWidget | None = None,
        categories: Iterable[str] = (),
        name: str = "",
        category: str = "",
        title: str = "Новая кампания",
        ok_text: str = "Создать",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        heading = QLabel(title)
        heading.setStyleSheet("font-size: 13pt; font-weight: 700; color: #1f2733;")
        layout.addWidget(heading)

        hint = QLabel("Имя используется как название кампании и имя JSON-файла.")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Например: hotline-new")
        self.name_edit.setText(name)
        form.addRow("Имя кампании", self.name_edit)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.category_combo.addItem("")
        for item in sorted({str(value).strip() for value in categories if str(value).strip()}, key=str.casefold):
            self.category_combo.addItem(item)
        # Some callers connect the dialog to a clicked/triggered signal, which
        # passes a bool.  Only strings are valid category names.
        self.category_combo.setCurrentText(category if isinstance(category, str) else "")
        self.category_combo.lineEdit().setPlaceholderText("Папка / категория (необязательно)")
        form.addRow("Категория", self.category_combo)

        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #c62828;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText(ok_text)
        ok_button.setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.name_edit.setFocus()
        self.name_edit.selectAll()
        self.name_edit.returnPressed.connect(self._accept)

    def _accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._show_error("Введите имя кампании.")
            return
        if "/" in name or "\\" in name:
            self._show_error("Имя не должно содержать символы '/' и '\\'.")
            return
        if name in {".", ".."}:
            self._show_error("Недопустимое имя кампании.")
            return
        self.error_label.hide()
        self.accept()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()
        self.name_edit.setFocus()

    @property
    def campaign_name(self) -> str:
        return self.name_edit.text().strip()

    @property
    def category(self) -> str:
        return self.category_combo.currentText().strip()


class ValuePreviewDialog(QDialog):
    """Show the complete value of a result cell."""

    def __init__(self, column: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Значение — {column}")
        self.setWindowIcon(app_icon())
        self.resize(820, 560)
        self._value = value

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel(column)
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        editor = QPlainTextEdit()
        editor.setPlainText(value)
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        editor.setFont(self.font())
        layout.addWidget(editor, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy_button = QPushButton("Копировать")
        copy_button.clicked.connect(lambda: self._copy())
        buttons.addWidget(copy_button)

        if value.strip().lower().startswith(("http://", "https://")):
            open_button = QPushButton("Открыть в браузере")
            open_button.setProperty("variant", "primary")
            open_button.clicked.connect(self._open_url)
            buttons.addWidget(open_button)

        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._value)

    def _open_url(self) -> None:
        QDesktopServices.openUrl(QUrl(self._value.strip()))
