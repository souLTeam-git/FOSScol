"""Results table with live updates during a run."""

from __future__ import annotations

import copy
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.specs_columns import order_dynamic_columns
from ui.dialogs import ValuePreviewDialog
from ui.widgets import SectionHeader

INTERNAL_PREFIX = "__"
TOOLTIP_LIMIT = 1500
BATCH_INTERVAL_MS = 100
BATCH_ROW_LIMIT = 50


class ResultsTab(QWidget):
    count_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._headers: list[str] = []
        self._column_index: dict[str, int] = {}
        self._priority_columns: Any = []
        self._running = False
        self._error_count = 0
        self._rows_since_update = 0
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(BATCH_INTERVAL_MS)
        self._update_timer.timeout.connect(self.flush_updates)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        layout.addWidget(
            SectionHeader(
                "Результаты",
                "Строки добавляются по мере обработки страниц. Двойной щелчок открывает полное значение.",
            )
        )

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.count_label = QLabel("0 строк")
        self.count_label.setObjectName("statusStrong")
        toolbar.addWidget(self.count_label)
        self.error_label = QLabel("")
        self.error_label.setObjectName("mutedLabel")
        toolbar.addWidget(self.error_label)
        toolbar.addStretch(1)

        self.copy_button = QPushButton("Копировать")
        self.copy_button.setProperty("variant", "flat")
        self.copy_button.clicked.connect(self.copy_all)
        toolbar.addWidget(self.copy_button)

        self.clear_button = QPushButton("Очистить")
        self.clear_button.setProperty("variant", "flat")
        self.clear_button.clicked.connect(self.clear_results)
        toolbar.addWidget(self.clear_button)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 0)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.verticalHeader().setMinimumWidth(38)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.table, 1)

    # ------------------------------------------------------------------
    def set_column_priority(self, priority: Any) -> None:
        """Set priority spec columns used when new columns appear."""

        self._priority_columns = copy.deepcopy(priority) if priority else []

    def prepare(self, field_names: list[str]) -> None:
        self._update_timer.stop()
        self._rows = []
        self._error_count = 0
        self._rows_since_update = 0
        self.table.clear()
        headers = list(
            dict.fromkeys(
                str(name).strip()
                for name in field_names
                if str(name).strip() and not str(name).startswith(INTERNAL_PREFIX)
            )
        )
        if "Статус" not in headers:
            headers.append("Статус")
        self._headers = headers
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(0)
        for index, header in enumerate(headers):
            width = 170 if index == 0 else 128
            if header == "Статус":
                width = 128
            self.table.setColumnWidth(index, width)
        self._column_index = {name: index for index, name in enumerate(headers)}
        self._update_count()
        self.count_changed.emit(0)

    def all_rows(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row) for row in self._rows]

    def row_count(self) -> int:
        return len(self._rows)

    @property
    def headers(self) -> list[str]:
        return list(self._headers)

    def set_running(self, running: bool) -> None:
        self._running = running
        self.clear_button.setEnabled(not running)
        if not running:
            self.flush_updates()

    # ------------------------------------------------------------------
    def add_row(self, row: dict[str, Any]) -> None:
        if not self._headers:
            keys = [
                str(key)
                for key in row
                if not str(key).startswith(INTERNAL_PREFIX)
            ]
            keys = order_dynamic_columns(keys, self._priority_columns)
            self.prepare(keys)

        new_keys = [
            str(key)
            for key in row
            if not str(key).startswith(INTERNAL_PREFIX)
            and key not in self._column_index
        ]
        # Priority spec columns always land before other new columns.
        for key in order_dynamic_columns(new_keys, self._priority_columns):
            if key not in self._column_index:
                self._insert_column(key)

        scrollbar = self.table.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 6
        table_row = self.table.rowCount()
        self.table.insertRow(table_row)
        status = str(row.get("__status__", ""))
        error = status.lower().startswith("ошибка")
        for column, header in enumerate(self._headers):
            if header == "Статус":
                value = status
            else:
                value = row.get(header, "")
            display = self._display_value(value)
            item = QTableWidgetItem(display)
            item.setToolTip(self._tooltip_text(self._plain_value(value)))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if header == "Статус":
                if error:
                    item.setForeground(QBrush(QColor("#b42318")))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                else:
                    item.setForeground(QBrush(QColor("#1a7f37")))
            if error:
                item.setBackground(QBrush(QColor("#fff0f0")))
            self.table.setItem(table_row, column, item)

        self._rows.append(dict(row))
        if error:
            self._error_count += 1
        if at_bottom:
            self.table.scrollToBottom()
        self._rows_since_update += 1
        if self._rows_since_update >= BATCH_ROW_LIMIT:
            self.flush_updates()
        elif not self._update_timer.isActive():
            self._update_timer.start()

    def _insert_column(self, header: str) -> None:
        status_index = self._column_index.get("Статус", len(self._headers))
        insert_at = status_index if status_index >= 0 else len(self._headers)
        self.table.insertColumn(insert_at)
        self._headers.insert(insert_at, header)
        self.table.setHorizontalHeaderLabels(self._headers)
        self.table.setColumnWidth(insert_at, max(120, min(230, len(header) * 8 + 26)))
        self._refresh_column_index()
        for row_index, row in enumerate(self._rows):
            value = row.get(header, "")
            item = QTableWidgetItem(self._display_value(value))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setToolTip(self._tooltip_text(self._plain_value(value)))
            self.table.setItem(row_index, insert_at, item)

    def _refresh_column_index(self) -> None:
        self._column_index = {name: index for index, name in enumerate(self._headers)}

    @staticmethod
    def _plain_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple, set)):
            return str(value)
        return str(value)

    @classmethod
    def _display_value(cls, value: Any) -> str:
        text = cls._plain_value(value)
        text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
        if len(text) > 500:
            text = text[:500] + "…"
        return text

    @staticmethod
    def _tooltip_text(text: str) -> str:
        if len(text) <= TOOLTIP_LIMIT:
            return text
        return text[: TOOLTIP_LIMIT - 1] + "…"

    def flush_updates(self) -> None:
        """Apply the batched row counter/status changes immediately."""

        if self._update_timer.isActive():
            self._update_timer.stop()
        if not self._rows_since_update:
            return
        self._rows_since_update = 0
        self._update_count()
        self.count_changed.emit(len(self._rows))

    def _update_count(self) -> None:
        count = len(self._rows)
        self.count_label.setText(f"{count} {self._plural(count, 'строка', 'строки', 'строк')}")
        self.error_label.setText(
            f"· ошибок: {self._error_count}" if self._error_count else ""
        )

    @staticmethod
    def _plural(number: int, one: str, few: str, many: str) -> str:
        if number % 10 == 1 and number % 100 != 11:
            return one
        if number % 10 in {2, 3, 4} and number % 100 not in {12, 13, 14}:
            return few
        return many

    def clear_results(self) -> None:
        if not self._rows:
            return
        answer = QMessageBox.question(
            self,
            "Очистить результаты",
            f"Удалить все строки результатов ({len(self._rows)})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._update_timer.stop()
        self._rows = []
        self._error_count = 0
        self._rows_since_update = 0
        self.table.setRowCount(0)
        self._update_count()
        self.count_changed.emit(0)

    def clear_silently(self) -> None:
        self._update_timer.stop()
        self._rows = []
        self._error_count = 0
        self._rows_since_update = 0
        self.table.setRowCount(0)
        self._update_count()
        self.count_changed.emit(0)

    # ------------------------------------------------------------------
    def copy_all(self) -> None:
        if not self._rows:
            return
        lines = ["\t".join(self._headers)]
        for row in self._rows:
            values = []
            for header in self._headers:
                value = row.get("__status__", "") if header == "Статус" else row.get(header, "")
                values.append(self._plain_value(value).replace("\t", " ").replace("\n", " "))
            lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(lines))

    def copy_selection(self) -> None:
        indexes = self.table.selectedIndexes()
        if not indexes:
            self.copy_all()
            return
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        lines = []
        for row in rows:
            values = []
            for column in columns:
                item = self.table.item(row, column)
                values.append(item.text().replace("\t", " ") if item else "")
            lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(lines))

    def _on_double_clicked(self, item: QTableWidgetItem) -> None:
        row_index = item.row()
        if not (0 <= row_index < len(self._rows)):
            return
        row = self._rows[row_index]
        header = self._headers[item.column()] if item.column() < len(self._headers) else ""
        value = row.get("__status__", "") if header == "Статус" else row.get(header, "")
        dialog = ValuePreviewDialog(header, self._plain_value(value), self)
        dialog.exec()

    def _show_context_menu(self, position: Any) -> None:
        item = self.table.itemAt(position)
        menu = QMenu(self)
        copy_action = menu.addAction("Копировать выделенное")
        copy_all_action = menu.addAction("Копировать всё")
        if item is not None:
            menu.addSeparator()
            preview_action = menu.addAction("Показать полностью")
            url_action = menu.addAction("Открыть URL строки")
        else:
            preview_action = None
            url_action = None
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen == copy_action:
            self.copy_selection()
        elif chosen == copy_all_action:
            self.copy_all()
        elif preview_action is not None and chosen == preview_action:
            self._on_double_clicked(item)
        elif url_action is not None and chosen == url_action:
            self._open_row_url(item.row())

    def _open_row_url(self, row_index: int) -> None:
        if not (0 <= row_index < len(self._rows)):
            return
        row = self._rows[row_index]
        url = str(row.get("URL") or row.get("__url__") or "").strip()
        if not url and "URL" in self._column_index:
            item = self.table.item(row_index, self._column_index["URL"])
            url = item.text() if item else ""
        if url.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))
