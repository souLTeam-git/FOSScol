"""Background workers and dialogs for one-page inspection.

The campaign editor uses these helpers for smart auto-detect and for the
"Test on 1 URL" action.  They never touch the crawl queue or the results
storage: a single page is opened, the fields are extracted, and the outcome is
shown in a modal dialog.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.auto_fields import DetectedField, detect_fields
from core.preview import PageSnapshot, PreviewError, PreviewResult, capture_page, preview_fields
from ui.icons import app_icon

# Workers that did not stop within the dialog's wait window are kept alive
# here until their Playwright cleanup finishes.  They are never terminated.
_DETACHED_WORKERS: set[QThread] = set()


def _detach_worker(worker: QThread) -> None:
    worker.setParent(None)
    _DETACHED_WORKERS.add(worker)
    worker.finished.connect(lambda: _DETACHED_WORKERS.discard(worker))


class AutoDetectWorker(QThread):
    """Open one page and suggest campaign fields from its markup."""

    succeeded = Signal(object, object)
    failed = Signal(str)

    def __init__(self, campaign: dict[str, Any], url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.campaign = campaign
        self.url = url
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            snapshot = capture_page(self.campaign, self.url, headless=True)
            detected = detect_fields(snapshot.html, snapshot.final_url)
        except PreviewError as exc:
            if not self._stop_event.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - defensive guard for the UI thread
            if not self._stop_event.is_set():
                message = str(exc).strip().splitlines()
                self.failed.emit(message[0] if message else exc.__class__.__name__)
        else:
            if not self._stop_event.is_set():
                self.succeeded.emit(detected, snapshot)


class PreviewWorker(QThread):
    """Extract the current campaign fields from a single page."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, campaign: dict[str, Any], url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.campaign = campaign
        self.url = url
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            result = preview_fields(self.campaign, self.url, headless=True)
        except PreviewError as exc:
            if not self._stop_event.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - defensive guard for the UI thread
            if not self._stop_event.is_set():
                message = str(exc).strip().splitlines()
                self.failed.emit(message[0] if message else exc.__class__.__name__)
        else:
            if not self._stop_event.is_set():
                self.succeeded.emit(result)


class AutoDetectDialog(QDialog):
    """Checkbox list of detected fields with value previews."""

    COLUMNS = ("", "Поле", "Тип", "Источник", "Значение")

    def __init__(
        self,
        detected: list[DetectedField],
        snapshot: PageSnapshot | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Автозаполнение по URL")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.resize(940, 540)
        self._detected = list(detected)
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(9)

        title = QLabel("Найденные поля и динамические характеристики")
        title.setStyleSheet("font-size: 13pt; font-weight: 700; color: #1f2733;")
        layout.addWidget(title)

        if snapshot is not None:
            details = (
                f"Страница: {snapshot.final_url}\n"
                f"HTTP {snapshot.status or '—'} · элементов предложено: {len(self._detected)} · "
                f"{snapshot.elapsed:.1f} с"
            )
            if snapshot.captcha:
                details += "\nВнимание: страница похожа на проверку бота, данные могут быть неполными."
        else:
            details = f"Предложено полей: {len(self._detected)}"
        hint = QLabel(details)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        if any(item.is_dynamic for item in self._detected):
            dynamic_hint = QLabel(
                "Динамические характеристики добавляются одним блоком — отметьте не более "
                "одной строки «Динамические характеристики». Остальные строки можно оставить "
                "для сравнения."
            )
            dynamic_hint.setObjectName("sectionHint")
            dynamic_hint.setWordWrap(True)
            layout.addWidget(dynamic_hint)

        self.table = QTableWidget(len(self._detected), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 28)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 170)
        self.table.setColumnWidth(3, 110)

        for row, item in enumerate(self._detected):
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            check.setCheckState(
                Qt.CheckState.Checked if item.checked else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, check)
            name_item = QTableWidgetItem(item.name)
            name_item.setToolTip(item.name)
            if item.is_dynamic:
                name_item.setToolTip(
                    f"{item.name}\n\n"
                    f"Строки: {item.row_selector}\n"
                    f"Названия: {item.name_selector}\n"
                    f"Значения: {item.value_selector}"
                )
            self.table.setItem(row, 1, name_item)
            type_item = QTableWidgetItem("Динамические" if item.is_dynamic else item.field_type)
            self.table.setItem(row, 2, type_item)
            source_item = QTableWidgetItem(item.source)
            self.table.setItem(row, 3, source_item)
            if item.is_dynamic:
                value_text = item.preview_text(5)
                value_tooltip = (
                    f"{item.value}\n\n"
                    f"Строки: {item.row_selector}\n"
                    f"Названия: {item.name_selector}\n"
                    f"Значения: {item.value_selector}"
                )
            else:
                value_text = item.value
                value_tooltip = f"{item.value}\n\n{item.selector}"
            value_item = QTableWidgetItem(value_text)
            value_item.setToolTip(value_tooltip)
            self.table.setItem(row, 4, value_item)

        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        self.all_button = QPushButton("Выбрать все")
        self.all_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        controls.addWidget(self.all_button)
        self.none_button = QPushButton("Снять все")
        self.none_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        controls.addWidget(self.none_button)
        self.counter_label = QLabel("")
        self.counter_label.setObjectName("mutedLabel")
        controls.addWidget(self.counter_label)
        controls.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        controls.addWidget(cancel)
        self.add_button = QPushButton("Добавить выбранные")
        self.add_button.setProperty("variant", "primary")
        self.add_button.clicked.connect(self.accept)
        controls.addWidget(self.add_button)
        layout.addLayout(controls)

        self._update_counter()

    def _set_all(self, state: Qt.CheckState) -> None:
        first_dynamic = next(
            (index for index, item in enumerate(self._detected) if item.is_dynamic),
            -1,
        )
        self._updating = True
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            if state == Qt.CheckState.Checked and self._detected[row].is_dynamic:
                item.setCheckState(
                    Qt.CheckState.Checked if row == first_dynamic else Qt.CheckState.Unchecked
                )
            else:
                item.setCheckState(state)
        self._updating = False
        self._update_counter()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 0:
            return
        row = item.row()
        if (
            0 <= row < len(self._detected)
            and self._detected[row].is_dynamic
            and item.checkState() == Qt.CheckState.Checked
        ):
            self._updating = True
            for other in range(self.table.rowCount()):
                if other == row or not self._detected[other].is_dynamic:
                    continue
                other_item = self.table.item(other, 0)
                if other_item is not None:
                    other_item.setCheckState(Qt.CheckState.Unchecked)
            self._updating = False
        self._update_counter()

    def _checked_rows(self) -> list[int]:
        rows: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def _update_counter(self, *_args: Any) -> None:
        count = len(self._checked_rows())
        self.counter_label.setText(f"Выбрано: {count} из {len(self._detected)}")
        self.add_button.setEnabled(count > 0)

    def selected_detected(self) -> list[DetectedField]:
        return [self._detected[row] for row in self._checked_rows()]

    def selected_fields(self) -> list[dict[str, Any]]:
        return [self._detected[row].as_field() for row in self._checked_rows()]


