"""Base contract for FOSScol plugins.

A plugin receives one extracted row at a time and may add, change or remove
keys.  Plugins are intentionally stateless: the parse engine calls
``process_row`` concurrently from several worker threads, so all mutable
settings must live in ``config`` and the returned dictionary.
"""

from __future__ import annotations

from typing import Any


class BasePlugin:
    """Minimal interface implemented by built-in and user plugins."""

    #: Stable identifier used in ``campaign["plugins"]``.
    id: str = ""
    #: Human readable name shown in the plugin settings tab.
    name: str = ""
    #: Short explanation of what the plugin does.
    description: str = ""
    #: Execution and display priority.  Lower values run first.
    order: int = 100

    def default_config(self) -> dict[str, Any]:
        """Return the configuration used for a new campaign."""

        return {"enabled": False}

    def config_schema(self) -> list[dict[str, Any]]:
        """Describe editable settings for the automatic plugin card.

        Supported ``type`` values are ``bool``, ``str``, ``int``, ``choice``
        and ``field_name``.  An empty list keeps the generic card minimal.
        """

        return []

    def process_row(
        self,
        row: dict[str, Any],
        config: dict[str, Any],
        campaign: dict[str, Any],
    ) -> dict[str, Any]:
        """Transform one result row and return it.

        Returning the same dictionary is fine; plugins may also mutate it in
        place and return ``None`` for backwards compatibility.
        """

        return row


def plugin_sort_key(plugin: BasePlugin) -> tuple[int, str]:
    """Stable ``(order, id)`` key used by the chain and the plugin list."""

    try:
        order = int(getattr(plugin, "order", 100))
    except (TypeError, ValueError):
        order = 100
    return order, str(getattr(plugin, "id", "") or "")
