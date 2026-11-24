"""Discovery and loading of plugins from the project ``plugins`` folder.

All plugins are equal: the bundled ``specs_parser``/``list_separator`` files
sit next to user supplied plugins and are loaded through exactly the same code
path.  Import errors are isolated per file so a broken or deleted plugin can
never prevent the application from starting.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from core.plugins.base import BasePlugin, plugin_sort_key


def project_root_path() -> Path:
    """Repository root (the directory that contains ``main.py``)."""

    return Path(__file__).resolve().parents[2]


def plugins_dir(project_root: str | Path | None = None) -> Path:
    root = Path(project_root).expanduser().resolve() if project_root else project_root_path()
    return root / "plugins"


@dataclass(frozen=True)
class PluginEntry:
    """One plugin loaded from a file."""

    plugin: BasePlugin
    path: Path
    module_name: str


class PluginRegistry:
    """Scan and import every ``*.py`` file in the project ``plugins`` folder."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = (
            Path(project_root).expanduser().resolve() if project_root else project_root_path()
        )
        self.plugin_dir = plugins_dir(self.project_root)
        self._lock = threading.RLock()
        self._entries: dict[str, PluginEntry] = {}
        self._module_seq = 0
        self.errors: list[str] = []

    # ------------------------------------------------------------------
    def refresh(self) -> list[str]:
        """Rescan the plugins folder and return import warnings."""

        with self._lock:
            entries: dict[str, PluginEntry] = {}
            errors: list[str] = []
            try:
                self.plugin_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"Не удалось создать папку plugins: {exc}")
            if self.plugin_dir.is_dir():
                for path in sorted(self.plugin_dir.glob("*.py")):
                    if path.name.startswith("_"):
                        continue
                    self._load_file(path, entries, errors)
            self._entries = entries
            self.errors = errors
            return list(errors)

    def _load_file(
        self,
        path: Path,
        entries: dict[str, PluginEntry],
        errors: list[str],
    ) -> None:
        self._module_seq += 1
        module_name = f"fosscol_plugin_{path.stem}_{self._module_seq}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("не удалось создать загрузчик модуля")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a plugin must never break startup
            sys.modules.pop(module_name, None)
            message = str(exc).strip().splitlines()
            detail = message[0] if message else exc.__class__.__name__
            errors.append(f"{path.name}: {exc.__class__.__name__}: {detail}")
            return

        found = False
        for candidate in list(vars(module).values()):
            if not isinstance(candidate, type):
                continue
            if candidate is BasePlugin or not issubclass(candidate, BasePlugin):
                continue
            if getattr(candidate, "__module__", "") != module_name:
                continue
            found = True
            try:
                plugin = candidate()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.name}: не удалось создать {candidate.__name__}: {exc}")
                continue
            plugin_id = str(getattr(plugin, "id", "") or "").strip()
            if not plugin_id:
                errors.append(f"{path.name}: у класса {candidate.__name__} не задан id.")
                continue
            if plugin_id in entries:
                errors.append(
                    f"{path.name}: id «{plugin_id}» уже занят файлом "
                    f"{entries[plugin_id].path.name}, файл пропущен."
                )
                continue
            entries[plugin_id] = PluginEntry(
                plugin=plugin,
                path=path,
                module_name=module_name,
            )
        if not found:
            errors.append(f"{path.name}: не найден класс-наследник BasePlugin.")

    # ------------------------------------------------------------------
    def all(self) -> list[PluginEntry]:
        with self._lock:
            return sorted(
                self._entries.values(),
                key=lambda entry: plugin_sort_key(entry.plugin),
            )

    def get(self, plugin_id: str) -> BasePlugin | None:
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            return entry.plugin if entry is not None else None

    def entry(self, plugin_id: str) -> PluginEntry | None:
        with self._lock:
            return self._entries.get(str(plugin_id))


_REGISTRIES: dict[str, PluginRegistry] = {}
_REGISTRY_LOCK = threading.Lock()


def get_registry(project_root: str | Path | None = None) -> PluginRegistry:
    """Return the shared registry for ``project_root`` (created on demand)."""

    root = Path(project_root).expanduser().resolve() if project_root else project_root_path()
    key = str(root)
    with _REGISTRY_LOCK:
        registry = _REGISTRIES.get(key)
        if registry is None:
            registry = PluginRegistry(root)
            registry.refresh()
            _REGISTRIES[key] = registry
    return registry
