"""Gocator L/R profile plots (pyqtgraph), windowed by ground distance."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from core.alignment import profiles_in_window
from core.formats import GocatorIndex
from . import styles

MAX_PROFILES_DRAWN = 12  # cap per plot so drawing stays fast

class ProfilePairView(QWidget):
    """Two stacked plots: Gocator Left and Right profiles near a ground distance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self):
        self.title_label = QLabel("Gocator profiles")
        self.left_plot = pg.PlotWidget(title="Left")
        self.right_plot = pg.PlotWidget(title="Right")
        for plot in (self.left_plot, self.right_plot):
            plot.setLabel("bottom", "x", units="mm")
            plot.setLabel("left", "z", units="mm")
            plot.showGrid(x=True, y=True, alpha=0.2)

    # --- Layout ---
    def _create_layouts(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(4, 4, 4, 4)
        self.main_layout.addWidget(self.title_label)
        self.main_layout.addWidget(self.left_plot)
        self.main_layout.addWidget(self.right_plot)

    # --- Signals & Slots ---
    def _connect_signals(self):
        pass

    # --- Styling ---
    def _apply_styles(self):
        self.title_label.setStyleSheet(styles.GROUP_TITLE)
        for plot in (self.left_plot, self.right_plot):
            plot.setBackground(styles.DARK_BG_ALT)

    # --- Initial State ---
    def _load_initial_state(self):
        self._sources: dict[str, tuple[GocatorIndex | None, np.ndarray | None]] = {
            "L": (None, None), "R": (None, None),
        }
        # What is actually on the plots right now, so the image viewer can draw
        # the SAME scans as overlay lines: side -> (rows drawn, nearest row).
        self.shown: dict[str, tuple[np.ndarray, int] | None] = {"L": None, "R": None}

    # --- Business Logic ---
    def set_sources(self, gocator_l: GocatorIndex | None, dist_l: np.ndarray | None,
                    gocator_r: GocatorIndex | None, dist_r: np.ndarray | None):
        self._sources = {"L": (gocator_l, dist_l), "R": (gocator_r, dist_r)}

    def show_window(self, center_m: float, half_span_m: float, cursor_offset: int = 0):
        """Draw the profiles covering this patch of ground.

        `cursor_offset` steps the highlighted scan away from the one nearest the
        centre, in WHOLE PROFILES. The highlighted scan is always drawn, even
        when the subsample would have skipped it — it is the one the user is
        pointing at, so it must be on the plot AND on the image.
        """
        for side, plot in (("L", self.left_plot), ("R", self.right_plot)):
            gocator, dist = self._sources[side]
            plot.clear()
            self.shown[side] = None
            if gocator is None or dist is None or not np.isfinite(center_m):
                continue
            rows = profiles_in_window(dist, center_m, half_span_m)
            if len(rows) == 0:
                continue
            centre_row = int(rows[int(np.argmin(np.abs(dist[rows] - center_m)))])
            cursor = int(np.clip(centre_row + cursor_offset, 0, len(dist) - 1))
            if len(rows) > MAX_PROFILES_DRAWN:
                keep = np.linspace(0, len(rows) - 1, MAX_PROFILES_DRAWN).astype(int)
                rows = rows[keep]
            if cursor not in rows:                      # the cursor is never hidden
                rows = np.sort(np.append(rows, cursor))
            nearest = cursor
            self.shown[side] = (rows, int(nearest))
            for row in rows:
                profile = gocator.read_profile(int(row))
                if profile.size == 0:
                    continue
                is_nearest = row == nearest
                pen = pg.mkPen(
                    styles.ACCENT_ORANGE if is_nearest else styles.ACCENT_BLUE,
                    width=2 if is_nearest else 1,
                )
                curve = plot.plot(profile[:, 0], profile[:, 1], pen=pen)
                if not is_nearest:
                    curve.setOpacity(0.35)
            plot.setTitle(
                f"{'Left' if side == 'L' else 'Right'} — cursor on SCAN {nearest} "
                f"at {dist[nearest]:.3f} m  ({len(rows)} profiles shown)"
            )
