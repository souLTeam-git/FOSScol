"""Main application window."""

from __future__ import annotations

import copy
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QSize, Qt, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.engine import CrawlSummary
from core.exporter import ExportError, export_results
from core.runner import CampaignRunner
from core.schema import (
    CampaignValidationError,
    new_campaign,
    normalize_campaign,
    validate_campaign,
)
from core.storage import CampaignRecord, CampaignStorage, StorageError
from ui.dialogs import CampaignDialog
from ui.icons import app_icon, logo_pixmap
from ui.inspector import AutoDetectDialog, AutoDetectWorker, TestRunDialog
from ui.tabs.fields_tab import FieldsTab
from ui.tabs.input_tab import InputTab
from ui.tabs.navigation_tab import NavigationTab
from ui.tabs.plugins_tab import PluginsTab
from ui.tabs.results_tab import ResultsTab
from ui.widgets import LogPanel
from ui.xpath_picker import XPathPickerDialog
ROLE_PATH = int(Qt.ItemDataRole.UserRole)
ROLE_CATEGORY = ROLE_PATH + 1
ROLE_NAME = ROLE_PATH + 2


class MainWindow(QMainWindow):
    def __init__(self, project_root: str | Path) -> None:
        super().__init__()
        self.project_root = Path(project_root).expanduser().resolve()
        self.storage = CampaignStorage(self.project_root / "campaigns")
        self.campaign: dict[str, Any] | None = None
        self.current_path: Path | None = None
        self._dirty = False
        self._loading = False
        self._tree_blocked = False
        self._records: list[CampaignRecord] = []
        self._runner: CampaignRunner | None = None
        self._auto_worker: AutoDetectWorker | None = None
        self._running = False
        self._paused = False
        self._pause_reason = ""
        self._run_started = 0.0
        self._last_summary: CrawlSummary | None = None
        self.settings = QSettings("FOSScol", "FOSScol")

        self.setWindowTitle("FOSScol")
        self.setMinimumSize(1100, 720)
        self.resize(1480, 930)
        self.setWindowIcon(app_icon())

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self.settings.value("windowState")
        if state is not None:
            self.restoreState(state)

        self.log_panel.append("system", "FOSScol — кроссплатформенный парсер на PySide6 + Playwright.")
        records = self._refresh_tree()
        if records:
            preferred = next((item for item in records if item.stem == "hotline-new"), records[0])
            self._load_campaign(preferred.path)
        else:
            self._show_empty()
            self.log_panel.append(
                "info", "Кампаний пока нет. Нажмите «Новая кампания», чтобы создать первую."
            )
        if self.storage.last_errors:
            for path, message in self.storage.last_errors:
                self.log_panel.append("warning", f"Пропущен файл {path.name}: {message}")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_actions(self) -> None:
        style = self.style()
        self.action_new = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "Новая кампания", self
        )
        self.action_new.setShortcut(QKeySequence.StandardKey.New)
        self.action_new.setToolTip("Создать новую кампанию (Ctrl+N)")
        self.action_new.triggered.connect(lambda _checked=False: self.new_campaign())

        self.action_save = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "Сохранить", self
        )
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_save.setToolTip("Сохранить кампанию в JSON (Ctrl+S)")
        self.action_save.triggered.connect(lambda: self.save_campaign())

        self.action_run = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "Запустить", self
        )
        self.action_run.setShortcut(QKeySequence("F5"))
        self.action_run.setToolTip("Запустить парсинг (F5)")
        self.action_run.triggered.connect(self.run_campaign)

        self.action_stop = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop), "Остановить", self
        )
        self.action_stop.setShortcut(QKeySequence("Shift+F5"))
        self.action_stop.setToolTip("Остановить парсинг (Shift+F5)")
        self.action_stop.setEnabled(False)
        self.action_stop.triggered.connect(self.stop_campaign)

        self.action_pause = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPause), "Пауза", self
        )
        self.action_pause.setShortcut(QKeySequence("F6"))
        self.action_pause.setToolTip("Поставить парсинг на паузу (F6)")
        self.action_pause.setEnabled(False)
        self.action_pause.triggered.connect(self.toggle_pause)

        self.action_export = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "Экспорт", self
        )
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_export.setToolTip("Экспортировать результаты (Ctrl+E)")
        self.action_export.triggered.connect(self.export_current_results)

        self.action_refresh = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Обновить список", self
        )
        self.action_refresh.setShortcut(QKeySequence("Ctrl+R"))
        self.action_refresh.triggered.connect(lambda: self._refresh_tree(select_current=True))

        self.action_open_folder = QAction("Открыть папку кампаний", self)
        self.action_open_folder.triggered.connect(
            lambda: self._open_folder(self.storage.directory)
        )

        self.action_about = QAction("О программе", self)
        self.action_about.triggered.connect(self._show_about)

    def _build_menu(self) -> None:
        menu_file = self.menuBar().addMenu("Файл")
        menu_file.addAction(self.action_new)
        menu_file.addAction(self.action_save)
        menu_file.addSeparator()
        menu_file.addAction(self.action_export)
        menu_file.addSeparator()
        menu_file.addAction(self.action_open_folder)
        menu_file.addSeparator()
        exit_action = menu_file.addAction("Выход")
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)

        menu_campaign = self.menuBar().addMenu("Кампания")
        menu_campaign.addAction(self.action_run)
        menu_campaign.addAction(self.action_pause)
        menu_campaign.addAction(self.action_stop)
        menu_campaign.addSeparator()
        menu_campaign.addAction(self.action_refresh)

        menu_help = self.menuBar().addMenu("Справка")
        menu_help.addAction(self.action_about)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Основные действия", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(17, 17))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        toolbar.addAction(self.action_new)
        toolbar.addAction(self.action_save)
        toolbar.addSeparator()
        toolbar.addAction(self.action_run)
        toolbar.addAction(self.action_pause)
        toolbar.addAction(self.action_stop)
        toolbar.addSeparator()
        toolbar.addAction(self.action_export)

        for action in (
            self.action_new,
            self.action_save,
            self.action_run,
            self.action_pause,
            self.action_stop,
            self.action_export,
        ):
            button = toolbar.widgetForAction(action)
            if isinstance(button, QToolButton):
                if action is self.action_run:
                    button.setObjectName("runAction")
                elif action is self.action_pause:
                    button.setObjectName("pauseAction")
                elif action is self.action_stop:
                    button.setObjectName("stopAction")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toolbar = toolbar

    def _build_central(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(9, 9, 9, 7)
        root.setSpacing(8)

        left = QWidget()
        left.setObjectName("leftPanel")
        left.setFixedWidth(292)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 10, 9)
        left_layout.setSpacing(7)

        header = QHBoxLayout()
        header.setSpacing(3)
        title = QLabel("Кампании")
        title.setObjectName("panelTitle")
        header.addWidget(title)
        header.addStretch(1)
        new_button = QToolButton()
        new_button.setObjectName("smallButton")
        new_button.setText("＋")
        new_button.setToolTip("Новая кампания")
        new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        new_button.clicked.connect(lambda _checked=False: self.new_campaign())
        header.addWidget(new_button)
        refresh_button = QToolButton()
        refresh_button.setObjectName("smallButton")
        refresh_button.setText("⟳")
        refresh_button.setToolTip("Обновить список кампаний")
        refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_button.clicked.connect(lambda: self._refresh_tree(select_current=True))
        header.addWidget(refresh_button)
        left_layout.addLayout(header)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("Поиск кампаний…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        left_layout.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setIconSize(QSize(15, 15))
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)
        self.tree.currentItemChanged.connect(self._on_tree_current_changed)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        left_layout.addWidget(self.tree, 1)

        self.tree_count_label = QLabel("0 кампаний")
        self.tree_count_label.setObjectName("mutedLabel")
        left_layout.addWidget(self.tree_count_label)

        root.addWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        right.setChildrenCollapsible(False)
        right.setObjectName("rightPanel")
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.input_tab = InputTab()
        self.fields_tab = FieldsTab()
        self.navigation_tab = NavigationTab()
        self.plugins_tab = PluginsTab(str(self.project_root))
        self.results_tab = ResultsTab()
        self.results_tab.count_changed.connect(self._on_results_count_changed)
        self.tabs.addTab(self.input_tab, "Входные данные")
        self.tabs.addTab(self.fields_tab, "Поля данных")
        self.tabs.addTab(self.navigation_tab, "Навигация и Сценарий")
        self.tabs.addTab(self.plugins_tab, "Плагины и Экспорт")
        self.tabs.addTab(self.results_tab, "Результаты")
        right.addWidget(self.tabs)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(2, 2, 2, 0)
        bottom_layout.setSpacing(4)
        self.log_panel = LogPanel()
        bottom_layout.addWidget(self.log_panel, 1)
        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        progress_label = QLabel("Прогресс")
        progress_label.setObjectName("mutedLabel")
        progress_row.addWidget(progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0 / 0")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setMinimumWidth(320)
        progress_row.addWidget(self.progress_bar, 1)
        self.progress_detail = QLabel("Ожидание запуска")
        self.progress_detail.setObjectName("mutedLabel")
        self.progress_detail.setMinimumWidth(190)
        self.progress_detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self.progress_detail)
        bottom_layout.addLayout(progress_row)
        right.addWidget(bottom)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        right.setSizes([660, 245])
        root.addWidget(right, 1)

        self.input_tab.changed.connect(self._mark_dirty)
        self.fields_tab.changed.connect(self._on_fields_changed)
        self.fields_tab.picker_requested.connect(self._open_picker)
        self.fields_tab.page_picker_requested.connect(self._open_page_picker)
        self.fields_tab.dynamic_picker_requested.connect(self._open_dynamic_picker)
        self.fields_tab.auto_detect_requested.connect(self._auto_detect_fields)
        self.fields_tab.test_run_requested.connect(self._test_fields)
        self.navigation_tab.changed.connect(self._mark_dirty)
        self.plugins_tab.changed.connect(self._mark_dirty)

    def _build_statusbar(self) -> None:
        self.status_label = QLabel("Готово")
        self.status_label.setObjectName("statusStrong")
        self.path_label = QLabel("")
        self.path_label.setObjectName("mutedLabel")
        self.statusBar().addWidget(self.status_label)
        self.statusBar().addPermanentWidget(self.path_label)

    # ------------------------------------------------------------------
    # Tree and campaigns
    # ------------------------------------------------------------------
    def _category_label(self, category: str) -> str:
        return category if category.strip() else "Без категории"

    def _refresh_tree(self, select_current: bool = False) -> list[CampaignRecord]:
        records = self.storage.list_campaigns()
        self._records = records
        groups: dict[str, list[CampaignRecord]] = {}
        for record in records:
            groups.setdefault(record.category, []).append(record)
        categories = sorted(groups, key=lambda value: (not bool(value.strip()), value.casefold()))

        self._tree_blocked = True
        self.tree.clear()
        for category in categories:
            folder = QTreeWidgetItem([self._category_label(category)])
            folder.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
            folder.setData(0, ROLE_CATEGORY, category)
            font = folder.font(0)
            font.setBold(True)
            folder.setFont(0, font)
            folder.setForeground(0, QBrush(QColor("#5d6572")))
            folder.setToolTip(0, f"Категория: {self._category_label(category)}")
            for record in sorted(groups[category], key=lambda item: item.name.casefold()):
                item = QTreeWidgetItem([record.name])
                item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
                item.setData(0, ROLE_PATH, str(record.path))
                item.setData(0, ROLE_CATEGORY, record.category)
                item.setData(0, ROLE_NAME, record.name)
                modified = datetime.fromtimestamp(record.modified).strftime("%d.%m.%Y %H:%M")
                item.setToolTip(0, f"{record.path}\nИзменён: {modified}")
                folder.addChild(item)
            self.tree.addTopLevelItem(folder)
        self.tree.expandAll()
        self._tree_blocked = False

        count = len(records)
        self.tree_count_label.setText(
            f"{count} {self._plural(count, 'кампания', 'кампании', 'кампаний')}"
        )
        self._apply_filter(self.search_edit.text())

        target = self.current_path if select_current or self.current_path else None
        if target is not None:
            self._select_tree_path(target)
        return records

    @staticmethod
    def _plural(number: int, one: str, few: str, many: str) -> str:
        if number % 10 == 1 and number % 100 != 11:
            return one
        if number % 10 in {2, 3, 4} and number % 100 not in {12, 13, 14}:
            return few
        return many

    def _select_tree_path(self, path: str | Path | None) -> None:
        if path is None:
            return
        target = str(Path(path).resolve())
        self._tree_blocked = True
        for index in range(self.tree.topLevelItemCount()):
            folder = self.tree.topLevelItem(index)
            for child_index in range(folder.childCount()):
                child = folder.child(child_index)
                if child.data(0, ROLE_PATH) == target:
                    self.tree.setCurrentItem(child)
                    self.tree.scrollToItem(child)
                    self.tree.expandItem(folder)
                    self._tree_blocked = False
                    return
        self._tree_blocked = False

    def _find_tree_item(self, path: str | Path) -> QTreeWidgetItem | None:
        target = str(Path(path).resolve())
        for index in range(self.tree.topLevelItemCount()):
            folder = self.tree.topLevelItem(index)
            for child_index in range(folder.childCount()):
                child = folder.child(child_index)
                if child.data(0, ROLE_PATH) == target:
                    return child
        return None

    def _apply_filter(self, text: str) -> None:
        query = text.strip().casefold()
        for index in range(self.tree.topLevelItemCount()):
            folder = self.tree.topLevelItem(index)
            visible_children = 0
            for child_index in range(folder.childCount()):
                child = folder.child(child_index)
                haystack = f"{child.text(0)} {child.data(0, ROLE_CATEGORY) or ''}".casefold()
                match = not query or query in haystack
                child.setHidden(not match)
                if match:
                    visible_children += 1
            folder.setHidden(visible_children == 0)
            if query and visible_children:
                folder.setExpanded(True)

    def _on_tree_current_changed(
        self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None
    ) -> None:
        if self._tree_blocked or current is None:
            return
        path_value = current.data(0, ROLE_PATH)
        if not path_value:
            return
        path = Path(str(path_value))
        if self.current_path is not None and path.resolve() == self.current_path:
            return
        if self._running:
            QMessageBox.information(
                self, "Парсинг выполняется", "Остановите парсинг перед переключением кампании."
            )
            self._select_tree_path(self.current_path)
            return
        if not self._confirm_leave():
            self._select_tree_path(self.current_path)
            return
        self._load_campaign(path)

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not item.data(0, ROLE_PATH):
            item.setExpanded(not item.isExpanded())

    def _show_tree_menu(self, position: Any) -> None:
        item = self.tree.itemAt(position)
        menu = QMenu(self)
        if item is None:
            action = menu.addAction("Новая кампания…")
            action.triggered.connect(lambda _checked=False: self.new_campaign())
            menu.addAction(self.action_refresh)
        elif not item.data(0, ROLE_PATH):
            category = str(item.data(0, ROLE_CATEGORY) or "")
            action = menu.addAction("Новая кампания в этой папке…")
            action.triggered.connect(
                lambda _checked=False, folder=category: self.new_campaign(folder)
            )
            rename = menu.addAction("Переименовать папку…")
            rename.triggered.connect(lambda: self._rename_category(category))
            menu.addSeparator()
            menu.addAction(self.action_refresh)
        else:
            path = Path(str(item.data(0, ROLE_PATH)))
            open_action = menu.addAction("Открыть")
            open_action.triggered.connect(lambda: self._open_campaign_from_tree(item))
            menu.addSeparator()
            duplicate = menu.addAction("Дублировать…")
            duplicate.triggered.connect(lambda: self.duplicate_campaign(path))
            rename = menu.addAction("Переименовать…")
            rename.triggered.connect(lambda: self.rename_campaign(path))
            delete = menu.addAction("Удалить…")
            delete.triggered.connect(lambda: self.delete_campaign(path))
            menu.addSeparator()
            show_file = menu.addAction("Показать папку с файлом")
            show_file.triggered.connect(lambda: self._open_folder(path.parent))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _open_campaign_from_tree(self, item: QTreeWidgetItem) -> None:
        path_value = item.data(0, ROLE_PATH)
        if not path_value:
            return
        path = Path(str(path_value))
        if self.current_path is not None and path.resolve() == self.current_path:
            return
        if self._running:
            QMessageBox.information(self, "Парсинг выполняется", "Остановите парсинг перед переключением.")
            return
        if not self._confirm_leave():
            return
        self._load_campaign(path)

    # ------------------------------------------------------------------
    # Loading and saving
    # ------------------------------------------------------------------
    def _load_campaign(self, path: str | Path) -> bool:
        path = Path(path).resolve()
        try:
            campaign = self.storage.load(path)
        except (StorageError, CampaignValidationError) as exc:
            QMessageBox.critical(self, "Загрузка кампании", str(exc))
            self.log_panel.append("error", f"Не удалось загрузить {path.name}: {exc}")
            return False

        self._loading = True
        self.campaign = campaign
        self.current_path = path
        self.tabs.setEnabled(True)
        self.input_tab.set_campaign(campaign)
        self.fields_tab.set_campaign(campaign)
        self.navigation_tab.set_campaign(campaign)
        self.plugins_tab.set_campaign(campaign)
        field_names = [str(field.get("name") or "") for field in campaign.get("fields", [])]
        self.results_tab.set_column_priority(self._priority_columns(campaign))
        self.results_tab.prepare([name for name in field_names if name])
        self.results_tab.set_running(False)
        self._loading = False
        self._dirty = False
        self._last_summary = None
        self._reset_progress()
        self._select_tree_path(path)
        self._update_title()
        self._update_actions()
        self.path_label.setText(str(path))
        self.status_label.setText("Кампания загружена")
        self.log_panel.append(
            "info",
            f"Загружена кампания «{campaign.get('campaign_name')}» "
            f"({len(campaign.get('fields', []))} полей, "
            f"{len((campaign.get('input') or {}).get('start_urls', []))} URL).",
        )
        return True

    @staticmethod
    def _priority_columns(campaign: dict[str, Any]) -> list:
        """Return configured priority spec columns from nested or legacy JSON."""

        plugins = campaign.get("plugins") if isinstance(campaign.get("plugins"), dict) else {}
        specs = plugins.get("specs_parser") if isinstance(plugins.get("specs_parser"), dict) else {}
        priority = specs.get("priority_columns")
        if not isinstance(priority, list):
            priority = plugins.get("priority_columns")
        return priority if isinstance(priority, list) else []

    def _compose_campaign(self) -> dict[str, Any]:
        if self.campaign is None:
            raise StorageError("Кампания не выбрана.")
        campaign = copy.deepcopy(self.campaign)
        input_section = campaign.get("input") if isinstance(campaign.get("input"), dict) else {}
        input_section = copy.deepcopy(input_section)
        input_section["start_urls"] = self.input_tab.start_urls()
        campaign["input"] = input_section
        campaign["fields"] = self.fields_tab.fields()
        campaign["navigation"] = self.navigation_tab.navigation()
        engine = campaign.get("engine") if isinstance(campaign.get("engine"), dict) else {}
        engine = copy.deepcopy(engine)
        engine.update(self.navigation_tab.engine_settings())
        campaign["engine"] = engine
        campaign["scenario"] = self.navigation_tab.scenario()
        campaign["plugins"] = self.plugins_tab.plugins()
        campaign["export"] = self.plugins_tab.export()
        return campaign

    def save_campaign(self, silent: bool = False) -> bool:
        if self.campaign is None or self.current_path is None:
            return False
        try:
            payload = self._compose_campaign()
            saved_path = self.storage.save(payload, previous_path=self.current_path)
        except CampaignValidationError as exc:
            self._report_validation_errors(exc.errors)
            return False
        except (StorageError, OSError) as exc:
            QMessageBox.critical(self, "Сохранение кампании", str(exc))
            self.log_panel.append("error", f"Сохранение не выполнено: {exc}")
            return False

        self.campaign = normalize_campaign(payload)
        self.current_path = saved_path.resolve()
        self._dirty = False
        self._refresh_tree(select_current=True)
        self._select_tree_path(self.current_path)
        self._update_title()
        self._update_actions()
        self.path_label.setText(str(self.current_path))
        self.status_label.setText("Сохранено")
        if not silent:
            self.log_panel.append("success", f"Кампания сохранена: {self.current_path}")
        return True

    def _update_title(self) -> None:
        if self.campaign is None:
            self.setWindowTitle("FOSScol")
            return
        marker = " *" if self._dirty else ""
        self.setWindowTitle(f"FOSScol — {self.campaign.get('campaign_name', '')}{marker}")

    def _mark_dirty(self) -> None:
        if self._loading or self.campaign is None:
            return
        if not self._dirty:
            self._dirty = True
            self._update_title()
            self.status_label.setText("Есть несохранённые изменения")

    def _on_fields_changed(self) -> None:
        self._mark_dirty()
        self.plugins_tab.refresh_sources(self.fields_tab.field_names())

    def _confirm_leave(self) -> bool:
        if not self._dirty or self.campaign is None:
            return True
        answer = QMessageBox.question(
            self,
            "Несохранённые изменения",
            "Сохранить изменения в текущей кампании?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_campaign(silent=True)
        if answer == QMessageBox.StandardButton.Discard:
            return True
        return False

    # ------------------------------------------------------------------
    # Campaign operations
    # ------------------------------------------------------------------
    def new_campaign(self, default_category: str = "") -> None:
        # QAction.triggered / QPushButton.clicked pass a bool flag.  Never let
        # it leak into the category value of the dialog.
        if not isinstance(default_category, str):
            default_category = ""
        if self._running:
            QMessageBox.information(self, "Парсинг выполняется", "Остановите парсинг перед созданием кампании.")
            return
        if not self._confirm_leave():
            return
        categories = sorted({record.category for record in self._records if record.category})
        dialog = CampaignDialog(
            self,
            categories=categories,
            category=default_category,
            title="Новая кампания",
            ok_text="Создать",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            campaign, path = self.storage.create(dialog.campaign_name, dialog.category)
        except CampaignValidationError as exc:
            self._report_validation_errors(exc.errors)
            return
        except (StorageError, OSError) as exc:
            QMessageBox.critical(self, "Новая кампания", str(exc))
            self.log_panel.append("error", f"Не удалось создать кампанию: {exc}")
            return
        self.campaign = campaign
        self.current_path = path.resolve()
        self._refresh_tree()
        self._load_campaign(path)
        self.log_panel.append("success", f"Создана кампания «{campaign.get('campaign_name')}».")

    def rename_campaign(self, path: str | Path | None = None) -> None:
        if self._running:
            QMessageBox.information(self, "Парсинг выполняется", "Остановите парсинг перед переименованием.")
            return
        target = Path(path).resolve() if path else self.current_path
        if target is None:
            return
        is_current = self.current_path is not None and target == self.current_path
        if is_current and self._dirty:
            if not self.save_campaign(silent=True):
                return
            target = self.current_path or target
            is_current = True
        try:
            campaign = self._compose_campaign() if is_current else self.storage.load(target)
        except (StorageError, CampaignValidationError) as exc:
            QMessageBox.critical(self, "Переименование", str(exc))
            return
        categories = sorted({record.category for record in self._records if record.category})
        dialog = CampaignDialog(
            self,
            categories=categories,
            name=str(campaign.get("campaign_name") or target.stem),
            category=str(campaign.get("category") or ""),
            title="Переименование кампании",
            ok_text="Сохранить",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        campaign["campaign_name"] = dialog.campaign_name
        campaign["category"] = dialog.category
        try:
            new_path = self.storage.save(campaign, previous_path=target)
        except CampaignValidationError as exc:
            self._report_validation_errors(exc.errors)
            return
        except (StorageError, OSError) as exc:
            QMessageBox.critical(self, "Переименование", str(exc))
            return
        if is_current:
            self.campaign = normalize_campaign(campaign)
            self.current_path = new_path.resolve()
            self._dirty = False
            self.path_label.setText(str(self.current_path))
            self._update_title()
        self._refresh_tree(select_current=True)
        self._select_tree_path(self.current_path)
        self.log_panel.append("success", f"Кампания переименована: {new_path.name}")
        self.status_label.setText("Переименовано")

    def duplicate_campaign(self, path: str | Path | None = None) -> None:
        if self._running:
            QMessageBox.information(self, "Парсинг выполняется", "Остановите парсинг перед дублированием.")
            return
        target = Path(path).resolve() if path else self.current_path
        if target is None:
            return
        is_current = self.current_path is not None and target == self.current_path
        if is_current and self._dirty:
            if not self.save_campaign(silent=True):
                return
            target = self.current_path or target
        try:
            campaign = self.storage.load(target)
        except (StorageError, CampaignValidationError) as exc:
            QMessageBox.critical(self, "Дублирование", str(exc))
            return
        default_name = f"{campaign.get('campaign_name', target.stem)}-copy"
        name, accepted = QInputDialog.getText(
            self, "Дублировать кампанию", "Имя новой кампании:", text=default_name
        )
        if not accepted:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Дублирование", "Введите имя кампании.")
            return
        try:
            new_path = self.storage.duplicate(target, name)
        except CampaignValidationError as exc:
            self._report_validation_errors(exc.errors)
            return
        except (StorageError, OSError) as exc:
            QMessageBox.critical(self, "Дублирование", str(exc))
            return
        self._refresh_tree(select_current=True)
        self.log_panel.append("success", f"Создана копия кампании: {new_path.name}")
        self.status_label.setText("Кампания продублирована")

    def delete_campaign(self, path: str | Path | None = None) -> None:
        if self._running:
            QMessageBox.information(self, "Парсинг выполняется", "Остановите парсинг перед удалением.")
            return
        target = Path(path).resolve() if path else self.current_path
        if target is None:
            return
        name = target.stem
        try:
            campaign = self.storage.load(target)
            name = str(campaign.get("campaign_name") or name)
        except (StorageError, CampaignValidationError):
            pass
        was_current = self.current_path is not None and target == self.current_path
        detail = "\n\nНесохранённые изменения будут потеряны." if was_current and self._dirty else ""
        answer = QMessageBox.warning(
            self,
            "Удаление кампании",
            f"Удалить кампанию «{name}»?\nФайл: {target}{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.storage.delete(target)
        except (StorageError, OSError) as exc:
            QMessageBox.critical(self, "Удаление", str(exc))
            return
        self.log_panel.append("warning", f"Кампания удалена: {target.name}")
        self._dirty = False
        self.current_path = None if was_current else self.current_path
        records = self._refresh_tree(select_current=not was_current)
        if was_current:
            self.campaign = None
            if records:
                self._load_campaign(records[0].path)
            else:
                self._show_empty()
        else:
            self._select_tree_path(self.current_path)
            self._update_actions()
        self.status_label.setText("Кампания удалена")

    def _rename_category(self, category: str) -> None:
        if self._running:
            QMessageBox.information(self, "Парсинг выполняется", "Остановите парсинг перед изменением папки.")
            return
        records = [record for record in self._records if record.category == category]
        if not records:
            return
        new_category, accepted = QInputDialog.getText(
            self,
            "Переименовать папку",
            "Новая категория (пустое значение — «Без категории»):",
            text=category,
        )
        if not accepted:
            return
        new_category = new_category.strip()
        if new_category == category:
            return
        if self.current_path is not None and any(record.path == self.current_path for record in records):
            if self._dirty and not self.save_campaign(silent=True):
                return
        errors = self.storage.update_categories(records, new_category)
        for message in errors:
            self.log_panel.append("error", f"Категория: {message}")
        if not errors:
            self.log_panel.append(
                "success",
                f"Категория «{self._category_label(category)}» переименована в "
                f"«{self._category_label(new_category)}» ({len(records)} кампаний).",
            )
        self._refresh_tree(select_current=True)
        if self.current_path is not None:
            try:
                current = self.storage.load(self.current_path)
                self.campaign = current
                self.plugins_tab.set_campaign(current)
                self._dirty = False
            except (StorageError, CampaignValidationError):
                pass
        self._update_title()
        self.status_label.setText("Категория обновлена")

    def _show_empty(self) -> None:
        self._loading = True
        self.campaign = None
        self.current_path = None
        self._dirty = False
        empty = new_campaign("Новая кампания")
        self.input_tab.set_campaign(empty)
        self.fields_tab.set_campaign(empty)
        self.navigation_tab.set_campaign(empty)
        self.plugins_tab.set_campaign(empty)
        self.results_tab.set_column_priority([])
        self.results_tab.prepare([])
        self.results_tab.set_running(False)
        self._loading = False
        self.tabs.setEnabled(False)
        self._reset_progress()
        self._update_title()
        self._update_actions()
        self.path_label.setText("")
        self.status_label.setText("Кампания не выбрана")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def run_campaign(self) -> None:
        if self.campaign is None:
            QMessageBox.information(self, "Запуск", "Сначала создайте или выберите кампанию.")
            return
        if self._running:
            return
        if not self.save_campaign(silent=True):
            return
        try:
            payload = normalize_campaign(self._compose_campaign())
        except Exception as exc:
            QMessageBox.critical(self, "Запуск", f"Не удалось собрать конфигурацию:\n{exc}")
            return
        errors = validate_campaign(payload, for_run=True)
        if errors:
            self._report_validation_errors(errors)
            self._focus_tab_for_errors(errors)
            return

        field_names = [str(field.get("name") or "") for field in payload.get("fields", [])]
        self.results_tab.set_column_priority(self._priority_columns(payload))
        self.results_tab.prepare([name for name in field_names if name])
        self.results_tab.clear_silently()
        self.results_tab.set_running(True)
        self._last_summary = None
        self._run_started = time.monotonic()
        self._reset_progress()
        self.status_label.setText("Запуск…")
        self.progress_detail.setText("Подготовка Chromium…")
        self.log_panel.append(
            "system",
            f"━━━ Запуск кампании «{payload.get('campaign_name')}» ━━━",
        )

        runner = CampaignRunner(payload, self)
        runner.result_ready.connect(self._on_result)
        runner.log_message.connect(self._on_runner_log)
        runner.progress_changed.connect(self._on_progress)
        runner.pause_changed.connect(self._on_pause_changed)
        runner.run_finished.connect(self._on_run_finished)
        runner.run_failed.connect(self._on_run_failed)
        runner.finished.connect(self._on_runner_thread_finished)
        self._runner = runner
        self._running = True
        self._paused = False
        self._pause_reason = ""
        self._sync_pause_action()
        self._update_actions()
        self.tabs.setCurrentWidget(self.results_tab)
        runner.start()

    def stop_campaign(self) -> None:
        if self._runner is None or not self._running:
            return
        self.log_panel.append("warning", "Останавливаю парсинг…")
        self.status_label.setText("Останавливаю…")
        self.progress_detail.setText("Завершение текущих страниц…")
        self.action_stop.setEnabled(False)
        self.action_pause.setEnabled(False)
        self._runner.stop()

    def toggle_pause(self) -> None:
        if self._runner is None or not self._running:
            return
        if self._paused and self._pause_reason == "captcha":
            self.log_panel.append(
                "info",
                "Идёт ожидание капчи: пройдите проверку в окне браузера — парсинг "
                "возобновится автоматически.",
            )
            return
        self._runner.toggle_pause()

    def _on_pause_changed(self, paused: bool, reason: str) -> None:
        self._paused = paused
        self._pause_reason = reason if paused else ""
        self._sync_pause_action()
        if not paused:
            if self._running:
                self.status_label.setText("Парсинг продолжен")
                self.progress_detail.setText("Работа продолжается")
            return
        if reason == "captcha":
            self.status_label.setText("Пауза: требуется пройти капчу в браузере")
            self.progress_detail.setText("Ожидание прохождения проверки (до 2 минут)")
        else:
            self.status_label.setText("Пауза")
            self.progress_detail.setText("Парсинг приостановлен")

    def _sync_pause_action(self) -> None:
        style = self.style()
        if self._paused:
            self.action_pause.setText("Продолжить")
            self.action_pause.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
            self.action_pause.setToolTip(
                "Продолжить парсинг (F6)"
                if self._pause_reason != "captcha"
                else "Капча: пройдите проверку в окне браузера"
            )
            self.action_pause.setEnabled(self._running)
        else:
            self.action_pause.setText("Пауза")
            self.action_pause.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPause))
            self.action_pause.setToolTip("Поставить парсинг на паузу (F6)")
            self.action_pause.setEnabled(self._running)

    def _on_result(self, row: dict[str, Any]) -> None:
        # The table batches its counter/status updates, so no per-row UI work
        # is needed here beyond inserting the row itself.
        self.results_tab.add_row(row)

    def _on_results_count_changed(self, count: int) -> None:
        self._update_actions()
        if self._running:
            self.status_label.setText(f"Обработано страниц: {count}")

    def _on_runner_log(self, level: str, message: str) -> None:
        self.log_panel.append(level, message)

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0 / 0")
        else:
            done = min(max(done, 0), total)
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            self.progress_bar.setFormat(f"{done} / {total}")
        self.progress_detail.setText(f"Обработано {done} из {total}")
        self.status_label.setText(f"Парсинг: {done} из {total}")

    def _on_run_finished(self, summary: CrawlSummary) -> None:
        self.results_tab.flush_updates()
        self._last_summary = summary
        elapsed = time.monotonic() - self._run_started if self._run_started else summary.duration_seconds
        if summary.stopped:
            self.status_label.setText(f"Остановлено: обработано {summary.success + summary.errors}")
        else:
            self.status_label.setText(
                f"Готово: успешно {summary.success}, ошибок {summary.errors}, {elapsed:.1f} с"
            )
        self.progress_detail.setText(
            f"Успешно {summary.success} · ошибок {summary.errors} · {elapsed:.1f} с"
        )
        if summary.total_urls:
            self.progress_bar.setRange(0, summary.total_urls)
            self.progress_bar.setValue(summary.success + summary.errors)
            self.progress_bar.setFormat(
                f"{summary.success + summary.errors} / {summary.total_urls}"
            )
        if self.results_tab.row_count() > 0:
            self.log_panel.append(
                "system",
                f"Результатов: {self.results_tab.row_count()}. Нажмите «Экспорт» для сохранения.",
            )
        else:
            self.log_panel.append("warning", "Парсинг завершён без результатов.")
        if summary.worker_errors:
            for message in summary.worker_errors:
                self.log_panel.append("error", message)
        self._update_actions()

    def _on_run_failed(self, error: str) -> None:
        self.log_panel.append("error", f"Критическая ошибка парсинга: {error}")
        self.status_label.setText("Ошибка парсинга")
        self.progress_detail.setText("Завершено с ошибкой")
        short = error.splitlines()[0] if error else "Неизвестная ошибка"
        QMessageBox.critical(self, "Ошибка парсинга", short)

    def _on_runner_thread_finished(self) -> None:
        self._running = False
        self._runner = None
        self._paused = False
        self._pause_reason = ""
        self.results_tab.set_running(False)
        self._sync_pause_action()
        self._update_actions()
        self._update_title()

    def _reset_progress(self) -> None:
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0 / 0")
        self.progress_detail.setText("Ожидание запуска")

    def _focus_tab_for_errors(self, errors: list[str]) -> None:
        text = " ".join(errors).lower()
        if any(word in text for word in ("сценар", "поток", "задерж", "глубин", "сбора ссылок")):
            self.tabs.setCurrentWidget(self.navigation_tab)
        elif "поле" in text:
            self.tabs.setCurrentWidget(self.fields_tab)
        elif "плагин" in text or "экспорт" in text:
            self.tabs.setCurrentWidget(self.plugins_tab)
        elif "url" in text or "стартов" in text:
            self.tabs.setCurrentWidget(self.input_tab)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_current_results(self) -> None:
        if self._running:
            QMessageBox.information(self, "Экспорт", "Дождитесь завершения или остановите парсинг.")
            return
        if self.campaign is None:
            QMessageBox.information(self, "Экспорт", "Кампания не выбрана.")
            return
        rows = self.results_tab.all_rows()
        if not rows:
            QMessageBox.information(
                self, "Экспорт", "Нет результатов. Сначала запустите парсинг кампании."
            )
            return
        try:
            payload = normalize_campaign(self._compose_campaign())
        except Exception as exc:
            QMessageBox.critical(self, "Экспорт", f"Не удалось прочитать настройки экспорта:\n{exc}")
            return

        try:
            from core.exporter import resolve_export_path

            predicted_path, _ = resolve_export_path(payload, self.project_root)
            if predicted_path.exists():
                answer = QMessageBox.question(
                    self,
                    "Перезаписать файл?",
                    f"Файл уже существует:\n{predicted_path}\n\nПерезаписать его?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            report = export_results(payload, rows, base_dir=self.project_root)
        except ExportError as exc:
            QMessageBox.critical(self, "Экспорт", str(exc))
            self.log_panel.append("error", f"Экспорт: {exc}")
            return
        except OSError as exc:
            QMessageBox.critical(self, "Экспорт", f"Ошибка файловой системы:\n{exc}")
            self.log_panel.append("error", f"Экспорт: {exc}")
            return

        self.log_panel.append(
            "success",
            f"Экспорт завершён: {report.path} ({report.exported_rows} строк, "
            f"{report.columns} колонок, {report.format.upper()}).",
        )
        self.status_label.setText(f"Экспортировано {report.exported_rows} строк")
        self._show_export_success(report.path, report.exported_rows, report.format)

    def _show_export_success(self, path: Path, rows: int, export_format: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Экспорт завершён")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Сохранено строк: {rows}\nФормат: {export_format.upper()}")
        box.setInformativeText(str(path))
        open_button = box.addButton("Открыть папку", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_button:
            self._open_folder(path.parent)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _open_page_picker(self, url: str) -> None:
        self._open_picker(-1, url)

    def _open_dynamic_picker(self, url: str) -> None:
        self._open_picker(-1, url, dynamic=True)

    def _open_picker(
        self,
        row: int,
        url: str | None = None,
        dynamic: bool = False,
    ) -> None:
        if self.campaign is None:
            return
        if not url:
            url = self.fields_tab.test_url()
        if not url:
            urls = self.input_tab.start_urls()
            url = urls[0] if urls else "https://"
        dialog = XPathPickerDialog(url, self, dynamic=dynamic)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.dynamic:
                payload = dialog.dynamic_payload
                applied = self._apply_dynamic_block(payload, url)
                if applied:
                    self.log_panel.append(
                        "success",
                        "Пикер собрал динамический блок характеристик: "
                        f"{len(dialog.dynamic_pairs)} пар.",
                    )
                return
            selector = dialog.selector
            if not selector:
                return
            payload = {
                "name": dialog.suggested_name,
                "type": dialog.field_type,
                "selector": selector,
                "xpath": dialog.xpath_edit.text().strip(),
                "css": dialog.css_selector,
                "preview": dialog.preview,
            }
            target = self.fields_tab.apply_pick(row, payload)
            if target >= 0:
                self.fields_tab.set_test_url(url)
                fields = self.fields_tab.fields()
                name = fields[target].get("name", "") if target < len(fields) else ""
                self.log_panel.append("success", f"Пикер вернул XPath для поля «{name}»: {selector}")
                if dialog.preview:
                    self.log_panel.append("info", f"Превью: {dialog.preview[:240]}")

    def _auto_detect_fields(self, url: str) -> None:
        if self.campaign is None:
            return
        if self._auto_worker is not None and self._auto_worker.isRunning():
            return
        try:
            campaign = self._compose_campaign()
        except StorageError:
            return
        self.fields_tab.set_busy(True)
        self.status_label.setText("Автозаполнение: загружаю страницу…")
        self.log_panel.append("info", f"Автозаполнение по URL: {url}")
        worker = AutoDetectWorker(campaign, url, self)
        worker.succeeded.connect(
            lambda detected, snapshot: self._show_auto_detect(detected, snapshot, url)
        )
        worker.failed.connect(
            lambda message: self._on_inspector_failed("Автозаполнение по URL", message)
        )
        worker.finished.connect(self._on_auto_worker_finished)
        self._auto_worker = worker
        worker.start()

    def _on_auto_worker_finished(self) -> None:
        self.fields_tab.set_busy(False)
        self._auto_worker = None

    def _show_auto_detect(self, detected: list, snapshot: object, url: str) -> None:
        snapshot_obj = snapshot if hasattr(snapshot, "final_url") else None
        if not detected:
            self.status_label.setText("Автозаполнение: полей не найдено")
            self.log_panel.append(
                "warn", "На странице не найдено JSON-LD Product, OpenGraph, микроразметки или таблиц."
            )
            QMessageBox.information(
                self,
                "Автозаполнение по URL",
                "На этой странице не удалось найти поля товара.\n"
                "Проверьте адрес или выберите элементы вручную через «Выбрать на странице».",
            )
            return
        dialog = AutoDetectDialog(detected, snapshot_obj, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("Автозаполнение отменено")
            return

        selected = dialog.selected_detected()
        static_items = [item.as_field() for item in selected if not item.is_dynamic]
        dynamic_items = [item for item in selected if item.is_dynamic]
        added = self.fields_tab.add_detected(static_items)
        dynamic_added = False
        for item in dynamic_items[:1]:
            dynamic_added = self._apply_dynamic_block(
                {
                    "selector": item.selector,
                    "source_field": item.source_field,
                    "row_selector": item.row_selector,
                    "name_selector": item.name_selector,
                    "value_selector": item.value_selector,
                    "source": item.source,
                    "pairs": item.pairs,
                },
                url,
            )
        self.fields_tab.set_test_url(url)
        parts = [f"полей — {added}"]
        if dynamic_added:
            parts.append("динамические характеристики — 1 блок")
        self.status_label.setText("Автозаполнение: " + ", ".join(parts))
        self.log_panel.append(
            "success",
            f"Автозаполнение добавило {added} из {len(detected)} найденных полей"
            + (" и динамический блок характеристик." if dynamic_added else "."),
        )

    def _apply_dynamic_block(self, payload: dict, url: str | None = None) -> bool:
        """Store one dynamic specs block and configure the split-specs plugin."""

        row = self.fields_tab.add_dynamic_block(payload)
        if row < 0:
            return False
        fields = self.fields_tab.fields()
        if row >= len(fields):
            return False
        source_name = str(fields[row].get("name") or "")
        row_selector = str(payload.get("row_selector") or "")
        name_selector = str(payload.get("name_selector") or "")
        value_selector = str(payload.get("value_selector") or "")
        self.plugins_tab.apply_specs_block(
            source_name,
            row_selector,
            name_selector,
            value_selector,
            self.fields_tab.field_names(),
        )
        if url:
            self.fields_tab.set_test_url(url)
        pairs = payload.get("pairs") or []
        preview = " · ".join(f"{name}: {value}" for name, value in list(pairs)[:3])
        self.log_panel.append(
            "success",
            f"Динамические характеристики: поле «{source_name}», пар — {len(pairs)}.",
        )
        if preview:
            self.log_panel.append("info", f"Превью характеристик: {preview[:240]}")
        return True

    def _test_fields(self, url: str) -> None:
        if self.campaign is None:
            return
        if not self.fields_tab.fields():
            QMessageBox.information(
                self,
                "Тест на 1 URL",
                "Сначала добавьте хотя бы одно поле данных.",
            )
            return
        try:
            campaign = self._compose_campaign()
        except StorageError:
            return
        self.fields_tab.set_test_url(url)
        self.log_panel.append("info", f"Тест на 1 URL: {url}")
        dialog = TestRunDialog(campaign, url, None, self)
        dialog.exec()
        self.status_label.setText("Тест на 1 URL завершён")

    def _on_inspector_failed(self, title: str, message: str) -> None:
        self.fields_tab.set_busy(False)
        self.status_label.setText(f"{title}: ошибка")
        self.log_panel.append("error", f"{title}: {message}")
        QMessageBox.critical(self, title, message)

    def _report_validation_errors(self, errors: list[str]) -> None:
        for message in errors:
            self.log_panel.append("error", message)
        shown = errors[:12]
        text = "\n".join(f"• {message}" for message in shown)
        if len(errors) > len(shown):
            text += f"\n…и ещё {len(errors) - len(shown)}"
        QMessageBox.warning(self, "Проверьте конфигурацию", text)
        self.status_label.setText("Ошибка проверки конфигурации")

    def _update_actions(self) -> None:
        has_campaign = self.campaign is not None
        self.action_save.setEnabled(has_campaign and not self._running)
        self.action_run.setEnabled(has_campaign and not self._running)
        self.action_stop.setEnabled(self._running)
        self.action_export.setEnabled(
            has_campaign and not self._running and self.results_tab.row_count() > 0
        )
        self.action_new.setEnabled(not self._running)
        self.action_refresh.setEnabled(not self._running)
        self._sync_pause_action()

    def _open_folder(self, path: str | Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def _show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("О программе FOSScol")
        box.setWindowIcon(app_icon())
        box.setTextFormat(Qt.TextFormat.RichText)
        pixmap = logo_pixmap()
        if not pixmap.isNull():
            box.setIconPixmap(
                pixmap.scaled(
                    64,
                    64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        box.setText(
            "<b>FOSScol</b> — кроссплатформенный парсер для Windows и Linux.<br><br>"
            "PySide6 · Playwright Chromium · lxml · openpyxl<br>"
            "Многопоточный обход, сценарии, XPath/regex-поля, "
            "расфасовка характеристик и экспорт XLSX/CSV.<br><br>"
            f"Папка кампаний: {self.storage.directory}"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def closeEvent(self, event: Any) -> None:
        worker = self._auto_worker
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(15000):
                QMessageBox.warning(
                    self,
                    "Завершение",
                    "Фоновый поток автозаполнения не завершился за 15 секунд. "
                    "Дождитесь его завершения и повторите выход.",
                )
                event.ignore()
                return
        self._auto_worker = None
        if self._runner is not None and self._runner.isRunning():
            answer = QMessageBox.question(
                self,
                "Парсинг выполняется",
                "Остановить парсинг и выйти из программы?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._runner.stop()
            if not self._runner.wait(15000):
                QMessageBox.warning(
                    self,
                    "Завершение",
                    "Поток парсинга не завершился за 15 секунд. Попробуйте ещё раз после остановки.",
                )
                event.ignore()
                return
        if not self._confirm_leave():
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        event.accept()
