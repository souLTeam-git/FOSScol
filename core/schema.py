"""Campaign data model, defaults and validation.

Campaigns are stored as plain JSON documents.  This module keeps the
canonical shape of those documents in one place so that the storage, the
user interface and the parsing engine always agree on field names.
"""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

from core.plugins.defaults import parse_target_fields
from core.specs_columns import parse_priority_columns

FIELD_TYPES: tuple[str, ...] = (
    "xpath",
    "xpath_outer_html",
    "xpath_attr",
    "xpath_attr_list",
    "regex",
    "spec_url",
)

FIELD_TYPE_LABELS: dict[str, str] = {
    "xpath": "XPath (текст)",
    "xpath_outer_html": "XPath (outer HTML)",
    "xpath_attr": "XPath (атрибут)",
    "xpath_attr_list": "XPath (все атрибуты)",
    "regex": "Регулярное выражение",
    "spec_url": "URL страницы",
}

SCENARIO_ACTIONS: tuple[str, ...] = ("click", "hover", "wait", "scroll")

SCENARIO_ACTION_LABELS: dict[str, str] = {
    "click": "Клик",
    "hover": "Наведение",
    "wait": "Пауза",
    "scroll": "Прокрутка",
}

EXPORT_FORMATS: tuple[str, ...] = ("xlsx", "csv")

DEFAULT_ENGINE: dict[str, Any] = {
    "mode": "playwright_chromium",
    "headless": False,
    "threads": 3,
    "delay_min_ms": 3000,
    "delay_max_ms": 7000,
    "disable_images": True,
    "block_heavy_resources": True,
    "proxy_list": "",
}

DEFAULT_NAVIGATION: dict[str, Any] = {
    "gather_links_xpaths": [],
    "max_depth": 3,
}

DEFAULT_INPUT: dict[str, Any] = {
    "start_urls": [],
}

DEFAULT_PLUGINS: dict[str, Any] = {}

# Campaigns created before the plugin system kept these keys directly in
# ``campaign["plugins"]``.  They are migrated to the ``specs_parser`` config.
LEGACY_SPECS_KEYS: tuple[str, ...] = (
    "split_specs_to_columns",
    "specs_source_field",
    "specs_row_xpath",
    "specs_name_xpath",
    "specs_val_xpath",
    "priority_columns",
)

_LEGACY_SPECS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "source_field": "Характеристики",
    "row_xpath": "//tr",
    "name_xpath": "//tr/td[1]",
    "value_xpath": "//tr/td[2]",
    "priority_columns": [],
}

DEFAULT_EXPORT: dict[str, Any] = {
    "format": "xlsx",
    "file_path": "./exports/Результат.xlsx",
}

DEFAULT_FIELD: dict[str, Any] = {
    "name": "Новое поле",
    "type": "xpath",
    "selector": "",
    "strip_tags": False,
}