class TestRunDialog(QDialog):
    """Per-field outcome for the current campaign on one URL."""

    COLUMNS = ("Поле", "Тип", "Статус", "Значение")

    def __init__(
        self,
        campaign: dict[str, Any],
        url: str,
        result: PreviewResult | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Тест на 1 URL")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.resize(960, 520)
        self._campaign = campaign
        self._url = url
        self._worker: PreviewWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(9)

        title = QLabel("Результат проверки полей")
        title.setStyleSheet("font-size: 13pt; font-weight: 700; color: #1f2733;")
        layout.addWidget(title)

        self.info_label = QLabel(f"Страница: {url}")
        self.info_label.setObjectName("sectionHint")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(2, 110)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.retry_button = QPushButton("Повторить")
        self.retry_button.clicked.connect(self.run_check)
        buttons.addWidget(self.retry_button)
        close_button = QPushButton("Закрыть")
        close_button.setProperty("variant", "primary")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        if result is not None:
            self._apply_result(result)
        else:
            self.run_check()

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def run_check(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.retry_button.setEnabled(False)
        self.retry_button.setText("Проверка…")
        self.info_label.setText(f"Открываю страницу: {self._url}")
        worker = PreviewWorker(self._campaign, self._url, self)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._worker = worker
        worker.start()

    def _on_succeeded(self, result: PreviewResult) -> None:
        self._apply_result(result)

    def _on_failed(self, message: str) -> None:
        self.info_label.setText(f"Не удалось открыть страницу: {message}")
        self.info_label.setStyleSheet("color: #c62828;")
        self.table.setRowCount(0)

    def _on_finished(self) -> None:
        self.retry_button.setEnabled(True)
        self.retry_button.setText("Повторить")
        self._worker = None

    def _apply_result(self, result: PreviewResult) -> None:
        snapshot = result.snapshot
        summary = (
            f"Страница: {snapshot.final_url} · HTTP {snapshot.status or '—'} · "
            f"{snapshot.elapsed:.1f} с · полей заполнено: {result.filled} из {len(result.fields)}"
        )
        if snapshot.captcha:
            summary += "\nВнимание: страница похожа на проверку бота."
        self.info_label.setText(summary)
        self.info_label.setStyleSheet("color: #c62828;" if snapshot.captcha else "")

        self.table.setRowCount(len(result.fields))
        for row, field in enumerate(result.fields):
            self.table.setItem(row, 0, QTableWidgetItem(field.name))
            type_text = "Динамика" if field.field_type == "specs" else field.field_type
            self.table.setItem(row, 1, QTableWidgetItem(type_text))
            status_text, color = {
                "ok": ("Успешно", "#1a7f37"),
                "empty": ("Пусто", "#b26a00"),
                "error": ("Ошибка", "#c62828"),
            }.get(field.status, (field.status, "#202631"))
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(QBrush(QColor(color)))
            self.table.setItem(row, 2, status_item)
            value_item = QTableWidgetItem(field.value)
            value_item.setToolTip((field.error or field.value) + f"\n\n{field.selector}")
            self.table.setItem(row, 3, value_item)

    def done(self, result: int) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(10000):
                # Playwright is still finishing a blocking call; keep the
                # thread alive until its finally-block closes the browser.
                _detach_worker(worker)
        super().done(result)
