"""Tab with the list of start URLs."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.widgets import SectionHeader, compact_button


class InputTab(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        layout.addWidget(
            SectionHeader(
                "Входные данные",
                "Страницы, с которых начинается обход. Каждый URL будет открыт в Chromium; "
                "ссылки для дальнейшего обхода задаются на вкладке «Навигация и Сценарий».",
            )
        )

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/catalog — вставьте один или несколько URL")
        self.url_edit.returnPressed.connect(self._add_from_edit)
        add_row.addWidget(self.url_edit, 1)
        self.add_button = QPushButton("Добавить")
        self.add_button.setProperty("variant", "primary")
        self.add_button.clicked.connect(self._add_from_edit)
        add_row.addWidget(self.add_button)
        layout.addLayout(add_row)

        content = QHBoxLayout()
        content.setSpacing(10)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.setToolTip("Двойной щелчок — изменить URL. Строки можно перетаскивать.")
        content.addWidget(self.list, 1)

        side = QVBoxLayout()
        side.setSpacing(5)
        self.remove_button = compact_button("Удалить", "Удалить выбранные URL (Delete)")
        self.remove_button.clicked.connect(self.remove_selected)
        side.addWidget(self.remove_button)
        self.up_button = compact_button("Вверх", "Переместить выбранные URL выше")
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        side.addWidget(self.up_button)
        self.down_button = compact_button("Вниз", "Переместить выбранные URL ниже")
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        side.addWidget(self.down_button)
        self.import_button = compact_button("Импорт из файла", "Загрузить список URL из текстового файла")
        self.import_button.clicked.connect(self.import_file)
        side.addWidget(self.import_button)
        self.clear_button = compact_button("Очистить", "Удалить все URL")
        self.clear_button.clicked.connect(self.clear_all)
        side.addWidget(self.clear_button)
        side.addStretch(1)
        content.addLayout(side)
        layout.addLayout(content, 1)

        footer = QHBoxLayout()
        self.count_label = QLabel("0 URL")
        self.count_label.setObjectName("mutedLabel")
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        hint = QLabel("Дубликаты сохраняются в конфигурации, но при запуске обходятся один раз.")
        hint.setObjectName("mutedLabel")
        footer.addWidget(hint)
        layout.addLayout(footer)

        delete_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_shortcut.activated.connect(self.remove_selected)

        self.list.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def set_campaign(self, campaign: dict) -> None:
        self._loading = True
        self.list.clear()
        urls = (campaign.get("input") or {}).get("start_urls") or []
        for url in urls:
            item = QListWidgetItem(str(url))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(str(url))
            self.list.addItem(item)
        self._loading = False
        self._update_count()
        self._update_buttons()

    def start_urls(self) -> list[str]:
        result: list[str] = []
        for index in range(self.list.count()):
            value = self.list.item(index).text().strip()
            if value:
                result.append(value)
        return result

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------
    def _add_from_edit(self) -> None:
        raw = self.url_edit.text().strip()
        if not raw:
            self.url_edit.setFocus()
            return
        candidates: list[str] = []
        for chunk in raw.replace("\n", " ").split():
            value = chunk.strip()
            if not value:
                continue
            if "://" not in value and "." in value and not value.startswith(("/", "./")):
                value = f"https://{value}"
            candidates.append(value)
        if not candidates:
            return

        added = 0
        for url in candidates:
            if not self._looks_like_url(url):
                continue
            item = QListWidgetItem(url)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(url)
            self.list.addItem(item)
            added += 1

        if not added:
            QMessageBox.warning(
                self,
                "Некорректный URL",
                "Введите адрес вида https://example.com/page.",
            )
            return
        self.url_edit.clear()
        self.list.setCurrentRow(self.list.count() - 1)
        self._update_count()
        self.changed.emit()

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def remove_selected(self) -> None:
        rows = sorted({self.list.row(item) for item in self.list.selectedItems()}, reverse=True)
        if not rows:
            return
        for row in rows:
            self.list.takeItem(row)
        self._update_count()
        self.changed.emit()

    def move_selected(self, direction: int) -> None:
        count = self.list.count()
        selected = sorted(self.list.row(item) for item in self.list.selectedItems())
        if not selected:
            return
        if direction < 0 and selected[0] == 0:
            return
        if direction > 0 and selected[-1] == count - 1:
            return

        entries = [
            (self.list.item(index).text(), index in selected)
            for index in range(count)
        ]
        if direction < 0:
            for index in range(1, count):
                if entries[index][1] and not entries[index - 1][1]:
                    entries[index], entries[index - 1] = entries[index - 1], entries[index]
        else:
            for index in range(count - 2, -1, -1):
                if entries[index][1] and not entries[index + 1][1]:
                    entries[index], entries[index + 1] = entries[index + 1], entries[index]

        self._loading = True
        self.list.clear()
        new_rows: list[int] = []
        for index, (value, is_selected) in enumerate(entries):
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(value)
            self.list.addItem(item)
            if is_selected:
                new_rows.append(index)
        self._loading = False
        self.list.clearSelection()
        for row in new_rows:
            if 0 <= row < self.list.count():
                self.list.item(row).setSelected(True)
        self.changed.emit()

    def clear_all(self) -> None:
        if self.list.count() == 0:
            return
        answer = QMessageBox.question(
            self,
            "Очистить список",
            "Удалить все стартовые URL?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.list.clear()
        self._update_count()
        self.changed.emit()

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт URL",
            str(Path.home()),
            "Текстовые файлы (*.txt *.csv *.list);;Все файлы (*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            QMessageBox.critical(self, "Импорт URL", f"Не удалось прочитать файл:\n{exc}")
            return
        urls = []
        for line in text.splitlines():
            value = line.strip().strip('"').strip("'")
            if not value or value.startswith("#"):
                continue
            if "://" not in value and "." in value:
                value = f"https://{value}"
            if self._looks_like_url(value):
                urls.append(value)
        if not urls:
            QMessageBox.information(self, "Импорт URL", "В файле не найдено корректных http(s) URL.")
            return
        for url in urls:
            item = QListWidgetItem(url)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(url)
            self.list.addItem(item)
        self._update_count()
        self.changed.emit()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        item.setToolTip(item.text().strip())
        if not self._loading:
            self.changed.emit()

    def _update_count(self) -> None:
        self.count_label.setText(f"{self.list.count()} URL")

    def _update_buttons(self) -> None:
        has_selection = bool(self.list.selectedItems())
        self.remove_button.setEnabled(has_selection)
        self.up_button.setEnabled(has_selection)
        self.down_button.setEnabled(has_selection)
