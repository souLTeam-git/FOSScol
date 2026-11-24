"""Apply the active plugin chain to one result row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.plugins.base import BasePlugin, plugin_sort_key
from core.plugins.registry import PluginRegistry, get_registry


@dataclass(frozen=True)
class ResolvedPlugin:
    plugin: BasePlugin
    config: dict[str, Any]


def campaign_plugin_configs(campaign: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only nested plugin configurations from a campaign."""

    raw = campaign.get("plugins") if isinstance(campaign.get("plugins"), dict) else {}
    return {
        str(plugin_id): config
        for plugin_id, config in raw.items()
        if isinstance(config, dict)
    }


def specs_parser_config(campaign: dict[str, Any]) -> dict[str, Any]:
    """Configuration of the specs plugin, including the legacy flat format."""

    configs = campaign_plugin_configs(campaign)
    nested = configs.get("specs_parser")
    if nested is not None:
        return nested
    raw = campaign.get("plugins") if isinstance(campaign.get("plugins"), dict) else {}
    if not any(
        key in raw
        for key in (
            "split_specs_to_columns",
            "specs_source_field",
            "specs_row_xpath",
            "specs_name_xpath",
            "specs_val_xpath",
            "priority_columns",
        )
    ):
        return {}
    return {
        "enabled": bool(raw.get("split_specs_to_columns", False)),
        "source_field": str(raw.get("specs_source_field") or ""),
        "row_xpath": str(raw.get("specs_row_xpath") or ""),
        "name_xpath": str(raw.get("specs_name_xpath") or ""),
        "value_xpath": str(raw.get("specs_val_xpath") or ""),
        "priority_columns": raw.get("priority_columns") or [],
    }


def specs_parser_enabled(campaign: dict[str, Any]) -> bool:
    return bool(specs_parser_config(campaign).get("enabled", False))


def is_plugin_active(
    campaign: dict[str, Any],
    plugin_id: str,
    registry: PluginRegistry | None = None,
) -> bool:
    """True only when the plugin is enabled *and* its file is installed."""

    return any(
        run.plugin.id == plugin_id
        for run in resolve_plugins(campaign, registry=registry)
    )


def resolve_plugins(
    campaign: dict[str, Any],
    registry: PluginRegistry | None = None,
) -> list[ResolvedPlugin]:
    """Return enabled plugins that are actually installed.

    Configurations for deleted or missing plugin files are silently skipped,
    which keeps old campaigns usable after a user removes a plugin.
    """

    registry = registry or get_registry()
    configs = campaign_plugin_configs(campaign)
    resolved: list[ResolvedPlugin] = []

    # A raw legacy campaign may still contain only the flat specs keys.
    if "specs_parser" not in configs:
        legacy = specs_parser_config(campaign)
        if legacy.get("enabled"):
            plugin = registry.get("specs_parser")
            if plugin is not None:
                resolved.append(ResolvedPlugin(plugin, legacy))

    for plugin_id, config in configs.items():
        if plugin_id == "specs_parser" and any(
            item.plugin.id == "specs_parser" for item in resolved
        ):
            continue
        if not bool(config.get("enabled", False)):
            continue
        plugin = registry.get(plugin_id)
        if plugin is None:
            continue
        resolved.append(ResolvedPlugin(plugin, config))
    # Explicit order makes chains predictable: parsing plugins can run before
    # generic user plugins, and final normalisers after them.
    resolved.sort(key=lambda run: plugin_sort_key(run.plugin))
    return resolved


def apply_plugins(
    row: dict[str, Any],
    campaign: dict[str, Any],
    runs: list[ResolvedPlugin] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Run every active plugin over ``row`` and return the transformed row."""

    result = row
    for run in runs if runs is not None else resolve_plugins(campaign):
        try:
            updated = run.plugin.process_row(result, dict(run.config), campaign)
        except Exception as exc:  # noqa: BLE001 - one plugin must not stop parsing
            if errors is not None:
                message = str(exc).strip().splitlines()
                detail = message[0] if message else exc.__class__.__name__
                errors.append(
                    f"Плагин «{run.plugin.name or run.plugin.id}»: "
                    f"{exc.__class__.__name__}: {detail}"
                )
            continue
        if isinstance(updated, dict):
            result = updated

    # Final type normalisation: plugins may leave list values (for example
    # ``xpath_attr_list`` with the separator plugin disabled).  The core uses
    # one plain default join; custom separators belong to plugins.
    for key, value in result.items():
        if isinstance(value, list):
            result[key] = ",".join(str(item) for item in value if item)
    return result
