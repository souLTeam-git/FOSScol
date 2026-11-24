"""Plugins and export settings tab.

Every plugin is a regular ``.py`` file in the project ``plugins`` folder and
can be installed, disabled, deleted and reinstalled from this tab.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.plugins.registry import get_registry, plugins_dir
from core.specs_columns import (
    flatten_priority_columns,
    parse_priority_columns,
    parse_priority_text,
)
from ui.widgets import PathEdit, SectionHeader


class PluginsTab(QWidget):
    changed = Signal()

    def __init__(self, project_root: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.registry = get_registry(project_root)
        self._loading = False
        self._configs: dict[str, dict] = {}
        self._field_names: list[str] = []
        self._priority_groups: list[list[str]] = []
        self._priority_loaded_text = ""
        self._plugin_cards: dict[str, QWidget] = {}
        self._generic_checkboxes: dict[str, QCheckBox] = {}
        self._schema_widgets: dict[str, dict[str, QWidget]] = {}
        self._schema_getters: dict[str, list[tuple[str, Any]]] = {}
        self._field_combos: list[tuple[QComboBox, bool]] = []

        # Widgets of plugins with a dedicated settings card.  They are ``None``
        # while the corresponding plugin file is not installed.
        self.split_checkbox: QCheckBox | None = None
        self.form_widget: QWidget | None = None
        self.source_combo: QComboBox | None = None
        self.row_xpath_edit: QLineEdit | None = None
        self.name_xpath_edit: QLineEdit | None = None
        self.value_xpath_edit: QLineEdit | None = None
        self.priority_edit: QLineEdit | None = None
        self.separator_checkbox: QCheckBox | None = None
        self.separator_form: QWidget | None = None
        self.separator_edit: QLineEdit | None = None

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
                "Плагины и Экспорт",
                "Дополнительная обработка результатов и файл, в который сохраняется выборка.",
            )
        )

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.refresh_button = QPushButton("Обновить список")
        self.refresh_button.setMinimumHeight(32)
        self.refresh_button.setToolTip("Пересканировать папку plugins/.")
        self.refresh_button.clicked.connect(self._refresh_plugins)
        toolbar.addWidget(self.refresh_button)

        self.install_button = QPushButton("Установить плагин (.py)...")
        self.install_button.setMinimumHeight(32)
        self.install_button.clicked.connect(self._install_plugin)
        toolbar.addWidget(self.install_button)

        self.open_folder_button = QPushButton("Открыть папку плагинов")
        self.open_folder_button.setMinimumHeight(32)
        self.open_folder_button.setToolTip(
            "Создать папку plugins/ (если её нет) и открыть её в системном файловом менеджере."
        )
        self.open_folder_button.clicked.connect(self._open_plugins_folder)
        toolbar.addWidget(self.open_folder_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        manage_row = QHBoxLayout()
        manage_row.setSpacing(8)
        manage_row.addWidget(QLabel("Плагин:"))
        self.plugin_combo = QComboBox()
        self.plugin_combo.setMinimumWidth(260)
        self.plugin_combo.setMinimumHeight(32)
        self.plugin_combo.currentIndexChanged.connect(self._update_delete_button)
        manage_row.addWidget(self.plugin_combo, 1)
        self.delete_button = QPushButton("Удалить плагин")
        self.delete_button.setMinimumHeight(32)
        self.delete_button.clicked.connect(self._delete_plugin)
        manage_row.addWidget(self.delete_button)
        layout.addLayout(manage_row)

        self.registry_label = QLabel("")
        self.registry_label.setObjectName("mutedLabel")
        self.registry_label.setWordWrap(True)
        layout.addWidget(self.registry_label)

        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(8)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.cards_layout)

        export_group = QGroupBox("Экспорт")
        export_layout = QGridLayout(export_group)
        export_layout.setHorizontalSpacing(12)
        export_layout.setVerticalSpacing(7)

        export_layout.addWidget(QLabel("Формат:"), 0, 0)
        format_row = QHBoxLayout()
        format_row.setSpacing(8)
        self.format_combo = QComboBox()
        self.format_combo.addItem("XLSX (Excel)", "xlsx")
        self.format_combo.addItem("CSV (UTF-8, разделитель ;)", "csv")
        self.format_combo.setFixedWidth(240)
        self.format_combo.setMinimumHeight(32)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        format_row.addWidget(self.format_combo)
        self.format_hint = QLabel("")
        self.format_hint.setObjectName("mutedLabel")
        format_row.addWidget(self.format_hint)
        format_row.addStretch(1)
        export_layout.addLayout(format_row, 0, 1)

        export_layout.addWidget(QLabel("Путь сохранения:"), 1, 0)
        self.path_edit = PathEdit(
            mode="save_file",
            file_filter="Excel (*.xlsx);;Все файлы (*)",
            default_suffix="xlsx",
            base_dir=project_root,
        )
        self.path_edit.set_placeholder("./exports/Результат.xlsx")
        self.path_edit.edit.setMinimumHeight(32)
        self.path_edit.button.setMinimumHeight(32)
        self.path_edit.path_changed.connect(self._emit_changed)
        export_layout.addWidget(self.path_edit, 1, 1)

        note = QLabel(
            "Относительные пути считаются от папки приложения. Кнопка «Экспорт» на панели "
            "инструментов сохраняет текущие результаты по этому пути."
        )
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        export_layout.addWidget(note, 2, 1)
        export_layout.setColumnStretch(1, 1)
        layout.addWidget(export_group)
        layout.addStretch(1)

        self._rebuild_cards()

    # ------------------------------------------------------------------
    # Card construction
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_config_schema(plugin) -> list[dict[str, Any]]:
        """Return a validated schema, ignoring broken third-party plugins."""

        method = getattr(plugin, "config_schema", None)
        if not callable(method):
            return []
        try:
            schema = method()
        except Exception:  # noqa: BLE001 - a broken plugin must not crash the tab
            return []
        if not isinstance(schema, (list, tuple)):
            return []
        result: list[dict[str, Any]] = []
        for item in schema:
            if not isinstance(item, dict):
                continue
            if str(item.get("key") or "").strip():
                result.append(dict(item))
        return result

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _choice_options(options: Any) -> list[tuple[Any, str]]:
        if not isinstance(options, (list, tuple)):
            return []
        result: list[tuple[Any, str]] = []
        for option in options:
            if isinstance(option, (list, tuple)) and len(option) >= 2:
                result.append((option[0], str(option[1])))
            else:
                result.append((option, str(option)))
        return result

    @staticmethod
    def _combo_index(combo: QComboBox, value: Any) -> int:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                return index
        return -1

    def _config_for_entry(self, plugin_id: str, plugin) -> dict:
        try:
            defaults = plugin.default_config()
        except Exception:  # noqa: BLE001 - a broken plugin must not crash the tab
            defaults = {}
        config = copy.deepcopy(defaults) if isinstance(defaults, dict) else {}
        current = self._configs.get(plugin_id)
        if isinstance(current, dict):
            config.update(copy.deepcopy(current))
        # Schema defaults cover plugins that expose settings without repeating
        # every key in ``default_config()``.
        for spec in self._safe_config_schema(plugin):
            key = str(spec.get("key") or "").strip()
            if key and key not in config and "default" in spec:
                config[key] = copy.deepcopy(spec["default"])
        self._configs[plugin_id] = config
        return config

    def _create_schema_widget(self, kind: str, spec: dict, value: Any) -> QWidget | None:
        if kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(value))
            widget.toggled.connect(self._emit_changed)
            return widget

        if kind == "int":
            widget = QSpinBox()
            minimum = self._as_int(spec.get("min"), 0)
            maximum = self._as_int(spec.get("max"), 999999)
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            widget.setRange(minimum, maximum)
            widget.setValue(max(minimum, min(maximum, self._as_int(value, minimum))))
            widget.setMinimumHeight(32)
            widget.valueChanged.connect(self._emit_changed)
            return widget

        if kind == "choice":
            widget = QComboBox()
            for option_value, option_label in self._choice_options(spec.get("options")):
                widget.addItem(option_label, option_value)
            index = self._combo_index(widget, value)
            if index < 0 and value not in (None, ""):
                # Preserve a saved value that is not in the current options.
                widget.addItem(str(value), value)
                index = widget.count() - 1
            widget.setCurrentIndex(max(0, index))
            widget.setMinimumHeight(32)
            widget.currentIndexChanged.connect(self._emit_changed)
            return widget

        if kind == "field_name":
            widget = QComboBox()
            widget.setEditable(True)
            widget.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            allow_empty = bool(spec.get("allow_empty", False))
            if allow_empty:
                widget.addItem("Все / не выбрано", "")
            for name in self._field_names:
                widget.addItem(name, name)
            text = "" if value is None else str(value)
            index = widget.findData(text)
            if index >= 0:
                widget.setCurrentIndex(index)
            else:
                widget.setEditText(text)
            widget.setMinimumHeight(32)
            widget.currentTextChanged.connect(self._emit_changed)
            self._field_combos.append((widget, allow_empty))
            return widget

        if kind != "str":
            return None
        widget = QLineEdit()
        placeholder = str(spec.get("placeholder") or "")
        if placeholder:
            widget.setPlaceholderText(placeholder)
        widget.setText("" if value is None else str(value))
        widget.setMinimumHeight(32)
        widget.textChanged.connect(self._emit_changed)
        return widget

    @staticmethod
    def _schema_getter(widget: QWidget, kind: str):
        if kind == "bool":
            return widget.isChecked
        if kind == "int":
            return widget.value
        if kind == "choice":
            return lambda: widget.currentData()
        if kind == "field_name":
            return lambda: widget.currentText().strip()
        return widget.text

    def _build_schema_form(self, plugin, config: dict) -> QWidget | None:
        specs = self._safe_config_schema(plugin)
        if not specs:
            return None

        form_widget = QWidget()
        form = QGridLayout(form_widget)
        form.setContentsMargins(22, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(7)

        widgets: dict[str, QWidget] = {}
        getters: list[tuple[str, Any]] = []
        row = 0
        for spec in specs:
            key = str(spec.get("key") or "").strip()
            if not key or key in widgets:
                continue
            kind = str(spec.get("type") or "str").strip().lower()
            value = config.get(key, spec.get("default"))
            widget = self._create_schema_widget(kind, spec, value)
            if widget is None:
                continue
            label = QLabel(str(spec.get("label") or key))
            tooltip = str(spec.get("tooltip") or spec.get("hint") or "").strip()
            if tooltip:
                label.setToolTip(tooltip)
                widget.setToolTip(tooltip)
            form.addWidget(label, row, 0)
            form.addWidget(widget, row, 1)
            widgets[key] = widget
            getters.append((key, self._schema_getter(widget, kind)))
            row += 1

        if not widgets:
            return None
        form.setColumnStretch(1, 1)
        self._schema_widgets[plugin.id] = widgets
        self._schema_getters[plugin.id] = getters
        return form_widget

    def _build_specs_card(self, plugin, config: dict) -> QGroupBox:
        group = QGroupBox(str(plugin.name or plugin.id))
        plugins_layout = QVBoxLayout(group)
        plugins_layout.setSpacing(8)

        self.split_checkbox = QCheckBox("Расфасовка характеристик по колонкам")
        self.split_checkbox.setToolTip(
            "Разбирает HTML-таблицу характеристик на отдельные колонки «название → значение»."
        )
        self.split_checkbox.setChecked(bool(config.get("enabled", False)))
        self.split_checkbox.toggled.connect(self._on_split_toggled)
        plugins_layout.addWidget(self.split_checkbox)

        self.form_widget = QWidget()
        form = QGridLayout(self.form_widget)
        form.setContentsMargins(22, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(7)

        form.addWidget(QLabel("Исходное поле:"), 0, 0)
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(220)
        self.source_combo.setMinimumHeight(32)
        form.addWidget(self.source_combo, 0, 1)

        form.addWidget(QLabel("XPath строк:"), 1, 0)
        self.row_xpath_edit = QLineEdit()
        self.row_xpath_edit.setPlaceholderText("//tr")
        self.row_xpath_edit.setMinimumHeight(32)
        form.addWidget(self.row_xpath_edit, 1, 1)

        form.addWidget(QLabel("XPath названий:"), 2, 0)
        self.name_xpath_edit = QLineEdit()
        self.name_xpath_edit.setPlaceholderText("//tr/td[1]")
        self.name_xpath_edit.setMinimumHeight(32)
        form.addWidget(self.name_xpath_edit, 2, 1)

        form.addWidget(QLabel("XPath значений:"), 3, 0)
        self.value_xpath_edit = QLineEdit()
        self.value_xpath_edit.setPlaceholderText("//tr/td[2]")
        self.value_xpath_edit.setMinimumHeight(32)
        form.addWidget(self.value_xpath_edit, 3, 1)

        form.addWidget(QLabel("Приоритетные колонки (через запятую):"), 4, 0)
        self.priority_edit = QLineEdit()
        self.priority_edit.setPlaceholderText("Бренд, Виробник, Производитель, Тип, Вид")
        self.priority_edit.setMinimumHeight(32)
        self.priority_edit.setToolTip(
            "Названия характеристик через запятую. Совпавшие колонки (без учёта "
            "регистра и лишних пробелов) выводятся первыми среди динамических, "
            "остальные — в порядке появления в таблице."
        )
        form.addWidget(self.priority_edit, 4, 1)

        form.setColumnStretch(1, 1)
        plugins_layout.addWidget(self.form_widget)
        hint = QLabel(
            "Поле «Исходное поле» должно содержать HTML таблицы характеристик — обычно это поле "
            "с типом «XPath (outer HTML)». Названия и значения сопоставляются по индексу строки. "
            "«Приоритетные колонки» задают порядок динамических столбцов: перечисленные "
            "характеристики идут сразу после базовых полей, остальные — за ними."
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        plugins_layout.addWidget(hint)

        source = str(config.get("source_field") or config.get("specs_source_field") or "")
        self._fill_source_combo(source)
        self.row_xpath_edit.setText(str(config.get("row_xpath") or config.get("specs_row_xpath") or ""))
        self.name_xpath_edit.setText(str(config.get("name_xpath") or config.get("specs_name_xpath") or ""))
        self.value_xpath_edit.setText(str(config.get("value_xpath") or config.get("specs_val_xpath") or ""))
        self._priority_groups = parse_priority_columns(config.get("priority_columns"))
        priority_text = ", ".join(flatten_priority_columns(self._priority_groups))
        self._priority_loaded_text = priority_text
        self.priority_edit.setText(priority_text)

        self.source_combo.currentIndexChanged.connect(self._emit_changed)
        self.row_xpath_edit.textChanged.connect(self._emit_changed)
        self.name_xpath_edit.textChanged.connect(self._emit_changed)
        self.value_xpath_edit.textChanged.connect(self._emit_changed)
        self.priority_edit.textChanged.connect(self._emit_changed)
        self.form_widget.setEnabled(bool(config.get("enabled", False)))
        return group

    def _build_list_card(self, plugin, config: dict) -> QGroupBox:
        group = QGroupBox(str(plugin.name or plugin.id))
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        self.separator_checkbox = QCheckBox(
            "Склеивать «все картинки» выбранным разделителем"
        )
        self.separator_checkbox.setToolTip(
            "Превращает список изображений в одну строку с выбранным разделителем."
        )
        self.separator_checkbox.setChecked(bool(config.get("enabled", True)))
        self.separator_checkbox.toggled.connect(self._on_separator_toggled)
        layout.addWidget(self.separator_checkbox)

        self.separator_form = QWidget()
        form = QGridLayout(self.separator_form)
        form.setContentsMargins(22, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(7)

        form.addWidget(QLabel("Разделитель:"), 0, 0)
        self.separator_edit = QLineEdit()
        self.separator_edit.setPlaceholderText(", ")
        self.separator_edit.setMinimumHeight(32)
        form.addWidget(self.separator_edit, 0, 1)

        form.setColumnStretch(1, 1)
        layout.addWidget(self.separator_form)

        hint = QLabel(
            "Разделитель применяется только к столбцу «все картинки». "
            "Остальные списковые поля склеиваются через запятую. "
            "Можно ввести \\n (перевод строки) или \\t (табуляция)."
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.separator_edit.setText(self._display_separator(config.get("separator", ", ")))
        self.separator_edit.textChanged.connect(self._emit_changed)
        self.separator_form.setEnabled(bool(config.get("enabled", True)))
        return group

    def _build_generic_card(self, plugin, config: dict) -> QGroupBox:
        group = QGroupBox(str(plugin.name or plugin.id))
        layout = QVBoxLayout(group)
        layout.setSpacing(6)
        checkbox = QCheckBox("Включён для текущей кампании")
        checkbox.setChecked(bool(config.get("enabled", False)))
        layout.addWidget(checkbox)
        description = QLabel(str(plugin.description or "Без описания."))
        description.setObjectName("mutedLabel")
        description.setWordWrap(True)
        layout.addWidget(description)

        form = self._build_schema_form(plugin, config)
        if form is not None:
            form.setEnabled(checkbox.isChecked())
            layout.addWidget(form)
        checkbox.toggled.connect(
            lambda checked, p=plugin.id, f=form: self._on_generic_toggled(p, checked, f)
        )

        path_label = QLabel(f"Файл: plugins/{self.registry.entry(plugin.id).path.name}")
        path_label.setObjectName("mutedLabel")
        layout.addWidget(path_label)
        self._generic_checkboxes[plugin.id] = checkbox
        return group

    def _rebuild_cards(self) -> None:
        was_loading = self._loading
        self._loading = True
        for widget in self._plugin_cards.values():
            self.cards_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self._plugin_cards.clear()
        self._generic_checkboxes.clear()
        self._schema_widgets.clear()
        self._schema_getters.clear()
        self._field_combos.clear()
        self.split_checkbox = None
        self.form_widget = None
        self.source_combo = None
        self.row_xpath_edit = None
        self.name_xpath_edit = None
        self.value_xpath_edit = None
        self.priority_edit = None
        self.separator_checkbox = None
        self.separator_form = None
        self.separator_edit = None

        self.plugin_combo.blockSignals(True)
        self.plugin_combo.clear()
        for entry in self.registry.all():
            plugin = entry.plugin
            config = self._config_for_entry(plugin.id, plugin)
            if plugin.id == "specs_parser":
                card = self._build_specs_card(plugin, config)
            elif plugin.id == "list_separator":
                card = self._build_list_card(plugin, config)
            else:
                card = self._build_generic_card(plugin, config)
            self._plugin_cards[plugin.id] = card
            self.cards_layout.addWidget(card)
            self.plugin_combo.addItem(
                f"{plugin.name or plugin.id} ({plugin.id})", plugin.id
            )
        if not self.registry.all():
            empty = QLabel(
                "Плагины не найдены. Нажмите «Установить плагин (.py)...» или положите "
                "файлы в папку plugins/ рядом с main.py."
            )
            empty.setObjectName("mutedLabel")
            empty.setWordWrap(True)
            self.cards_layout.addWidget(empty)
            self._plugin_cards["__empty__"] = empty
        self.plugin_combo.blockSignals(False)
        self._update_delete_button()
        self._update_registry_label()
        self._loading = was_loading

    def _fill_source_combo(self, source: str) -> None:
        if self.source_combo is None:
            return
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("— выберите поле —", "")
        seen: set[str] = set()
        for name in self._field_names:
            if not name or name in seen:
                continue
            seen.add(name)
            self.source_combo.addItem(name, name)
        if source and source not in seen:
            self.source_combo.addItem(source, source)
        index = self.source_combo.findData(source)
        self.source_combo.setCurrentIndex(index if index >= 0 else 0)
        self.source_combo.blockSignals(False)

    def _update_registry_label(self) -> None:
        count = len(self.registry.all())
        text = f"Установлено плагинов: {count}"
        if self.registry.errors:
            text += f" · предупреждений: {len(self.registry.errors)}"
        self.registry_label.setText(text)
        self.registry_label.setToolTip("\n".join(self.registry.errors))

    def _update_delete_button(self) -> None:
        self.delete_button.setEnabled(bool(self.plugin_combo.currentData()))

    # ------------------------------------------------------------------
    # Campaign state
    # ------------------------------------------------------------------
    def set_campaign(self, campaign: dict) -> None:
        self._loading = True
        plugins = campaign.get("plugins") if isinstance(campaign.get("plugins"), dict) else {}
        self._configs = {
            str(plugin_id): copy.deepcopy(config)
            for plugin_id, config in plugins.items()
            if isinstance(config, dict)
        }
        self._field_names = [
            str(field.get("name") or "")
            for field in campaign.get("fields", [])
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        ]
        self._rebuild_cards()

        export = campaign.get("export") if isinstance(campaign.get("export"), dict) else {}
        export_format = str(export.get("format") or "xlsx")
        index = self.format_combo.findData(export_format)
        self.format_combo.setCurrentIndex(max(0, index))
        self.path_edit.setText(str(export.get("file_path") or "./exports/Результат.xlsx"))
        self._update_format_ui()
        self._loading = False

    def _refresh_field_combos(self) -> None:
        for combo, allow_empty in self._field_combos:
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            if allow_empty:
                combo.addItem("Все / не выбрано", "")
            for name in self._field_names:
                combo.addItem(name, name)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(current)
            combo.blockSignals(False)

    def refresh_sources(self, names: list[str]) -> None:
        self._field_names = [str(name) for name in names if str(name or "").strip()]
        if self.source_combo is not None:
            current = str(self.source_combo.currentData() or "")
            self._fill_source_combo(current)
        self._refresh_field_combos()

    def plugins(self) -> dict:
        self._store_ui_configs()
        return copy.deepcopy(self._configs)

    def _store_ui_configs(self) -> None:
        if self.split_checkbox is not None:
            priority_text = self.priority_edit.text().strip() if self.priority_edit else ""
            if priority_text == self._priority_loaded_text:
                priority_columns = [list(group) for group in self._priority_groups]
            else:
                priority_columns = parse_priority_text(priority_text)
            specs = dict(self._configs.get("specs_parser") or {})
            specs.update(
                {
                    "enabled": self.split_checkbox.isChecked(),
                    "source_field": str(self.source_combo.currentData() or "")
                    if self.source_combo
                    else "",
                    "row_xpath": self.row_xpath_edit.text().strip()
                    if self.row_xpath_edit
                    else "",
                    "name_xpath": self.name_xpath_edit.text().strip()
                    if self.name_xpath_edit
                    else "",
                    "value_xpath": self.value_xpath_edit.text().strip()
                    if self.value_xpath_edit
                    else "",
                    "priority_columns": priority_columns,
                }
            )
            specs["split_specs_to_columns"] = specs["enabled"]
            self._configs["specs_parser"] = specs

        if self.separator_checkbox is not None:
            separator = dict(self._configs.get("list_separator") or {})
            separator.update(
                {
                    "enabled": self.separator_checkbox.isChecked(),
                    "separator": self.separator_edit.text() if self.separator_edit else ", ",
                }
            )
            self._configs["list_separator"] = separator

        for plugin_id, checkbox in self._generic_checkboxes.items():
            config = dict(self._configs.get(plugin_id) or {})
            config["enabled"] = checkbox.isChecked()
            self._configs[plugin_id] = config

        for plugin_id, getters in self._schema_getters.items():
            config = dict(self._configs.get(plugin_id) or {})
            for key, getter in getters:
                try:
                    config[key] = getter()
                except Exception:  # noqa: BLE001 - keep other settings usable
                    continue
            self._configs[plugin_id] = config

    def apply_specs_block(
        self,
        source_field: str,
        row_xpath: str,
        name_xpath: str,
        value_xpath: str,
        field_names: list[str] | None = None,
    ) -> None:
        """Enable the split-specifications plugin with a picked/detected block.

        Works even when ``plugins/specs_parser.py`` is deleted: the settings are
        stored in the campaign and take effect as soon as the file is back.
        """

        self._loading = True
        if field_names is not None:
            self._field_names = [
                str(name) for name in field_names if str(name or "").strip()
            ]
        if self.source_combo is not None:
            self._fill_source_combo(source_field)
            if self.split_checkbox is not None:
                self.split_checkbox.setChecked(True)
            if self.row_xpath_edit is not None:
                self.row_xpath_edit.setText(row_xpath)
            if self.name_xpath_edit is not None:
                self.name_xpath_edit.setText(name_xpath)
            if self.value_xpath_edit is not None:
                self.value_xpath_edit.setText(value_xpath)
            if self.form_widget is not None:
                self.form_widget.setEnabled(True)

        specs = dict(self._configs.get("specs_parser") or {})
        specs.update(
            {
                "enabled": True,
                "source_field": source_field,
                "row_xpath": row_xpath,
                "name_xpath": name_xpath,
                "value_xpath": value_xpath,
                "split_specs_to_columns": True,
            }
        )
        self._configs["specs_parser"] = specs
        self._loading = False
        self.changed.emit()

    def export(self) -> dict:
        return {
            "format": str(self.format_combo.currentData() or "xlsx"),
            "file_path": self.path_edit.text(),
        }

    # ------------------------------------------------------------------
    # Plugin management
    # ------------------------------------------------------------------
    def _open_plugins_folder(self) -> None:
        folder = plugins_dir(self.project_root)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Папка плагинов",
                f"Не удалось создать папку:\n{folder}\n\n{exc}",
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(
                self,
                "Папка плагинов",
                f"Не удалось открыть папку в файловом менеджере:\n{folder}",
            )

    def _refresh_plugins(self) -> None:
        self._store_ui_configs()
        errors = self.registry.refresh()
        self._rebuild_cards()
        if errors:
            QMessageBox.warning(
                self,
                "Обновление плагинов",
                "Некоторые плагины не загрузились:\n\n" + "\n".join(errors),
            )

    def _install_plugin(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Установить плагин",
            str(self.project_root),
            "Python-плагин (*.py)",
        )
        if not filename:
            return
        source = Path(filename)
        if source.suffix.lower() != ".py":
            QMessageBox.warning(self, "Установка плагина", "Нужен файл с расширением .py.")
            return
        target = plugins_dir(self.project_root) / source.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Установка плагина", f"Не удалось создать папку plugins:\n{exc}")
            return
        if target.exists() and target.resolve() != source.resolve():
            answer = QMessageBox.question(
                self,
                "Установка плагина",
                f"Файл plugins/{target.name} уже существует. Заменить его?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._store_ui_configs()
        try:
            if target.resolve() != source.resolve():
                shutil.copy2(source, target)
        except OSError as exc:
            QMessageBox.critical(self, "Установка плагина", f"Не удалось скопировать файл:\n{exc}")
            return
        errors = self.registry.refresh()
        self._rebuild_cards()
        self.changed.emit()
        if errors:
            QMessageBox.warning(
                self,
                "Установка плагина",
                "Файл скопирован, но плагин не загрузился:\n\n" + "\n".join(errors),
            )

    def _delete_plugin(self) -> None:
        plugin_id = str(self.plugin_combo.currentData() or "")
        entry = self.registry.entry(plugin_id) if plugin_id else None
        if entry is None:
            return
        answer = QMessageBox.question(
            self,
            "Удаление плагина",
            f"Удалить плагин «{entry.plugin.name or plugin_id}»?\n\n"
            f"Файл plugins/{entry.path.name} будет удалён. Настройки кампании сохранятся, "
            "а плагин будет пропускаться, пока файл не вернут.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store_ui_configs()
        try:
            entry.path.unlink()
        except OSError as exc:
            QMessageBox.critical(self, "Удаление плагина", f"Не удалось удалить файл:\n{exc}")
            return
        self.registry.refresh()
        self._rebuild_cards()
        self.changed.emit()

    # ------------------------------------------------------------------
    @staticmethod
    def _display_separator(value) -> str:
        text = str(value if value is not None else ", ")
        return text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")

    def _on_split_toggled(self, checked: bool) -> None:
        if self.form_widget is not None:
            self.form_widget.setEnabled(checked)
        self._emit_changed()

    def _on_separator_toggled(self, checked: bool) -> None:
        if self.separator_form is not None:
            self.separator_form.setEnabled(checked)
        self._emit_changed()

    def _on_generic_toggled(
        self,
        plugin_id: str,
        checked: bool,
        form: QWidget | None = None,
    ) -> None:
        if form is not None:
            form.setEnabled(checked)
        config = self._configs.setdefault(plugin_id, {})
        config["enabled"] = checked
        self._emit_changed()

    def _on_format_changed(self) -> None:
        if self._loading:
            return
        export_format = str(self.format_combo.currentData() or "xlsx")
        current = self.path_edit.text()
        if current:
            from pathlib import Path

            path = Path(current)
            if path.suffix.lower() in {".xlsx", ".csv"} and path.suffix.lower() != f".{export_format}":
                self.path_edit.setText(str(path.with_suffix(f".{export_format}")))
        self._update_format_ui()
        self._emit_changed()

    def _update_format_ui(self) -> None:
        export_format = str(self.format_combo.currentData() or "xlsx")
        if export_format == "xlsx":
            self.path_edit.file_filter = "Excel (*.xlsx);;Все файлы (*)"
            self.path_edit.default_suffix = "xlsx"
            self.format_hint.setText("Книга Excel с заголовками, фильтром и переносом текста.")
        else:
            self.path_edit.file_filter = "CSV (*.csv);;Все файлы (*)"
            self.path_edit.default_suffix = "csv"
            self.format_hint.setText("Текст UTF-8 с BOM, разделитель «;» — открывается в Excel.")

    def _emit_changed(self) -> None:
        if not self._loading:
            self.changed.emit()
