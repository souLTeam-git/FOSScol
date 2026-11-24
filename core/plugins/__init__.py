"""Modular plugin system.

Importing this package is intentionally cheap: the registry and the plugin
modules are loaded lazily through :mod:`core.plugins.registry` when needed.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BasePlugin",
    "PluginEntry",
    "PluginRegistry",
    "apply_plugins",
    "get_registry",
    "is_plugin_active",
    "resolve_plugins",
    "specs_parser_config",
]


def __getattr__(name: str) -> Any:
    if name == "BasePlugin":
        from core.plugins.base import BasePlugin

        return BasePlugin
    if name in {"PluginEntry", "PluginRegistry", "get_registry"}:
        from core.plugins import registry

        return getattr(registry, name)
    if name in {"apply_plugins", "resolve_plugins", "specs_parser_config", "is_plugin_active"}:
        from core.plugins import chain

        return getattr(chain, name)
    raise AttributeError(name)
