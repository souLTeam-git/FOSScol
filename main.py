#!/usr/bin/env python3
"""FOSScol — cross-platform parser for Windows and Linux.

Run:
    .venv/bin/python main.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ui.icons import app_icon  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import apply_theme  # noqa: E402


def _exception_hook(exc_type, exc_value, exc_traceback) -> None:
    """Log uncaught exceptions and show a readable message instead of dying silently."""

    text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print(text, file=sys.stderr)
    app = QApplication.instance()
    if app is not None:
        box = QMessageBox()
        box.setWindowTitle("Необработанная ошибка")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("Произошла непредвиденная ошибка.")
        box.setInformativeText(str(exc_value))
        box.setDetailedText(text)
        box.exec()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("FOSScol")
    app.setApplicationDisplayName("FOSScol")
    app.setOrganizationName("FOSScol")
    app.setOrganizationDomain("fosscol.local")
    app.setWindowIcon(app_icon())
    apply_theme(app)

    window = MainWindow(PROJECT_ROOT)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.excepthook = _exception_hook
    raise SystemExit(main())
