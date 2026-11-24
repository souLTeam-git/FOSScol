"""Qt thread wrapper around :mod:`core.engine`."""

from __future__ import annotations

import copy
import traceback
from typing import Any

from PySide6.QtCore import QThread, Signal

from core.engine import CrawlEngine, CrawlSummary


class CampaignRunner(QThread):
    """Runs one campaign and re-emits engine callbacks as Qt signals."""

    result_ready = Signal(dict)
    log_message = Signal(str, str)
    progress_changed = Signal(int, int)
    pause_changed = Signal(bool, str)
    run_finished = Signal(object)
    run_failed = Signal(str)

    def __init__(self, campaign: dict[str, Any], parent: Any = None) -> None:
        super().__init__(parent)
        self._campaign = copy.deepcopy(campaign)
        self._engine: CrawlEngine | None = None
        self._stop_requested = False

    def run(self) -> None:  # noqa: D401 - Qt entry point
        try:
            engine = CrawlEngine(
                self._campaign,
                on_log=lambda level, message: self.log_message.emit(level, message),
                on_result=lambda row: self.result_ready.emit(row),
                on_progress=lambda done, total: self.progress_changed.emit(done, total),
                on_pause=lambda paused, reason: self.pause_changed.emit(paused, reason),
            )
            self._engine = engine
            if self._stop_requested:
                engine.stop()
            summary: CrawlSummary = engine.run()
            self.run_finished.emit(summary)
        except Exception as exc:  # pragma: no cover - defensive boundary
            details = traceback.format_exc()
            self.run_failed.emit(f"{exc}\n\n{details}")
        finally:
            self._engine = None

    def stop(self) -> None:
        self._stop_requested = True
        engine = self._engine
        if engine is not None:
            engine.stop()

    def pause(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.pause()

    def resume(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.resume()

    def toggle_pause(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.toggle_pause()
