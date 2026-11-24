"""Tab for configuring extracted fields."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.schema import FIELD_TYPE_LABELS, FIELD_TYPES, new_field
from ui.widgets import SectionHeader, compact_button


class FieldsTab(QWidget):
    changed = Signal()
    picker_requested = Signal(int)
    page_picker_requested = Signal(str)
    dynamic_picker_requested = Signal(str)
    auto_detect_requested = Signal(str)
    test_run_requested = Signal(str)

    COLUMNS = ("Имя", "Тип", "Селектор", "Удалять теги", "Пикер", "Удалить")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fields: list[dict] = []
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)

        layout.addWidget(
            SectionHeader(
                "Поля данных",
                "Для каждой колонки результата задайте имя, тип и селектор. "
                "«Выбрать на странице» открывает Chromium и возвращает XPath выбранного "
                "элемента вместе с типом и превью значения. «Выбрать характеристики» "
                "собирает динамический блок: название → значение, без ручного перечисления "
                "десятков полей.",
            )
        )

        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        url_label = QLabel("Тестовая страница:")
        url_row.addWidget(url_label)
        self.test_url_edit = QLineEdit()
        self.test_url_edit.setPlaceholderText("https://example.com/product/123")
        self.test_url_edit.setClearButtonEnabled(True)
        self.test_url_edit.returnPressed.connect(self._request_test_run)
        url_row.addWidget(self.test_url_edit, 1)

        self.pick_button = QPushButton("Выбрать на странице")
        self.pick_button.setProperty("variant", "primary")
        self.pick_button.setToolTip("Открыть страницу в Chromium и выбрать элемент кликом")
        self.pick_button.clicked.connect(self._request_page_picker)
        url_row.addWidget(self.pick_button)

        self.dynamic_pick_button = QPushButton("Выбрать характеристики")
        self.dynamic_pick_button.setToolTip(
            "Динамические характеристики: кликните название характеристики, затем её значение"
        )
        self.dynamic_pick_button.clicked.connect(self._request_dynamic_picker)
        url_row.addWidget(self.dynamic_pick_button)

        self.auto_button = QPushButton("Автозаполнение по URL")
        self.auto_button.setToolTip(
            "Найти поля товара в JSON-LD, OpenGraph, микроразметке и таблицах характеристик"
        )
        self.auto_button.clicked.connect(self._request_auto_detect)
        url_row.addWidget(self.auto_button)

        self.test_button = QPushButton("Тест на 1 URL")
        self.test_button.setToolTip("Проверить текущие поля на одной странице без запуска очереди")
        self.test_button.clicked.connect(self._request_test_run)
        url_row.addWidget(self.test_button)
        layout.addLayout(url_row)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 190)
        self.table.setColumnWidth(3, 110)
        self.table.setColumnWidth(4, 78)
        self.table.setColumnWidth(5, 92)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.add_button = QPushButton("Добавить поле")
        self.add_button.setProperty("variant", "primary")
        self.add_button.clicked.connect(self.add_field)
        bottom.addWidget(self.add_button)

        self.remove_button = compact_button("Удалить выбранное", "Удалить поле")
        self.remove_button.clicked.connect(self.remove_selected)
        bottom.addWidget(self.remove_button)

        self.up_button = compact_button("Вверх", "Переместить поле выше")
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        bottom.addWidget(self.up_button)

        self.down_button = compact_button("Вниз", "Переместить поле ниже")
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        bottom.addWidget(self.down_button)

        bottom.addStretch(1)
        hint = QLabel("Поддерживаются XPath, outer HTML, атрибуты, regex и URL страницы.")
        hint.setObjectName("mutedLabel")
        bottom.addWidget(hint)
        layout.addLayout(bottom)

        self._update_buttons()

    # ------------------------------------------------------------------
    # Loading / reading
    # ------------------------------------------------------------------
    def set_campaign(self, campaign: dict) -> None:
        raw_fields = campaign.get("fields") if isinstance(campaign.get("fields"), list) else []
        self._fields = [copy.deepcopy(item) for item in raw_fields if isinstance(item, dict)]
        self._rebuild()

    def fields(self) -> list[dict]:
        result: list[dict] = []
        for row in range(self.table.rowCount()):
            base = copy.deepcopy(self._fields[row]) if row < len(self._fields) else {}
            name_item = self.table.item(row, 0)
            selector_item = self.table.item(row, 2)
            type_combo = self.table.cellWidget(row, 1)
            strip_checkbox = self.table.cellWidget(row, 3)
            field_type = str(type_combo.currentData()) if isinstance(type_combo, QComboBox) else "xpath"
            base["name"] = name_item.text().strip() if name_item else ""
            base["type"] = field_type if field_type in FIELD_TYPES else "xpath"
            base["selector"] = "" if base["type"] == "spec_url" else (selector_item.text() if selector_item else "")
            base["strip_tags"] = bool(strip_checkbox.isChecked()) if isinstance(strip_checkbox, QCheckBox) else False
            result.append(base)
        return result

    def field_names(self) -> list[str]:
        names: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text().strip():
                names.append(item.text().strip())
        return names

    def set_field_selector(self, row: int, selector: str) -> None:
        if 0 <= row < self.table.rowCount():
            item = self.table.item(row, 2)
            if item is not None:
                item.setText(selector)
                item.setToolTip(selector)
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                self.changed.emit()

    # ------------------------------------------------------------------
    # Test page helpers
    # ------------------------------------------------------------------
    def test_url(self) -> str:
        return self.test_url_edit.text().strip()

    def set_test_url(self, url: str) -> None:
        if url and not self.test_url_edit.text().strip():
            self.test_url_edit.setText(url)

    def set_busy(self, busy: bool) -> None:
        for button in (
            self.pick_button,
            self.dynamic_pick_button,
            self.auto_button,
            self.test_button,
        ):
            button.setEnabled(not busy)

    def _valid_test_url(self) -> str:
        url = self.test_url()
        if url.lower().startswith(("http://", "https://")):
            return url
        QMessageBox.warning(
            self,
            "Тестовая страница",
            "Укажите корректный http:// или https:// адрес в поле «Тестовая страница».",
        )
        return ""

    def _request_page_picker(self) -> None:
        url = self._valid_test_url()
        if url:
            self.page_picker_requested.emit(url)

    def _request_dynamic_picker(self) -> None:
        url = self._valid_test_url()
        if url:
            self.dynamic_picker_requested.emit(url)

    def _request_auto_detect(self) -> None:
        url = self._valid_test_url()
        if url:
            self.auto_detect_requested.emit(url)

    def _request_test_run(self) -> None:
        url = self._valid_test_url()
        if url:
            self.test_run_requested.emit(url)

    def apply_pick(self, row: int, payload: dict) -> int:
        """Apply a picker payload to a row, or append a new field for ``row < 0``."""

        self._sync_fields()
        selector = str(payload.get("selector") or payload.get("xpath") or "").strip()
        if not selector:
            return -1
        field_type = str(payload.get("type") or "xpath").strip()
        if field_type not in FIELD_TYPES:
            field_type = "xpath"
        suggestion = str(payload.get("name") or payload.get("suggested_name") or "").strip()

        if 0 <= row < len(self._fields):
            base = self._fields[row]
            current = str(base.get("name") or "").strip()
            if suggestion and (not current or current.startswith("Новое поле")):
                base["name"] = self._unique_name(suggestion)
            base["type"] = field_type
            base["selector"] = selector
            target = row
        else:
            name = self._unique_name(suggestion or "Новое поле")
            base = new_field(name)
            base["type"] = field_type
            base["selector"] = selector
            self._fields.append(base)
            target = len(self._fields) - 1

        self._rebuild()
        if 0 <= target < self.table.rowCount():
            self.table.selectRow(target)
            self._scroll_to(target)
        self.changed.emit()
        return target

    def add_detected(self, detected: list[dict]) -> int:
        """Append auto-detected fields and return how many were added."""

        self._sync_fields()
        added = 0
        for item in detected:
            if not isinstance(item, dict):
                continue
            selector = str(item.get("selector") or "").strip()
            if not selector:
                continue
            field_type = str(item.get("type") or "xpath").strip()
            if field_type not in FIELD_TYPES:
                field_type = "xpath"
            name = self._unique_name(str(item.get("name") or "Поле"))
            field = new_field(name)
            field["type"] = field_type
            field["selector"] = selector
            field["strip_tags"] = bool(item.get("strip_tags"))
            self._fields.append(field)
            added += 1
        if added:
            self._rebuild()
            last = len(self._fields) - 1
            self.table.selectRow(last)
            self._scroll_to(last)
            self.changed.emit()
        return added

    def add_dynamic_block(self, payload: dict) -> int:
        """Add or update the single dynamic specifications block.

        The campaign engine supports one split-specifications source at a time,
        so re-applying a picked/detected block updates the existing dynamic
        field instead of creating a second one.
        """

        self._sync_fields()
        selector = str(payload.get("selector") or "").strip()
        if not selector:
            return -1
        source_name = str(payload.get("source_field") or "Характеристики").strip()
        if not source_name:
            source_name = "Характеристики"

        target = -1
        for index, field in enumerate(self._fields):
            if str(field.get("kind") or "") == "dynamic":
                target = index
                break
        if target < 0:
            # Campaigns created before this feature already store the specs
            # HTML in a plain outer-HTML field; upgrade it in place.
            for index, field in enumerate(self._fields):
                if (
                    str(field.get("type") or "") == "xpath_outer_html"
                    and str(field.get("name") or "").strip().casefold()
                    == source_name.casefold()
                ):
                    target = index
                    break

        if target >= 0:
            base = self._fields[target]
            used = {
                str(field.get("name") or "").casefold()
                for index, field in enumerate(self._fields)
                if index != target
            }
            desired = source_name
            if desired.casefold() in used:
                suffix = 2
                while f"{desired} ({suffix})".casefold() in used:
                    suffix += 1
                desired = f"{desired} ({suffix})"
            base["name"] = desired
            base["type"] = "xpath_outer_html"
            base["selector"] = selector
            base["strip_tags"] = False
            base["kind"] = "dynamic"
            base.setdefault("source", str(payload.get("source") or ""))
        else:
            name = self._unique_name(source_name)
            base = new_field(name)
            base["type"] = "xpath_outer_html"
            base["selector"] = selector
            base["strip_tags"] = False
            base["kind"] = "dynamic"
            base["source"] = str(payload.get("source") or "")
            self._fields.append(base)
            target = len(self._fields) - 1

        self._rebuild()
        if 0 <= target < self.table.rowCount():
            self.table.selectRow(target)
            self._scroll_to(target)
            item = self.table.item(target, 2)
            if item is not None:
                item.setToolTip(
                    "Динамические характеристики: HTML-блок разбирается плагином "
                    "на колонки «название → значение»."
                )
        self.changed.emit()
        return target

    def _scroll_to(self, row: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.table.scrollToItem(item)

    def _unique_name(self, name: str) -> str:
        cleaned = name.strip() or "Поле"
        existing = {str(item.get("name") or "").casefold() for item in self._fields}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                existing.add(item.text().strip().casefold())
        if cleaned.casefold() not in existing:
            return cleaned
        suffix = 2
        while f"{cleaned} ({suffix})".casefold() in existing:
            suffix += 1
        return f"{cleaned} ({suffix})"

    # ------------------------------------------------------------------
    # Table construction
    # ------------------------------------------------------------------
    def _rebuild(self) -> None:
        current = self.table.currentRow()
        self._loading = True
        self.table.clearContents()
        self.table.setRowCount(len(self._fields))
        for row, field in enumerate(self._fields):
            name = str(field.get("name") or f"Поле {row + 1}")
            name_item = QTableWidgetItem(name)
            name_item.setToolTip(name)
            self.table.setItem(row, 0, name_item)

            field_type = str(field.get("type") or "xpath")
            if field_type not in FIELD_TYPES:
                field_type = "xpath"
            combo = QComboBox()
            for type_key in FIELD_TYPES:
                combo.addItem(FIELD_TYPE_LABELS[type_key], type_key)
            combo.setCurrentIndex(max(0, combo.findData(field_type)))
            combo.currentIndexChanged.connect(
                lambda _index, target=row: self._on_type_changed(target)
            )
            self.table.setCellWidget(row, 1, combo)

            selector = str(field.get("selector") or "")
            selector_item = QTableWidgetItem(selector)
            selector_item.setToolTip(selector)
            self.table.setItem(row, 2, selector_item)

            checkbox = QCheckBox()
            checkbox.setChecked(bool(field.get("strip_tags")))
            checkbox.setToolTip("Удалять HTML-теги и нормализовать пробелы")
            checkbox.toggled.connect(lambda _checked: self._emit_changed())
            self.table.setCellWidget(row, 3, self._centered(checkbox))

            picker_button = QPushButton("Пикер")
            picker_button.setProperty("variant", "flat")
            picker_button.setCursor(Qt.CursorShape.PointingHandCursor)
            picker_button.setToolTip("Выбрать элемент на странице и получить XPath")
            picker_button.clicked.connect(
                lambda _checked=False, target=row: self.picker_requested.emit(target)
            )
            self.table.setCellWidget(row, 4, self._centered(picker_button))

            delete_button = QPushButton("Удалить")
            delete_button.setProperty("variant", "danger")
            delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
            delete_button.clicked.connect(
                lambda _checked=False, target=row: self._delete_row(target)
            )
            self.table.setCellWidget(row, 5, self._centered(delete_button))

            self._update_selector_state(row, field_type)

        self._loading = False
        if self.table.rowCount():
            row = min(max(current, 0), self.table.rowCount() - 1)
            self.table.selectRow(row)
        self._update_buttons()

    @staticmethod
    def _centered(widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(0)
        layout.addWidget(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return container

    def _update_selector_state(self, row: int, field_type: str | None = None) -> None:
        item = self.table.item(row, 2)
        combo = self.table.cellWidget(row, 1)
        if item is None or not isinstance(combo, QComboBox):
            return
        if field_type is None:
            field_type = str(combo.currentData())
        if field_type == "spec_url":
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setToolTip("Для типа «URL страницы» селектор не используется.")
            item.setForeground(QBrush(QColor("#9aa1ab")))
        else:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setForeground(QBrush(QColor("#202631")))
            if field_type == "regex":
                item.setToolTip("Регулярное выражение. Например: fullDescription:\"(.*?)\"")
            elif field_type == "xpath_attr":
                item.setToolTip("XPath к атрибуту. Например: //img/@src")
            elif field_type == "xpath_attr_list":
                item.setToolTip(
                    "XPath ко всем атрибутам: собирает все совпадения, "
                    "удаляет дубликаты и объединяет ссылки через «, »."
                )
            elif field_type == "xpath_outer_html":
                item.setToolTip("XPath к элементу; будет сохранён outer HTML.")
            else:
                item.setToolTip(item.text())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def add_field(self) -> None:
        self._sync_fields()
        existing = set(self.field_names())
        counter = 1
        name = "Новое поле"
        while name in existing:
            counter += 1
            name = f"Новое поле {counter}"
        field = new_field(name)
        self._fields.append(field)
        self._rebuild()
        row = self.table.rowCount() - 1
        self.table.selectRow(row)
        name_item = self.table.item(row, 0)
        if name_item is not None:
            self.table.editItem(name_item)
        self.changed.emit()

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self._delete_row(row)

    def _sync_fields(self) -> None:
        if self.table.rowCount():
            self._fields = self.fields()

    def _delete_row(self, row: int) -> None:
        self._sync_fields()
        if not (0 <= row < len(self._fields)):
            return
        self._fields.pop(row)
        self._rebuild()
        self.changed.emit()

    def move_selected(self, direction: int) -> None:
        self._sync_fields()
        row = self.table.currentRow()
        if row < 0:
            return
        target = row + direction
        if not (0 <= target < len(self._fields)):
            return
        self._fields[row], self._fields[target] = self._fields[target], self._fields[row]
        self._rebuild()
        self.table.selectRow(target)
        self.changed.emit()

    def _on_type_changed(self, row: int) -> None:
        combo = self.table.cellWidget(row, 1)
        if not isinstance(combo, QComboBox):
            return
        field_type = str(combo.currentData())
        item = self.table.item(row, 2)
        if item is not None:
            stored = item.data(Qt.ItemDataRole.UserRole) or ""
            if field_type == "spec_url":
                if item.text():
                    item.setData(Qt.ItemDataRole.UserRole, item.text())
                item.setText("")
            elif not item.text() and stored:
                item.setText(str(stored))
        self._update_selector_state(row, field_type)
        self._emit_changed()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() in (0, 2):
            item.setToolTip(item.text())
        if not self._loading:
            self.changed.emit()

    def _emit_changed(self) -> None:
        if not self._loading:
            self.changed.emit()

    def _update_buttons(self) -> None:
        row = self.table.currentRow()
        has_row = row >= 0
        self.remove_button.setEnabled(has_row)
        self.up_button.setEnabled(has_row and row > 0)
        self.down_button.setEnabled(has_row and row < self.table.rowCount() - 1)
