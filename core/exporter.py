"""Result export to Excel (XLSX) and CSV."""

from __future__ import annotations

import csv
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.utils.exceptions import IllegalCharacterError

from core.plugins.chain import (
    apply_plugins,
    is_plugin_active,
    resolve_plugins,
    specs_parser_config,
)
from core.specs_columns import order_dynamic_columns

INTERNAL_PREFIX = "__"
PLUGINS_APPLIED_KEY = "__plugins_applied__"


class ExportError(RuntimeError):
    """Raised when a result file cannot be written."""


@dataclass(frozen=True)
class ExportReport:
    path: Path
    format: str
    exported_rows: int
    skipped_rows: int
    columns: int


def _is_internal(key: str) -> bool:
    return key.startswith(INTERNAL_PREFIX)


def _split_specs_config(
    campaign: dict[str, Any],
    registry=None,
) -> tuple[bool, str, dict[str, Any]]:
    config = specs_parser_config(campaign)
    enabled = bool(config.get("enabled", False)) and is_plugin_active(
        campaign, "specs_parser", registry=registry
    )
    source = str(config.get("source_field") or "").strip()
    return enabled, source, config


def expand_spec_columns(
    campaign: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    registry=None,
) -> list[dict[str, Any]]:
    """Apply the active plugin chain to rows that have not seen it yet.

    The engine normally does this while parsing, but repeating it at export
    time guarantees that XLSX/CSV always contains clean parameter columns
    even for results loaded from an older run.  Rows marked by the engine are
    left untouched, so ``specs_parser`` is not run twice over the same HTML.
    """

    runs = resolve_plugins(campaign, registry=registry)
    prepared: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = dict(row)
        # The crawl engine already ran the plugin chain over this row.  Running
        # specs_parser again would re-parse the same HTML and duplicate work.
        if not data.get(PLUGINS_APPLIED_KEY):
            data = apply_plugins(data, campaign, runs=runs)
        prepared.append(data)
    return prepared


def result_columns(
    campaign: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    registry=None,
) -> list[str]:
    """Field columns first, then dynamically discovered spec columns.

    Priority spec columns (``plugins.priority_columns``) are placed directly
    after the static fields and before the remaining spec columns.
    """

    rows = list(rows)
    split_enabled, source_field, plugins = _split_specs_config(campaign, registry)
    static_columns: list[str] = []
    for field in campaign.get("fields", []):
        if isinstance(field, dict):
            name = str(field.get("name") or "").strip()
            if split_enabled and name == source_field:
                # The raw HTML specs cell is replaced by separate columns.
                continue
            if name and name not in static_columns:
                static_columns.append(name)
    columns = list(static_columns)
    for row in rows:
        for key in row:
            if _is_internal(key) or key in columns:
                continue
            if split_enabled and key == source_field:
                continue
            columns.append(key)
    if split_enabled:
        static_set = set(static_columns)
        dynamic_columns = [column for column in columns if column not in static_set]
        columns = static_columns + order_dynamic_columns(
            dynamic_columns, plugins.get("priority_columns")
        )
    has_problems = any(
        str(row.get("__status__", "")).strip() not in {"", "OK"}
        for row in rows
        if isinstance(row, dict)
    )
    if has_problems and "Статус" not in columns:
        columns.append("Статус")
    return columns


def resolve_export_path(
    campaign: dict[str, Any],
    base_dir: str | os.PathLike[str] | None = None,
    override: str | os.PathLike[str] | None = None,
) -> tuple[Path, str]:
    export = campaign.get("export") if isinstance(campaign.get("export"), dict) else {}
    export_format = str(export.get("format") or "xlsx").strip().lower()
    if export_format not in {"xlsx", "csv"}:
        export_format = "xlsx"
    raw_path = Path(str(override or export.get("file_path") or "exports/result.xlsx")).expanduser()
    if not raw_path.is_absolute():
        root = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd()
        raw_path = root / raw_path
    suffix = f".{export_format}"
    if raw_path.suffix.lower() != suffix:
        raw_path = raw_path.with_suffix(suffix)
    return raw_path, export_format


def _stringify(value: Any) -> Any:
    """Flatten multi-value fields into a readable cell value."""

    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return str(value)
    return value


def _safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, (int, float)):
        return value
    value = _stringify(value)
    text = str(value)
    # Excel refuses control characters and cells longer than 32767 chars.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > 32767:
        text = text[:32767]
    return text


def _sheet_title(name: str) -> str:
    title = re.sub(r"[\[\]:*?/\\]", " ", name).strip() or "Результаты"
    return title[:31]


def _write_xlsx(columns: list[str], rows: list[dict[str, Any]], path: Path, sheet_name: str) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _sheet_title(sheet_name)

    for column_index, column in enumerate(columns, start=1):
        sheet.cell(row=1, column=column_index, value=_safe_cell(column))

    for row_index, row in enumerate(rows, start=2):
        for column_index, column in enumerate(columns, start=1):
            if column == "Статус":
                value = row.get("__status__", "")
            else:
                value = row.get(column, "")
            sheet.cell(row=row_index, column=column_index, value=_safe_cell(value))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=".export-", suffix=".xlsx", delete=False
        ) as handle:
            temp_name = handle.name
        workbook.save(temp_name)
        os.replace(temp_name, path)
        temp_name = None
    except (OSError, IllegalCharacterError, ValueError) as exc:
        raise ExportError(f"Не удалось сохранить XLSX: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _write_csv(columns: list[str], rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8-sig",
            newline="",
            dir=path.parent,
            prefix=".export-",
            suffix=".csv",
            delete=False,
        ) as handle:
            temp_name = handle.name
            writer = csv.writer(handle, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
            writer.writerow(columns)
            for row in rows:
                values = []
                for column in columns:
                    value = row.get("__status__", "") if column == "Статус" else row.get(column, "")
                    value = _stringify(value)
                    values.append("" if value is None else str(value))
                writer.writerow(values)
        os.replace(temp_name, path)
        temp_name = None
    except OSError as exc:
        raise ExportError(f"Не удалось сохранить CSV: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def export_results(
    campaign: dict[str, Any],
    rows: list[dict[str, Any]],
    base_dir: str | os.PathLike[str] | None = None,
    path_override: str | os.PathLike[str] | None = None,
    registry=None,
) -> ExportReport:
    """Write results using the format configured in the campaign."""

    if not rows:
        raise ExportError("Нет данных для экспорта. Сначала выполните парсинг.")

    rows = expand_spec_columns(campaign, rows, registry=registry)
    columns = result_columns(campaign, rows, registry=registry)
    if not columns or columns == ["Статус"]:
        raise ExportError("В результатах нет колонок для экспорта.")

    path, export_format = resolve_export_path(campaign, base_dir, path_override)
    prepared = [
        row
        for row in rows
        if any(str(row.get(column, "")).strip() for column in columns if column != "Статус")
    ]
    if not prepared:
        raise ExportError("Все строки пусты — экспортировать нечего.")

    if export_format == "xlsx":
        _write_xlsx(columns, prepared, path, str(campaign.get("campaign_name") or "Результаты"))
    else:
        _write_csv(columns, prepared, path)
    return ExportReport(
        path=path,
        format=export_format,
        exported_rows=len(prepared),
        skipped_rows=len(rows) - len(prepared),
        columns=len(columns),
    )
