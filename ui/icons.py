"""Application branding helpers (name, icon, logo)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
ICON_ICO = ASSETS_DIR / "logo_fosscol.ico"
ICON_PNG = ASSETS_DIR / "logo_fosscol.png"


def icon_path() -> Path:
    """Prefer ICO on Windows and PNG on Linux/other platforms."""

    preferred = ICON_ICO if sys.platform.startswith("win") else ICON_PNG
    fallback = ICON_PNG if preferred == ICON_ICO else ICON_ICO
    if preferred.exists():
        return preferred
    if fallback.exists():
        return fallback
    return preferred


def app_icon() -> QIcon:
    path = icon_path()
    return QIcon(str(path)) if path.exists() else QIcon()


def logo_pixmap() -> QPixmap:
    if ICON_PNG.exists():
        return QPixmap(str(ICON_PNG))
    if ICON_ICO.exists():
        return QPixmap(str(ICON_ICO))
    return QPixmap()
