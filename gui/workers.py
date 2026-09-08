"""Background workers — all file I/O and processing stays off the GUI thread."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    finished = Signal(object)   # result of fn()
    error = Signal(str)


class Worker(QRunnable):
    """Run fn(*args, **kwargs) in the global QThreadPool."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        else:
            self.signals.finished.emit(result)
