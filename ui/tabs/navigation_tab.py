"""Navigation, engine settings and scenario editor."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.schema import (
    SCENARIO_ACTION_LABELS,
    SCENARIO_ACTIONS,
    new_scenario_step,
)
from ui.widgets import SectionHeader, compact_button


class NavigationTab(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._scenario: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(
            SectionHeader(
                "Навигация и Сценарий",
                "Движок обходит стартовые URL, выполняет сценарий и собирает ссылки для следующих страниц.",
            )
        )

        navigation_group = QGroupBox("Сбор ссылок и глубина обхода")
        navigation_layout = QVBoxLayout(navigation_group)
        navigation_layout.setSpacing(6)

        self.links_table = QTableWidget(0, 1)
        self.links_table.setHorizontalHeaderLabels(["XPath ссылок или пагинации"])
        self.links_table.verticalHeader().setVisible(False)
        self.links_table.verticalHeader().setDefaultSectionSize(34)
        self.links_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.links_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.links_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.links_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.links_table.verticalHeader().setDefaultSectionSize(34)
        self.links_table.itemChanged.connect(self._on_link_changed)
        self.links_table.itemSelectionChanged.connect(self._update_buttons)
        self.links_table.setMinimumHeight(92)
        self.links_table.setMaximumHeight(118)
        navigation_layout.addWidget(self.links_table)

        links_buttons = QHBoxLayout()
        links_buttons.setSpacing(5)
        self.add_link_button = compact_button("Добавить XPath", "Добавить правило сбора ссылок")
        self.add_link_button.clicked.connect(self.add_link)
        links_buttons.addWidget(self.add_link_button)
        self.remove_link_button = compact_button("Удалить", "Удалить выбранный XPath")
        self.remove_link_button.clicked.connect(self.remove_link)
        links_buttons.addWidget(self.remove_link_button)
        self.up_link_button = compact_button("Вверх", "Переместить XPath выше")
        self.up_link_button.clicked.connect(lambda: self.move_link(-1))
        links_buttons.addWidget(self.up_link_button)
        self.down_link_button = compact_button("Вниз", "Переместить XPath ниже")
        self.down_link_button.clicked.connect(lambda: self.move_link(1))
        links_buttons.addWidget(self.down_link_button)
        links_buttons.addStretch(1)
        depth_label = QLabel("Максимальная глубина:")
        links_buttons.addWidget(depth_label)
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 1000)
        self.depth_spin.setValue(10)
        self.depth_spin.setFixedWidth(85)
        self.depth_spin.setToolTip("0 — только стартовые страницы")
        self.depth_spin.valueChanged.connect(self._emit_changed)
        links_buttons.addWidget(self.depth_spin)
        navigation_layout.addLayout(links_buttons)
        layout.addWidget(navigation_group)

        engine_group = QGroupBox("Движок")
        engine_layout = QGridLayout(engine_group)
        engine_layout.setHorizontalSpacing(12)
        engine_layout.setVerticalSpacing(7)

        mode_label = QLabel("Режим:")
        mode_value = QLabel("Playwright Chromium")
        mode_value.setObjectName("statusStrong")
        engine_layout.addWidget(mode_label, 0, 0)
        engine_layout.addWidget(mode_value, 0, 1)

        threads_label = QLabel("Потоков:")
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 32)
        self.threads_spin.setValue(3)
        self.threads_spin.valueChanged.connect(self._emit_changed)
        engine_layout.addWidget(threads_label, 0, 2)
        engine_layout.addWidget(self.threads_spin, 0, 3)

        self.headless_checkbox = QCheckBox("Headless (без окна браузера)")
        self.headless_checkbox.toggled.connect(self._emit_changed)
        self.images_checkbox = QCheckBox("Не загружать изображения")
        self.images_checkbox.setChecked(True)
        self.images_checkbox.setToolTip(
            "Блокировать запросы изображений. Атрибуты src / data-src остаются в HTML."
        )
        self.images_checkbox.toggled.connect(self._emit_changed)
        self.heavy_checkbox = QCheckBox("Блокировать тяжёлые ресурсы")
        self.heavy_checkbox.setChecked(True)
        self.heavy_checkbox.setToolTip(
            "Не загружать изображения, медиа, шрифты и трекеры — быстрее и экономнее. "
            "Скрипты, стили, XHR и сами страницы загружаются как обычно."
        )
        self.heavy_checkbox.toggled.connect(self._emit_changed)
        engine_options = QVBoxLayout()
        engine_options.setSpacing(4)
        engine_options.addWidget(self.headless_checkbox)
        engine_options.addWidget(self.images_checkbox)
        engine_options.addWidget(self.heavy_checkbox)
        engine_layout.addLayout(engine_options, 0, 4, 2, 2)

        delay_label = QLabel("Задержка между страницами:")
        engine_layout.addWidget(delay_label, 1, 0)
        delay_row = QHBoxLayout()
        delay_row.setSpacing(5)
        self.delay_min_spin = QSpinBox()
        self.delay_min_spin.setRange(0, 600000)
        self.delay_min_spin.setSingleStep(500)
        self.delay_min_spin.setSuffix(" мс")
        self.delay_min_spin.setValue(3000)
        self.delay_min_spin.valueChanged.connect(self._emit_changed)
        delay_row.addWidget(self.delay_min_spin)
        delay_row.addWidget(QLabel("—"))
        self.delay_max_spin = QSpinBox()
        self.delay_max_spin.setRange(0, 600000)
        self.delay_max_spin.setSingleStep(500)
        self.delay_max_spin.setSuffix(" мс")
        self.delay_max_spin.setValue(7000)
        self.delay_max_spin.valueChanged.connect(self._emit_changed)
        delay_row.addWidget(self.delay_max_spin)
        delay_row.addStretch(1)
        engine_layout.addLayout(delay_row, 1, 1, 1, 3)

        proxy_label = QLabel("Прокси (по одному на строку):")
        proxy_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        engine_layout.addWidget(proxy_label, 2, 0, 1, 2)
        self.proxy_edit = QPlainTextEdit()
        self.proxy_edit.setPlaceholderText("http://user:password@host:port\nsocks5://host:port")
        self.proxy_edit.setFixedHeight(62)
        self.proxy_edit.textChanged.connect(self._emit_changed)
        engine_layout.addWidget(self.proxy_edit, 2, 2, 1, 4)
        layout.addWidget(engine_group)

        scenario_group = QGroupBox("Сценарий перед извлечением данных")
        scenario_layout = QVBoxLayout(scenario_group)
        scenario_layout.setSpacing(6)
        scenario_hint = QLabel(
            "Шаги выполняются по порядку на каждой странице: клик/наведение по XPath, пауза или прокрутка. "
            "Необязательный шаг не прерывает обработку страницы при ошибке."
        )
        scenario_hint.setObjectName("sectionHint")
        scenario_hint.setWordWrap(True)
        scenario_layout.addWidget(scenario_hint)

        self.scenario_table = QTableWidget(0, 5)
        self.scenario_table.setHorizontalHeaderLabels(
            ["Действие", "XPath элемента", "Пауза, мс", "Необязательный", ""]
        )
        self.scenario_table.verticalHeader().setVisible(False)
        self.scenario_table.verticalHeader().setDefaultSectionSize(36)
        self.scenario_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.scenario_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.scenario_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        header = self.scenario_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.scenario_table.setColumnWidth(0, 155)
        self.scenario_table.setColumnWidth(2, 115)
        self.scenario_table.setColumnWidth(3, 125)
        self.scenario_table.setColumnWidth(4, 55)
        self.scenario_table.itemChanged.connect(self._on_scenario_item_changed)
        self.scenario_table.itemSelectionChanged.connect(self._update_buttons)
        self.scenario_table.setMinimumHeight(112)
        self.scenario_table.setMaximumHeight(170)
        scenario_layout.addWidget(self.scenario_table)

        scenario_buttons = QHBoxLayout()
        scenario_buttons.setSpacing(5)
        self.add_step_button = QPushButton("Добавить шаг")
        self.add_step_button.setProperty("variant", "primary")
        self.add_step_button.clicked.connect(self.add_step)
        scenario_buttons.addWidget(self.add_step_button)
        self.remove_step_button = compact_button("Удалить", "Удалить выбранный шаг")
        self.remove_step_button.clicked.connect(self.remove_step)
        scenario_buttons.addWidget(self.remove_step_button)
        self.up_step_button = compact_button("Вверх", "Переместить шаг выше")
        self.up_step_button.clicked.connect(lambda: self.move_step(-1))
        scenario_buttons.addWidget(self.up_step_button)
        self.down_step_button = compact_button("Вниз", "Переместить шаг ниже")
        self.down_step_button.clicked.connect(lambda: self.move_step(1))
        scenario_buttons.addWidget(self.down_step_button)
        scenario_buttons.addStretch(1)
        scenario_layout.addLayout(scenario_buttons)
        layout.addWidget(scenario_group)
        layout.addStretch(1)

    # ------------------------------------------------------------------
    # Loading / reading
    # ------------------------------------------------------------------
    def set_campaign(self, campaign: dict) -> None:
        self._loading = True
        navigation = campaign.get("navigation") if isinstance(campaign.get("navigation"), dict) else {}
        links = navigation.get("gather_links_xpaths") or []
        self.links_table.setRowCount(0)
        for xpath in links:
            self._append_link_row(str(xpath))
        self.depth_spin.setValue(max(0, int(navigation.get("max_depth", 10) or 0)))

        engine = campaign.get("engine") if isinstance(campaign.get("engine"), dict) else {}
        self.threads_spin.setValue(max(1, min(32, int(engine.get("threads", 3) or 3))))
        self.delay_min_spin.setValue(max(0, int(engine.get("delay_min_ms", 3000) or 0)))
        self.delay_max_spin.setValue(max(0, int(engine.get("delay_max_ms", 7000) or 0)))
        self.headless_checkbox.setChecked(bool(engine.get("headless", False)))
        self.images_checkbox.setChecked(bool(engine.get("disable_images", True)))
        self.heavy_checkbox.setChecked(bool(engine.get("block_heavy_resources", True)))
        self.proxy_edit.setPlainText(str(engine.get("proxy_list") or ""))

        scenario = campaign.get("scenario") if isinstance(campaign.get("scenario"), list) else []
        self._scenario = [copy.deepcopy(step) for step in scenario if isinstance(step, dict)]
        self._rebuild_scenario()
        self._loading = False
        self._update_buttons()

    def navigation(self) -> dict:
        xpaths: list[str] = []
        for row in range(self.links_table.rowCount()):
            item = self.links_table.item(row, 0)
            if item and item.text().strip():
                xpaths.append(item.text().strip())
        return {"gather_links_xpaths": xpaths, "max_depth": self.depth_spin.value()}

    def engine_settings(self) -> dict:
        return {
            "mode": "playwright_chromium",
            "headless": self.headless_checkbox.isChecked(),
            "threads": self.threads_spin.value(),
            "delay_min_ms": self.delay_min_spin.value(),
            "delay_max_ms": self.delay_max_spin.value(),
            "disable_images": self.images_checkbox.isChecked(),
            "block_heavy_resources": self.heavy_checkbox.isChecked(),
            "proxy_list": self.proxy_edit.toPlainText().strip(),
        }

    def scenario(self) -> list[dict]:
        steps: list[dict] = []
        for row in range(self.scenario_table.rowCount()):
            combo = self.scenario_table.cellWidget(row, 0)
            xpath_item = self.scenario_table.item(row, 1)
            wait_spin = self.scenario_table.cellWidget(row, 2)
            optional = self.scenario_table.cellWidget(row, 3)
            action = str(combo.currentData()) if isinstance(combo, QComboBox) else "click"
            steps.append(
                {
                    "action": action,
                    "xpath": xpath_item.text().strip() if xpath_item else "",
                    "wait_ms": wait_spin.value() if isinstance(wait_spin, QSpinBox) else 0,
                    "optional": optional.isChecked() if isinstance(optional, QCheckBox) else True,
                }
            )
        return steps

    def has_configured_scenario(self) -> bool:
        return self.scenario_table.rowCount() > 0

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------
    def _append_link_row(self, xpath: str) -> None:
        row = self.links_table.rowCount()
        self.links_table.insertRow(row)
        item = QTableWidgetItem(xpath)
        item.setToolTip(xpath)
        self.links_table.setItem(row, 0, item)

    def add_link(self) -> None:
        self._append_link_row("")
        row = self.links_table.rowCount() - 1
        self.links_table.selectRow(row)
        item = self.links_table.item(row, 0)
        if item is not None:
            self.links_table.editItem(item)
        self._emit_changed()

    def remove_link(self) -> None:
        row = self.links_table.currentRow()
        if row < 0:
            return
        self.links_table.removeRow(row)
        self._emit_changed()

    def move_link(self, direction: int) -> None:
        current = self.links_table.currentRow()
        target = current + direction
        if current < 0 or not (0 <= target < self.links_table.rowCount()):
            return
        current_item = self.links_table.takeItem(current, 0)
        target_item = self.links_table.takeItem(target, 0)
        self.links_table.setItem(current, 0, target_item)
        self.links_table.setItem(target, 0, current_item)
        self.links_table.selectRow(target)
        self._emit_changed()

    def _on_link_changed(self, item: QTableWidgetItem) -> None:
        item.setToolTip(item.text())
        self._emit_changed()

    # ------------------------------------------------------------------
    # Scenario
    # ------------------------------------------------------------------
    def _rebuild_scenario(self) -> None:
        current = self.scenario_table.currentRow()
        self.scenario_table.clearContents()
        self.scenario_table.setRowCount(len(self._scenario))
        for row, step in enumerate(self._scenario):
            action = str(step.get("action") or "click")
            combo = QComboBox()
            for key in SCENARIO_ACTIONS:
                combo.addItem(SCENARIO_ACTION_LABELS[key], key)
            index = combo.findData(action)
            combo.setCurrentIndex(max(0, index))
            combo.currentIndexChanged.connect(
                lambda _index, target=row: self._on_action_changed(target)
            )
            self.scenario_table.setCellWidget(row, 0, combo)

            xpath_item = QTableWidgetItem(str(step.get("xpath") or ""))
            xpath_item.setToolTip(str(step.get("xpath") or ""))
            self.scenario_table.setItem(row, 1, xpath_item)

            wait_spin = QSpinBox()
            wait_spin.setRange(0, 600000)
            wait_spin.setSingleStep(100)
            wait_spin.setSuffix(" мс")
            wait_spin.setValue(max(0, int(step.get("wait_ms", 0) or 0)))
            wait_spin.valueChanged.connect(self._emit_changed)
            self.scenario_table.setCellWidget(row, 2, wait_spin)

            optional = QCheckBox()
            optional.setChecked(bool(step.get("optional", True)))
            optional.setToolTip("Не прерывать обработку страницы при ошибке шага")
            optional.toggled.connect(self._emit_changed)
            self.scenario_table.setCellWidget(row, 3, self._centered(optional))

            remove_button = QPushButton("✕")
            remove_button.setProperty("variant", "danger")
            remove_button.setFixedWidth(34)
            remove_button.setToolTip("Удалить шаг")
            remove_button.clicked.connect(
                lambda _checked=False, target=row: self._delete_step(target)
            )
            self.scenario_table.setCellWidget(row, 4, self._centered(remove_button))
            self._update_xpath_state(row, action)

        if self.scenario_table.rowCount():
            self.scenario_table.selectRow(min(max(current, 0), self.scenario_table.rowCount() - 1))
        self._update_buttons()

    @staticmethod
    def _centered(widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(3, 0, 3, 0)
        layout.setSpacing(0)
        layout.addWidget(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return container

    def add_step(self) -> None:
        self._sync_scenario()
        self._scenario.append(new_scenario_step())
        self._rebuild_scenario()
        row = self.scenario_table.rowCount() - 1
        self.scenario_table.selectRow(row)
        self._emit_changed()

    def remove_step(self) -> None:
        row = self.scenario_table.currentRow()
        if row >= 0:
            self._delete_step(row)

    def _sync_scenario(self) -> None:
        if self.scenario_table.rowCount():
            self._scenario = self.scenario()

    def _delete_step(self, row: int) -> None:
        self._sync_scenario()
        if not (0 <= row < len(self._scenario)):
            return
        self._scenario.pop(row)
        self._rebuild_scenario()
        self._emit_changed()

    def move_step(self, direction: int) -> None:
        self._sync_scenario()
        row = self.scenario_table.currentRow()
        target = row + direction
        if row < 0 or not (0 <= target < len(self._scenario)):
            return
        self._scenario[row], self._scenario[target] = self._scenario[target], self._scenario[row]
        self._rebuild_scenario()
        self.scenario_table.selectRow(target)
        self._emit_changed()

    def _on_action_changed(self, row: int) -> None:
        combo = self.scenario_table.cellWidget(row, 0)
        if isinstance(combo, QComboBox):
            self._update_xpath_state(row, str(combo.currentData()))
        self._emit_changed()

    def _update_xpath_state(self, row: int, action: str) -> None:
        item = self.scenario_table.item(row, 1)
        if item is None:
            return
        if action in {"click", "hover"}:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setForeground(QBrush(QColor("#202631")))
            item.setToolTip("XPath элемента для действия")
        else:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setForeground(QBrush(QColor("#9aa1ab")))
            item.setToolTip("Для этого действия XPath не используется")

    def _on_scenario_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 1:
            item.setToolTip(item.text())
            self._emit_changed()

    # ------------------------------------------------------------------
    # Common
    # ------------------------------------------------------------------
    def _emit_changed(self) -> None:
        self._update_buttons()
        if not self._loading:
            self.changed.emit()

    def _update_buttons(self) -> None:
        link_row = self.links_table.currentRow()
        self.remove_link_button.setEnabled(link_row >= 0)
        self.up_link_button.setEnabled(link_row > 0)
        self.down_link_button.setEnabled(0 <= link_row < self.links_table.rowCount() - 1)

        step_row = self.scenario_table.currentRow()
        self.remove_step_button.setEnabled(step_row >= 0)
        self.up_step_button.setEnabled(step_row > 0)
        self.down_step_button.setEnabled(0 <= step_row < self.scenario_table.rowCount() - 1)
