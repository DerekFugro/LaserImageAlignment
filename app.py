"""LaserImageAlignment — entry point.

Aligns Pave (rear) camera images with Gocator laser profiles on a shared
PTP-time / distance axis and exports per-image geolocations.
Run via launch.bat (or: python app.py).
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LaserImageAlignment")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
