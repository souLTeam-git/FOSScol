"""Ordering helpers for dynamically discovered specification columns.

The split-specifications plugin turns the specs HTML block into separate
columns whose order follows the source table.  Some campaigns want a few
well-known parameters (brand, type) to always appear first, right after the
static fields, regardless of where they are in the table.  ``priority_columns``
stores that preference as a list of names or synonym groups:

    [["Бренд", "Виробник", "Производитель"], ["Тип", "Вид"]]

Matching is case-insensitive, ignores repeated whitespace and surrounding
quotes.  Columns that are not mentioned keep their original order.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_QUOTES = "\"'«»“”„`"
_SPACE_RE = re.compile(r"\s+")
_SPLIT_RE = re.compile(r"[,;]")


def _clean_name(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\xa0", " ").replace("\u2009", " ").replace("\u202f", " ")
    text = _SPACE_RE.sub(" ", text).strip()
    return text.strip(_QUOTES + ":").strip()


def normalize_column_key(name: Any) -> str:
    """Normalise a column name for case/spacing-insensitive comparison."""

    return _clean_name(name).casefold()


def parse_priority_columns(value: Any) -> list[list[str]]:
    """Coerce ``priority_columns`` into a list of synonym groups.

    Accepts a list of strings, a list of lists, or a comma/semicolon separated
    string (the format used by the settings field).
    """

    if value is None:
        return []
    if isinstance(value, str):
        return [[name] for name in parse_priority_text(value)]
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    groups: list[list[str]] = []
    for item in value:
        if isinstance(item, (list, tuple, set)):
            names = []
            for name in item:
                cleaned = _clean_name(name)
                if cleaned:
                    names.append(cleaned)
        else:
            name = _clean_name(item)
            names = [name] if name else []
        if names:
            groups.append(names)
    return groups


def parse_priority_text(text: str) -> list[str]:
    """Split the settings field text into individual priority names."""

    if not text:
        return []
    names: list[str] = []
    for part in _SPLIT_RE.split(str(text)):
        name = _clean_name(part)
        if name:
            names.append(name)
    return names


def flatten_priority_columns(value: Any) -> list[str]:
    """Return every configured name in display order."""

    return [name for group in parse_priority_columns(value) for name in group]


def build_priority_groups(value: Any) -> list[set[str]]:
    """Return normalised synonym groups, dropping empty/duplicate groups."""

    groups: list[set[str]] = []
    seen: set[frozenset[str]] = set()
    for group in parse_priority_columns(value):
        keys = {normalize_column_key(name) for name in group}
        keys.discard("")
        if not keys:
            continue
        signature = frozenset(keys)
        if signature in seen:
            continue
        seen.add(signature)
        groups.append(keys)
    return groups


def order_dynamic_columns(names: Iterable[str], priority: Any) -> list[str]:
    """Put priority columns first, keeping the remaining order unchanged."""

    ordered_names = [str(name) for name in names]
    groups = build_priority_groups(priority)
    if not groups or not ordered_names:
        return ordered_names

    keys = [normalize_column_key(name) for name in ordered_names]
    used: set[int] = set()
    ordered: list[str] = []
    for group in groups:
        for index, key in enumerate(keys):
            if index in used or key not in group:
                continue
            used.add(index)
            ordered.append(ordered_names[index])
    for index, name in enumerate(ordered_names):
        if index not in used:
            ordered.append(name)
    return ordered