class CampaignValidationError(ValueError):
    """Raised when a campaign cannot be normalised or validated."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "да"}:
            return True
        if lowered in {"0", "false", "no", "off", "нет", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    return result


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        return [str(value)]
    return [str(item) for item in value if item is not None]


def new_field(name: str = "Новое поле", selector: str = "") -> dict[str, Any]:
    field = dict(DEFAULT_FIELD)
    field["name"] = name
    field["selector"] = selector
    return field


def new_scenario_step(action: str = "click") -> dict[str, Any]:
    return {
        "action": action,
        "xpath": "",
        "wait_ms": 1000,
        "optional": True,
    }


def new_campaign(name: str, category: str = "") -> dict[str, Any]:
    """Return a complete, empty campaign document."""

    return normalize_campaign(
        {
            "campaign_name": name,
            "category": category,
            "engine": copy.deepcopy(DEFAULT_ENGINE),
            "scenario": [],
            "input": {"start_urls": []},
            "navigation": copy.deepcopy(DEFAULT_NAVIGATION),
            "fields": [],
            "plugins": copy.deepcopy(DEFAULT_PLUGINS),
            "export": copy.deepcopy(DEFAULT_EXPORT),
        }
    )


def _normalize_plugin_config(plugin_id: str, config: Any) -> dict[str, Any]:
    """Coerce one nested plugin configuration, keeping unknown keys.

    Configuration is plugin defined: the schema only normalises the generic
    ``enabled`` flag and the legacy/compatibility keys it knows about.
    """

    source = config if isinstance(config, dict) else {}
    result = copy.deepcopy(source)

    if "enabled" not in result and "split_specs_to_columns" in result:
        result["enabled"] = _as_bool(result.get("split_specs_to_columns"), False)
    else:
        result["enabled"] = _as_bool(result.get("enabled"), False)

    if plugin_id == "specs_parser":
        aliases = {
            "source_field": "specs_source_field",
            "row_xpath": "specs_row_xpath",
            "name_xpath": "specs_name_xpath",
            "value_xpath": "specs_val_xpath",
        }
        for key, legacy in aliases.items():
            if key not in result and legacy in result:
                result[key] = _as_text(result.get(legacy))
        if "priority_columns" in result:
            result["priority_columns"] = parse_priority_columns(
                result.get("priority_columns")
            )
        # Old readers still look for this flag inside the specs configuration.
        result["split_specs_to_columns"] = result["enabled"]
    elif plugin_id == "list_separator":
        separator = result.get("separator")
        if separator is None:
            result["separator"] = ", "
        else:
            result["separator"] = str(separator)
        if "target_fields" in result:
            result["target_fields"] = parse_target_fields(result.get("target_fields"))
    return result


def normalize_campaign(raw: Any) -> dict[str, Any]:
    """Fill missing values and coerce scalar types.

    Unknown keys are preserved to keep campaigns forward-compatible.
    """

    if not isinstance(raw, dict):
        raw = {}

    campaign: dict[str, Any] = copy.deepcopy(raw)
    campaign["campaign_name"] = _as_text(raw.get("campaign_name")).strip() or "Новая кампания"
    campaign["category"] = _as_text(raw.get("category")).strip()

    raw_engine = raw.get("engine") if isinstance(raw.get("engine"), dict) else {}
    engine = copy.deepcopy(DEFAULT_ENGINE)
    engine.update(raw_engine)
    engine["mode"] = _as_text(engine.get("mode")) or "playwright_chromium"
    engine["headless"] = _as_bool(engine.get("headless"), False)
    engine["threads"] = _as_int(engine.get("threads"), 3, 1)
    engine["delay_min_ms"] = _as_int(engine.get("delay_min_ms"), 3000, 0)
    engine["delay_max_ms"] = _as_int(engine.get("delay_max_ms"), 7000, 0)
    if engine["delay_max_ms"] < engine["delay_min_ms"]:
        engine["delay_max_ms"] = engine["delay_min_ms"]
    engine["disable_images"] = _as_bool(engine.get("disable_images"), True)
    engine["block_heavy_resources"] = _as_bool(engine.get("block_heavy_resources"), True)
    engine["proxy_list"] = _as_text(engine.get("proxy_list"))
    campaign["engine"] = engine

    raw_input = raw.get("input") if isinstance(raw.get("input"), dict) else {}
    start_urls = _as_string_list(raw_input.get("start_urls"))
    normalize_input = copy.deepcopy(DEFAULT_INPUT)
    normalize_input.update(raw_input)
    normalize_input["start_urls"] = start_urls
    campaign["input"] = normalize_input

    raw_navigation = raw.get("navigation") if isinstance(raw.get("navigation"), dict) else {}
    navigation = copy.deepcopy(DEFAULT_NAVIGATION)
    navigation.update(raw_navigation)
    navigation["gather_links_xpaths"] = _as_string_list(navigation.get("gather_links_xpaths"))
    navigation["max_depth"] = _as_int(navigation.get("max_depth"), 3, 0)
    campaign["navigation"] = navigation

    scenario: list[dict[str, Any]] = []
    raw_scenario = raw.get("scenario")
    if isinstance(raw_scenario, list):
        for item in raw_scenario:
            if not isinstance(item, dict):
                continue
            step: dict[str, Any] = {}
            step.update(item)
            step["action"] = _as_text(item.get("action")).strip().lower() or "click"
            step["xpath"] = _as_text(item.get("xpath")).strip()
            step["wait_ms"] = _as_int(item.get("wait_ms"), 1000, 0)
            step["optional"] = _as_bool(item.get("optional"), True)
            scenario.append(step)
    campaign["scenario"] = scenario

    fields: list[dict[str, Any]] = []
    raw_fields = raw.get("fields")
    if isinstance(raw_fields, list):
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            field: dict[str, Any] = {}
            field.update(item)
            field["name"] = _as_text(item.get("name")).strip()
            field_type = _as_text(item.get("type")).strip().lower()
            field["type"] = field_type if field_type in FIELD_TYPES else "xpath"
            field["selector"] = _as_text(item.get("selector"))
            field["strip_tags"] = _as_bool(item.get("strip_tags"), False)
            if "replace_remove" in item:
                field["replace_remove"] = _as_string_list(item.get("replace_remove"))
            fields.append(field)
    campaign["fields"] = fields

    raw_plugins = raw.get("plugins") if isinstance(raw.get("plugins"), dict) else {}
    nested_raw = {
        str(plugin_id): copy.deepcopy(config)
        for plugin_id, config in raw_plugins.items()
        if isinstance(config, dict)
    }
    plugins: dict[str, Any] = dict(nested_raw)

    # Campaigns created before the plugin system stored the specs settings as
    # flat keys in ``campaign["plugins"]``.  Convert them once, keeping every
    # value (including priority synonym groups).
    if "specs_parser" not in plugins and any(
        key in raw_plugins for key in LEGACY_SPECS_KEYS
    ):
        legacy = copy.deepcopy(_LEGACY_SPECS_DEFAULTS)
        if "split_specs_to_columns" in raw_plugins:
            legacy["enabled"] = _as_bool(raw_plugins.get("split_specs_to_columns"), False)
        if "specs_source_field" in raw_plugins:
            legacy["source_field"] = _as_text(raw_plugins.get("specs_source_field"))
        if "specs_row_xpath" in raw_plugins:
            legacy["row_xpath"] = _as_text(raw_plugins.get("specs_row_xpath"))
        if "specs_name_xpath" in raw_plugins:
            legacy["name_xpath"] = _as_text(raw_plugins.get("specs_name_xpath"))
        if "specs_val_xpath" in raw_plugins:
            legacy["value_xpath"] = _as_text(raw_plugins.get("specs_val_xpath"))
        if "priority_columns" in raw_plugins:
            legacy["priority_columns"] = raw_plugins.get("priority_columns")
        plugins["specs_parser"] = legacy

    campaign["plugins"] = {
        str(plugin_id): _normalize_plugin_config(plugin_id, config)
        for plugin_id, config in plugins.items()
    }

    raw_export = raw.get("export") if isinstance(raw.get("export"), dict) else {}
    export = copy.deepcopy(DEFAULT_EXPORT)
    export.update(raw_export)
    export_format = _as_text(export.get("format")).strip().lower()
    if export_format not in EXPORT_FORMATS:
        export_format = "xlsx"
    export["format"] = export_format
    export["file_path"] = _as_text(export.get("file_path")).strip() or "./exports/Результат.xlsx"
    campaign["export"] = export

    return campaign


def _check_xpath(errors: list[str], label: str, selector: str) -> None:
    if not selector.strip():
        return
    try:
        etree.XPath(selector)
    except etree.XPathSyntaxError as exc:
        errors.append(f"{label}: некорректный XPath ({exc})")


def validate_campaign(campaign: dict[str, Any], for_run: bool = False) -> list[str]:
    """Return a list of human readable validation errors."""

    errors: list[str] = []
    name = _as_text(campaign.get("campaign_name")).strip()
    if not name:
        errors.append("Не задано имя кампании.")
    if "/" in name or "\\" in name:
        errors.append("Имя кампании не должно содержать символы '/' и '\\'.")

    engine = campaign.get("engine") if isinstance(campaign.get("engine"), dict) else {}
    threads = _as_int(engine.get("threads"), 3, 1)
    if threads > 32:
        errors.append("Количество потоков не должно превышать 32.")
    delay_min = _as_int(engine.get("delay_min_ms"), 0, 0)
    delay_max = _as_int(engine.get("delay_max_ms"), 0, 0)
    if delay_max < delay_min:
        errors.append("Максимальная задержка меньше минимальной.")

    navigation = campaign.get("navigation") if isinstance(campaign.get("navigation"), dict) else {}
    for index, xpath in enumerate(navigation.get("gather_links_xpaths", []) or [], start=1):
        _check_xpath(errors, f"XPath сбора ссылок №{index}", _as_text(xpath))
    if _as_int(navigation.get("max_depth"), 0, 0) < 0:
        errors.append("Глубина обхода не может быть отрицательной.")

    field_names: set[str] = set()
    fields = campaign.get("fields") if isinstance(campaign.get("fields"), list) else []
    for index, field in enumerate(fields, start=1):
        if not isinstance(field, dict):
            continue
        field_name = _as_text(field.get("name")).strip()
        field_type = _as_text(field.get("type")).strip()
        selector = _as_text(field.get("selector"))
        if not field_name:
            errors.append(f"Поле №{index}: не задано имя.")
        elif field_name in field_names:
            errors.append(f"Поле «{field_name}»: имя повторяется.")
        else:
            field_names.add(field_name)
        if field_type not in FIELD_TYPES:
            errors.append(f"Поле «{field_name or index}»: неизвестный тип «{field_type}».")
            continue
        if field_type == "spec_url":
            continue
        if not selector.strip():
            errors.append(f"Поле «{field_name or index}»: не задан селектор.")
            continue
        if field_type == "regex":
            try:
                re.compile(selector, re.DOTALL)
            except re.error as exc:
                errors.append(f"Поле «{field_name or index}»: ошибка регулярного выражения ({exc}).")
        else:
            _check_xpath(errors, f"Поле «{field_name or index}»", selector)

    scenario = campaign.get("scenario") if isinstance(campaign.get("scenario"), list) else []
    for index, step in enumerate(scenario, start=1):
        if not isinstance(step, dict):
            continue
        action = _as_text(step.get("action")).strip().lower()
        if action not in SCENARIO_ACTIONS:
            errors.append(f"Сценарий, шаг {index}: неизвестное действие «{action}».")
            continue
        if action in {"click", "hover"}:
            xpath = _as_text(step.get("xpath")).strip()
            if not xpath:
                errors.append(f"Сценарий, шаг {index}: не задан XPath элемента.")
            else:
                _check_xpath(errors, f"Сценарий, шаг {index}", xpath)

    plugins = campaign.get("plugins") if isinstance(campaign.get("plugins"), dict) else {}
    specs = plugins.get("specs_parser") if isinstance(plugins.get("specs_parser"), dict) else {}
    if _as_bool(specs.get("enabled"), False):
        source = _as_text(specs.get("source_field")).strip()
        if not source:
            errors.append("Плагин характеристик: не выбрано исходное поле.")
        elif source not in field_names:
            errors.append(f"Плагин характеристик: поле «{source}» отсутствует в списке полей.")
        for key, label in (
            ("row_xpath", "XPath строк"),
            ("name_xpath", "XPath названий"),
            ("value_xpath", "XPath значений"),
        ):
            _check_xpath(errors, f"Плагин характеристик ({label})", _as_text(specs.get(key)))

    export = campaign.get("export") if isinstance(campaign.get("export"), dict) else {}
    if export.get("format") not in EXPORT_FORMATS:
        errors.append("Экспорт: поддерживаются только форматы XLSX и CSV.")
    if not _as_text(export.get("file_path")).strip():
        errors.append("Экспорт: не задан путь к файлу.")

    if for_run:
        urls = (campaign.get("input") or {}).get("start_urls") or []
        valid_urls = 0
        for url in urls:
            parsed = urlsplit(_as_text(url))
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                valid_urls += 1
        if valid_urls == 0:
            errors.append("Для запуска добавьте хотя бы один корректный URL (http:// или https://).")
        if not fields:
            errors.append("Для запуска добавьте хотя бы одно поле данных.")

    return errors


def ensure_valid(campaign: dict[str, Any], for_run: bool = False) -> dict[str, Any]:
    normalized = normalize_campaign(campaign)
    errors = validate_campaign(normalized, for_run=for_run)
    if errors:
        raise CampaignValidationError(errors)
    return normalized
