"""Small shared helpers for plugins.

The module is plugin agnostic: it contains no concrete plugin ids, classes or
settings.  Concrete plugins, including the bundled ``specs_parser`` and
``list_separator``, live in the project ``plugins`` directory.
"""

from __future__ import annotations

from typing import Any


def decode_separator(value: Any) -> str:
    """Turn the ``\\n`` / ``\\t`` escapes typed in the UI into real characters."""

    text = "" if value is None else str(value)
    return text.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")


def parse_target_fields(value: Any) -> list[str]:
    """Coerce a comma separated or list value to a list of unique names."""

    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        name = str(item or "").strip()
        if name and name not in result:
            result.append(name)
    return result
