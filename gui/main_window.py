"""Main window — wires panels, viewer, plots, navigation, and export."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QLabel, QMainWindow, QMessageBox, QSlider, QSplitter,
    QToolBar, QVBoxLayout, QWidget,
)

from core import discovery as disc
from core.batch import preflight, process_collection, write_reports
from core.calibration import (
    Undistorter, camera_ground_center_m, ground_to_image_row, lens_height_m,
)
from core import config
from core.pipeline import RunResult, export_csv, process_run
from . import styles
from .image_view import ImageView
from .panels import QCPanel, RunListPanel
from .profile_view import ProfilePairView
from .workers import Worker


MARGIN = 8
LEFT_PANEL_WIDTH = 360
HALF_SPAN_M = 0.575  # fallback only; derived from the calibration when present
                     # (see MainWindow._along_track_half_span_m)
SLIDER_TICKS = 1000
BATCH_PROGRESS_MS = 250  # how often the batch status line refreshes


class ImageWindow(QMainWindow):
    """Stand-alone window hosting the (reparented) image stack - the secondary
    camera above the primary - with its own toolbar sharing the MainWindow's
    actions so shortcuts keep working.

    It holds whatever widget it is handed and hands the same one back, so the
    popped-out window shows the cameras in the same order as the docked one.
    """

    from PySide6.QtCore import Signal as _Signal

    closed = _Signal()

    def __init__(self, image_stack: QWidget, shared_actions: list[QAction], parent=None):
        super().__init__(parent)
        self._image_view = image_stack
        self._shared_actions = shared_actions
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self):
        self.image_toolbar = QToolBar("Image")
        self.image_toolbar.setMovable(False)

    # --- Layout ---
    def _create_layouts(self):
        self.addToolBar(self.image_toolbar)
        for action in self._shared_actions:
            self.image_toolbar.addAction(action)
            self.addAction(action)  # keyboard shortcuts work in this window too
        self.setCentralWidget(self._image_view)

    # --- Signals & Slots ---
    def _connect_signals(self):
        pass

    # --- Styling ---
    def _apply_styles(self):
        self.setStyleSheet(styles.MAIN_WINDOW)
        self.setWindowTitle("LaserImageAlignment — Image")

    # --- Initial State ---
    def _load_initial_state(self):
        pass

    # --- Business Logic ---
    def take_image_view(self) -> QWidget:
        """Detach the stack so closing/deleting this window can't destroy it."""
        return self.takeCentralWidget()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class MainWindow(QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self):
        self.toolbar = QToolBar("Main")
        self.toolbar.setMovable(False)
        self.open_action = QAction("Open Run Root…", self)
        self.prev_action = QAction("◀ Prev", self)
        self.prev_action.setShortcut(QKeySequence(Qt.Key_Left))
        self.next_action = QAction("Next ▶", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key_Right))
        self.undistort_action = QAction("Undistort", self)
        self.undistort_action.setCheckable(True)
        self.batch_action = QAction("Process All (write GPS)", self)
        self.batch_action.setToolTip(
            "Batch-process every run in this root: writes corrected GPS into the "
            "JPEGs and Gocator CSVs, exports the tables, and reports failures")
        self.popout_action = QAction("Pop Out Image", self)
        self.popout_action.setCheckable(True)
        self.popout_action.setToolTip("Show the image in its own (maximized) window")
        self.scan_back10_action = QAction("⏪ -10", self)
        self.scan_back10_action.setShortcut(QKeySequence("Ctrl+Left"))
        self.scan_back1_action = QAction("◀ -1", self)
        self.scan_back1_action.setShortcut(QKeySequence("Shift+Left"))
        self.scan_fwd1_action = QAction("+1 ▶", self)
        self.scan_fwd1_action.setShortcut(QKeySequence("Shift+Right"))
        self.scan_fwd10_action = QAction("+10 ⏩", self)
        self.scan_fwd10_action.setShortcut(QKeySequence("Ctrl+Right"))
        self.scan_reset_action = QAction("Cursor 0", self)
        for a, tip in ((self.scan_back10_action, "Ctrl+Left"), (self.scan_back1_action, "Shift+Left"),
                       (self.scan_fwd1_action, "Shift+Right"), (self.scan_fwd10_action, "Ctrl+Right"),
                       (self.scan_reset_action, "clear the nudge")):
            a.setToolTip("Step the scan CURSOR ({}). The orange curve on the plot "
                         "and the orange line on the image are the same scan, so "
                         "stepping it is how you check that the laser and the "
                         "picture agree about where a feature is."
                         .format(tip))
        self.export_action = QAction("Export CSV", self)
        self.distance_slider = QSlider(Qt.Horizontal)
        self.distance_slider.setRange(0, SLIDER_TICKS)
        self.readout_label = QLabel("no run loaded")
        self.image_state_label = QLabel("RAW")

        self.batch_progress_timer = QTimer(self)
        self.batch_progress_timer.setInterval(BATCH_PROGRESS_MS)

        self.run_panel = RunListPanel()
        self.qc_panel = QCPanel()
        self.image_view = ImageView()
        # The secondary camera (ROW), shown ABOVE the primary. A plain
        # picture viewer: no measure, no bar, no undistort. ROW's oblique
        # ground-plane projection is not implemented, so anything turning
        # its pixels into millimetres would be inventing a number.
        # Looking at it is the point.
        self.secondary_view = ImageView()
        self.secondary_label = QLabel("")
        self.secondary_pane = QWidget()
        # One stack, so the pop-out carries BOTH cameras in the same
        # order rather than only the primary.
        self.image_stack = QSplitter(Qt.Vertical)
        self.profile_view = ProfilePairView()

        self.left_splitter = QSplitter(Qt.Vertical)
        self.main_splitter = QSplitter(Qt.Horizontal)

    # --- Layout ---
    def _create_layouts(self):
        self.addToolBar(self.toolbar)
        self.toolbar.addAction(self.open_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.prev_action)
        self.toolbar.addAction(self.next_action)
        self.toolbar.addWidget(self.distance_slider)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.undistort_action)
        self.toolbar.addSeparator()
        for a in (self.scan_back10_action, self.scan_back1_action, self.scan_reset_action,
                  self.scan_fwd1_action, self.scan_fwd10_action):
            self.toolbar.addAction(a)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.popout_action)
        self.toolbar.addAction(self.export_action)
        self.toolbar.addAction(self.batch_action)

        self.left_splitter.addWidget(self.run_panel)
        self.left_splitter.addWidget(self.qc_panel)
        self.left_splitter.setStretchFactor(1, 1)

        self.main_splitter.addWidget(self.left_splitter)
        sec = QVBoxLayout(self.secondary_pane)
        sec.setContentsMargins(0, 0, 0, 0)
        sec.setSpacing(2)
        sec.addWidget(self.secondary_label)
        sec.addWidget(self.secondary_view, 1)
        self.image_stack.addWidget(self.secondary_pane)
        self.image_stack.addWidget(self.image_view)
        self.secondary_pane.setVisible(False)   # until a run says otherwise
        self.image_stack.setStretchFactor(0, 1)
        self.image_stack.setStretchFactor(1, 1)
        self.main_splitter.addWidget(self.image_stack)
        self.main_splitter.addWidget(self.profile_view)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setStretchFactor(2, 1)
        self.setCentralWidget(self.main_splitter)
        self.statusBar().addWidget(self.readout_label, 1)
        self.statusBar().addPermanentWidget(self.image_state_label)

    # --- Signals & Slots ---
    def _connect_signals(self):
        self.open_action.triggered.connect(self._on_open_action_triggered)
        self.prev_action.triggered.connect(self._on_prev_action_triggered)
        self.next_action.triggered.connect(self._on_next_action_triggered)
        self.undistort_action.toggled.connect(self._on_undistort_action_toggled)
        self.popout_action.toggled.connect(self._on_popout_action_toggled)
        self.batch_action.triggered.connect(self._on_batch_action_triggered)
        self.export_action.triggered.connect(self._on_export_action_triggered)
        self.distance_slider.valueChanged.connect(self._on_distance_slider_value_changed)
        self.run_panel.run_selected.connect(self._on_run_selected)
        self.qc_panel.locate_requested.connect(self._on_locate_requested)
        self.batch_progress_timer.timeout.connect(self._on_batch_progress_timer_timeout)
        self.scan_back10_action.triggered.connect(lambda: self._nudge_scans(-10))
        self.scan_back1_action.triggered.connect(lambda: self._nudge_scans(-1))
        self.scan_fwd1_action.triggered.connect(lambda: self._nudge_scans(+1))
        self.scan_fwd10_action.triggered.connect(lambda: self._nudge_scans(+10))
        self.scan_reset_action.triggered.connect(lambda: self._nudge_scans(0, absolute=True))

    # --- Styling ---
    def _apply_styles(self):
        self.setStyleSheet(styles.MAIN_WINDOW)
        self.setWindowTitle("LaserImageAlignment")

    # --- Initial State ---
    def _load_initial_state(self):
        self.settings = QSettings("Derek", "LaserImageAlignment")
        self.thread_pool = QThreadPool.globalInstance()
        self.overrides = disc.OverrideStore()
        # From lia.ini beside the app (core.config), NOT from QSettings. It
        # used to come from a registry value with no UI to set it, so on any
        # machine but the original the GUI looked in a folder that does not
        # exist, every run said "missing lever_arms", and there was no way to
        # tell it otherwise. A file the user can open is the fix.
        self.calibrations_dir = config.calibrations_dir()
        self.run_root: Path | None = None
        self.runs: list[disc.RunPaths] = []
        self.result: RunResult | None = None
        self.current_image = 0
        self._undistorter: Undistorter | None = None
        self._slider_updating = False
        self._batch_stage: tuple | None = None   # (stage, label, i, total) from the worker
        self._cam_centre_m = float("nan")        # ground under the lens, current image
        self._gocator_extent_m: tuple | None = None   # (start_m, end_m) of laser data
        self._scan_cursor = 0                         # cursor offset, in whole profiles
        self._undistort_error = ""                    # why raw, when we wanted undistorted
        self.image_window: ImageWindow | None = None
        self._set_run_loaded(False)
        # Undistort is ON by default: the ground/pixel model is only exact on the
        # undistorted image, so raw should be the deliberate exception (Derek,
        # 2026-08-20). _ensure_undistorter() drops back to raw quietly if a run
        # has no calibration.
        self.undistort_action.setChecked(True)
        self._update_image_state_label(False)
        self.resize(1500, 900)
        self.main_splitter.setSizes([340, 820, 340])  # image gets the most room
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if self.settings.value("image_popped", "false") == "true":
            self.popout_action.setChecked(True)  # triggers the pop-out

    # --- Business Logic ---
    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        if self.image_window is not None:
            self.settings.setValue("image_window_geometry", self.image_window.saveGeometry())
            self.image_window.closed.disconnect(self._on_image_window_closed)
            self.image_window.close()
        super().closeEvent(event)

    def _set_run_loaded(self, loaded: bool):
        for action in (self.prev_action, self.next_action, self.undistort_action,
                       self.scan_back1_action, self.scan_fwd1_action,
                       self.scan_fwd10_action, self.scan_reset_action):
            action.setEnabled(loaded)
        self.export_action.setEnabled(loaded and self.result is not None
                                      and not self.result.export_blocked)
        self.distance_slider.setEnabled(loaded)
        if loaded and self.result is not None and self.result.export_blocked:
            failed = ", ".join(c.check_id for c in self.result.report.failed)
            self.export_action.setToolTip(f"Export blocked by failed QC: {failed}")
        else:
            self.export_action.setToolTip("Export the image→location table")

    # open root --------------------------------------------------------------
    def _on_open_action_triggered(self):
        # last folder opened, else lia.ini's `collections`, else home - no
        # hardcoded drive letter, which is meaningless off the original machine
        start = self.settings.value("last_root") or config.collections_dir() \
            or Path.home()
        chosen = QFileDialog.getExistingDirectory(self, "Open run root", str(start))
        if not chosen:
            return
        self.settings.setValue("last_root", chosen)
        self.open_root(Path(chosen))

    def open_root(self, root: Path):
        self.run_root = root
        self.readout_label.setText(f"discovering runs in {root.name}…")
        worker = Worker(disc.discover_runs, root,
                        calibrations_dir=self.calibrations_dir, overrides=self.overrides)
        worker.signals.finished.connect(self._on_runs_discovered)
        worker.signals.error.connect(self._on_worker_error)
        self.thread_pool.start(worker)

    def _on_runs_discovered(self, runs: list):
        self.runs = runs
        self.run_panel.populate_runs(runs)
        self.readout_label.setText(
            f"{len(runs)} run(s) found — select one" if runs else
            "no runs found in this folder (expected Images/, GoCatorData/, SBGData/)")

    def _on_run_selected(self, run_id: str):
        run = next((r for r in self.runs if r.run_id == run_id), None)
        if run is None:
            return
        self.readout_label.setText(f"processing {run_id} (QC + alignment)…")
        self.result = None
        self._set_run_loaded(False)
        worker = Worker(process_run, run)
        worker.signals.finished.connect(self._on_run_processed)
        worker.signals.error.connect(self._on_worker_error)
        self.thread_pool.start(worker)

    def _on_run_processed(self, result):
        self.result = result
        # (side, row) keys mean nothing once the Gocator files change — a stale
        # cache would report the PREVIOUS run's bar on this run's scans.
        self.qc_panel.populate_report(result.report)
        parsed = result.parsed
        self.profile_view.set_sources(
            parsed.gocator_l, result.gocator_l_dist_m,
            parsed.gocator_r, result.gocator_r_dist_m,
        )
        self._undistorter = None
        loaded = parsed.images is not None and len(parsed.images) > 0
        self._set_run_loaded(loaded)
        if loaded:
            self.current_image = self._first_matched_index()
            self._show_current_image()
        summary = []
        if result.ptp_l is not None:
            summary.append(f"PTP {result.ptp_l.offset_s:.2f} s (r={result.ptp_l.r:.3f})")
        if result.match is not None:
            summary.append(f"match k={result.match.shift_k:+d}, "
                           f"{result.match.n_matched}/{len(parsed.images or [])} images")
        n_fail = len(result.report.failed)
        summary.append(f"{n_fail} QC failure(s)" if n_fail else "QC clean")
        self.readout_label.setText(f"{result.run.run_id}: " + "; ".join(summary))

    def _first_matched_index(self) -> int:
        if self.result is not None and self.result.match is not None:
            matched = np.where(self.result.match.trigger_for_image >= 0)[0]
            if len(matched):
                return int(matched[0])
        return 0

    # locate flow ------------------------------------------------------------
    def _on_locate_requested(self, key: str, chosen_path: str):
        if self.result is None or self.run_root is None:
            return
        run = self.result.run
        self.overrides.set(run.root, run.run_id, key, Path(chosen_path))
        run.paths[key] = Path(chosen_path)
        self._on_run_selected(run.run_id)  # re-run QC + alignment automatically

    # navigation -------------------------------------------------------------
    def _on_prev_action_triggered(self):
        self._step_image(-1)

    def _on_next_action_triggered(self):
        self._step_image(+1)

    def _step_image(self, delta: int):
        if self.result is None or self.result.parsed.images is None:
            return
        n = len(self.result.parsed.images)
        target = int(np.clip(self.current_image + delta, 0, n - 1))
        self.current_image = target
        self._show_current_image()

    def _on_distance_slider_value_changed(self, value: int):
        if self._slider_updating or self.result is None:
            return
        images = self.result.parsed.images
        if images is None or len(images) < 2:
            return
        target = int(round(value / SLIDER_TICKS * (len(images) - 1)))
        self.current_image = target
        self._show_current_image()

    def _show_current_image(self):
        result = self.result
        images = result.parsed.images
        j = self.current_image
        path = images.path(j)
        if self.undistort_action.isChecked() and self._ensure_undistorter(interactive=False):
            import cv2

            bgr = cv2.imread(str(path))
            if bgr is None:
                QMessageBox.warning(self, "Image", f"Could not read {path.name}")
                return
            try:
                self.image_view.show_array(self._undistorter.undistort(bgr))
                self._undistort_error = ""
                self._update_image_state_label(True)
            except ValueError as exc:
                # Image size does not match the calibration. Now that undistort
                # is genuinely on by default this fires on EVERY image, so a
                # modal here would make the app unusable. Fall back to raw and
                # let the badge carry the reason.
                self._undistort_error = str(exc)
                self.image_view.show_file(path)
                self._update_image_state_label(False)
        else:
            self.image_view.show_file(path)
            self._update_image_state_label(False)
        self._show_secondary_image(j)

        al = result.alignment
        parts = [f"{path.name}  [{j + 1}/{len(images)}]"]
        center = float("nan")
        if al is not None:
            i = int(al.match.trigger_for_image[j])
            if i >= 0:
                center = float(al.dmi_dist_m[j])
                parts.append(f"trigger {i}  dist {center:.2f} m  "
                             f"(cam ground {self._cam_ground_m(center):.2f} m)")
                if np.isfinite(al.utc_s[j]):
                    from datetime import datetime, timezone
                    utc = datetime.fromtimestamp(al.utc_s[j], tz=timezone.utc)
                    parts.append(f"UTC {utc.strftime('%H:%M:%S.%f')[:-3]}")
                if np.isfinite(al.lat_deg[j]):
                    parts.append(f"lat {al.lat_deg[j]:.7f}  lon {al.lon_deg[j]:.7f}")
                else:
                    parts.append("no location (outside nav window)")
            else:
                parts.append("UNMATCHED (pre-collection trigger)")
        # Profiles first — the plots decide WHICH scans are shown, then we draw
        # those same scans onto the image so the two views agree by construction.
        #
        # The nudge does NOT move the plot window. If it did, the marks and the
        # selection would slide together by exactly one scan spacing and the
        # pattern would look unchanged — motion you cannot see is no use for
        # lining anything up. So: the plot holds still, the marks slide.
        cam_centre = self._cam_ground_m(center)
        self.profile_view.show_window(cam_centre, self._along_track_half_span_m(),
                                      cursor_offset=self._scan_cursor)
        lines = self._gocator_extent_lines(center)
        lines.extend(self._scan_overlay_lines(cam_centre))
        self.image_view.set_overlay_lines(lines)
        cur = self._cursor_scan()
        if cur is not None:
            row, ground = cur
            parts.append(f"cursor SCAN {row} @ {ground:.3f} m")
        self.readout_label.setText("   |   ".join(parts))

        self._slider_updating = True
        n = len(images)
        self.distance_slider.setValue(int(j / max(n - 1, 1) * SLIDER_TICKS))
        self._slider_updating = False

    # image pop-out ----------------------------------------------------------
    def _on_popout_action_toggled(self, checked: bool):
        self.settings.setValue("image_popped", "true" if checked else "false")
        if checked:
            self._pop_out_image()
        else:
            self._dock_image()

    def _pop_out_image(self):
        if self.image_window is not None:
            return
        shared = [self.prev_action, self.next_action, self.undistort_action,
                  self.scan_back10_action, self.scan_back1_action,
                  self.scan_reset_action, self.scan_fwd1_action, self.scan_fwd10_action]
        self.image_window = ImageWindow(self.image_stack, shared)
        self.image_window.closed.connect(self._on_image_window_closed)
        geometry = self.settings.value("image_window_geometry")
        if geometry is not None:
            self.image_window.restoreGeometry(geometry)
            self.image_window.show()
        else:
            self.image_window.showMaximized()
        self.image_view.fit()
        self.secondary_view.fit()

    def _dock_image(self):
        if self.image_window is None:
            return
        self.settings.setValue("image_window_geometry", self.image_window.saveGeometry())
        window, self.image_window = self.image_window, None
        view = window.take_image_view()
        window.closed.disconnect(self._on_image_window_closed)
        window.close()
        window.deleteLater()
        self.main_splitter.insertWidget(1, view)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([340, 820, 340])
        view.show()
        # `view` is the STACK now, not a single ImageView - fit each camera.
        self.image_view.fit()
        self.secondary_view.fit()

    def _on_image_window_closed(self):
        if self.popout_action.isChecked():
            self.popout_action.setChecked(False)  # docks the view back

    def _lens_height_m(self) -> float:
        """Rear lens height above the pavement, from the lever-arm file in the
        run's own AllCalibrations folder. Falls back to the built-in when the
        file is missing, exactly as the arms do."""
        res = self.result
        return lens_height_m(res.run.calibrations_dir if res else None)

    def _cam_ground_m(self, trigger_dist_m: float) -> float:
        """Ground under the rear lens, using the arm from the RUN'S lever-arm
        file — the same number the written EXIF uses.

        Every call in this window went through camera_ground_center_m() with no
        calibrations_dir, so the viewer used the built-in constant while the
        batch used the file. Edit the file and the plot would not move: the one
        place the arm can be judged by eye was the one place the file did not
        reach (found 2026-09-02)."""
        res = self.result
        al = res.alignment if res else None
        if al is not None and al.arm_m is not None:
            return trigger_dist_m - float(al.arm_m)   # the number the EXIF got
        # no arm for this run (missing file - the QC panel shows the FAIL):
        # draw with the historical constant rather than nothing
        return camera_ground_center_m(
            trigger_dist_m, res.run.calibrations_dir if res else None)

    def _along_track_half_span_m(self) -> float:
        """Radius of the Gocator-profile window, from the calibration when we
        have one so it tracks a lens change; the nominal constant otherwise."""
        cal = self.result.parsed.calibration if self.result else None
        return cal.along_track_half_span_m(self._lens_height_m()) if cal else HALF_SPAN_M

    # scan cursor ------------------------------------------------------------
    def _nudge_scans(self, delta: int, absolute: bool = False):
        """Step the cursor through the laser scans, in whole profiles.

        WHY A CURSOR AND NOT AN OFFSET: the first version shifted the marks and
        the plot window together, so every mark was replaced by its neighbour
        and nothing appeared to move. What is actually needed is a way to POINT
        at the scan showing a feature -- then its position on the image is a
        measurement, not a guess. One scan = 23.98 mm = ~40 px on the image.
        """
        self._scan_cursor = delta if absolute else self._scan_cursor + delta
        self.scan_reset_action.setText(
            f"Cursor {self._scan_cursor:+d}" if self._scan_cursor else "Cursor 0")
        if self.result is not None and self.result.parsed.images is not None:
            self._show_current_image()

    def _cursor_scan(self) -> tuple[int, float] | None:
        """(profile index, its ground position) for the scan under the cursor."""
        res = self.result
        shown = self.profile_view.shown.get("L") if res else None
        dist = res.gocator_l_dist_m if res else None
        if shown is None or dist is None:
            return None
        return int(shown[1]), float(dist[int(shown[1])])

    def _scan_overlay_lines(self, cam_centre_m: float) -> list:
        """The scans on the PLOT, drawn onto the image at their true positions.

        Thin blue for context; thick orange and labelled for the cursor scan.
        Stepping the cursor walks that orange line along the image ~40 px at a
        time, because consecutive scans really are 23.98 mm apart on the ground.
        """
        res = self.result
        if res is None or not np.isfinite(cam_centre_m):
            return []
        cal = res.parsed.calibration
        shown = self.profile_view.shown.get("L")
        dist = res.gocator_l_dist_m
        if cal is None or shown is None or dist is None:
            return []
        rows, cursor = shown
        cy = float(cal.camera_matrix[1, 2])
        mm_px = cal.mm_per_px_y(self._lens_height_m())
        height = float(cal.height)
        out = []
        for r in rows:
            y = ground_to_image_row(float(dist[int(r)]), cam_centre_m, cy, mm_px)
            if int(r) != int(cursor):
                out.append((y, "", styles.ACCENT_BLUE, 1))
                continue
            # The cursor must NEVER just vanish. Step it far enough and its scan
            # leaves the picture; pin it to the edge and say how far off it is,
            # rather than silently dropping the one line being worked with.
            if y < 0:
                out.append((2.0, f"SCAN {int(r)} ^ off image ({-y * mm_px:.0f} mm)",
                            styles.ACCENT_ORANGE, 3))
            elif y > height:
                out.append((height - 2.0,
                            f"SCAN {int(r)} v off image ({(y - height) * mm_px:.0f} mm)",
                            styles.ACCENT_ORANGE, 3))
            else:
                out.append((y, f"SCAN {int(r)}", styles.ACCENT_ORANGE, 3))
        return out

    # calibration bar ---------------------------------------------------------




    # --- the four-corner workflow ---------------------------------------



    # --- save / cancel / delete -------------------------------------------





    # --- the saved list ----------------------------------------------------




    def _gocator_extent_lines(self, trigger_dist_m: float) -> list:
        """Overlay lines marking where Gocator coverage starts/ends on the
        ground shown in the current image (usually visible only on the first
        and last images of a run)."""
        result = self.result
        self._gocator_extent_m = None
        self._cam_centre_m = self._cam_ground_m(trigger_dist_m) \
            if np.isfinite(trigger_dist_m) else float("nan")
        if result is None or not np.isfinite(trigger_dist_m):
            return []
        cal = result.parsed.calibration
        if cal is None:
            return []
        extents = [d for d in (result.gocator_l_dist_m, result.gocator_r_dist_m)
                   if d is not None and len(d)]
        if not extents:
            return []
        start_m = min(float(np.min(d)) for d in extents)
        end_m = max(float(np.max(d)) for d in extents)
        self._gocator_extent_m = (start_m, end_m)
        cam_center = self._cam_ground_m(trigger_dist_m)
        cy = float(cal.camera_matrix[1, 2])
        mm_px = cal.mm_per_px_y(self._lens_height_m())  # rows = along-track -> fy
        lines = []
        for g, label, color in (
            (start_m, f"Gocator data starts ({start_m:.2f} m)", styles.ACCENT_GREEN),
            (end_m, f"Gocator data ends ({end_m:.2f} m)", styles.ACCENT_RED),
        ):
            row = ground_to_image_row(g, cam_center, cy, mm_px)
            lines.append((row, label, color))  # off-image rows skipped by the view
        return lines

    # calibration tools ------------------------------------------------------
    def _ensure_undistorter(self, interactive: bool = True) -> bool:
        """`interactive=False` when we are only honouring the default-on state —
        fall back to raw quietly and let the status badge explain, rather than
        throwing a dialog at the user every time they open a run."""
        if self._undistorter is not None:
            return True
        cal = self.result.parsed.calibration if self.result else None
        if cal is None:
            # Only give up on undistorting once a run is actually open and shown
            # to have no calibration. Before that there is nothing to complain
            # about — and unchecking here silently cancelled the default-on
            # setting during construction, which is why clicks were logged with
            # undistorted=False all morning.
            if self.result is not None:
                if interactive:
                    QMessageBox.warning(self, "Undistort",
                                        "No calibration loaded (see QC panel).")
                self.undistort_action.setChecked(False)
            return False
        self._undistorter = Undistorter(cal)
        return True

    def _on_undistort_action_toggled(self, checked: bool):
        if checked:
            self._ensure_undistorter(interactive=True)   # complain here, not on load
        if self.result is not None and self.result.parsed.images is not None:
            self._show_current_image()
        else:
            self._update_image_state_label(False)

    def _secondary_camera(self) -> str:
        """The camera to show ABOVE the primary, by name.

        Whatever was discovered that is not the viewer's own folder - ROW in
        practice. Named rather than hard-coded so a third camera on the cart
        shows up instead of being silently ignored.
        """
        res = self.result
        if res is None or not res.parsed.cameras:
            return ""
        primary = res.parsed.images
        for name, im in sorted(res.parsed.cameras.items()):
            if im is not primary:
                return name
        return ""

    def _primary_camera(self) -> str:
        res = self.result
        if res is None or not res.parsed.cameras:
            return ""
        for name, im in res.parsed.cameras.items():
            if im is res.parsed.images:
                return name
        return ""

    def _secondary_index(self, name: str, j: int) -> int:
        """Which of that camera's images was taken on the SAME TRIGGER as the
        primary's image j.

        Not j. The two cameras are matched to the trigger train independently,
        so their ordinals only agree while both hold the same number of
        pictures; file one pre-section frame into BeforeCollection on one of
        them and every pair after it is off by one - a ROW frame 0.75 m from
        the Rear one it is meant to sit beside, with nothing on screen saying
        so. The trigger is the thing the two cameras actually share.
        """
        res = self.result
        prim = res.camera_matches.get(self._primary_camera())
        sec = res.camera_matches.get(name)
        if prim is None or sec is None:
            return -1
        if j >= len(prim.trigger_for_image):
            return -1
        i = int(prim.trigger_for_image[j])
        if i < 0:
            return -1
        hits = np.nonzero(sec.trigger_for_image == i)[0]
        return int(hits[0]) if len(hits) else -1

    def _show_secondary_image(self, j: int):
        """The secondary camera's view of the same trigger, above the primary.

        Raw pixels only. ROW has no ground-plane projection in this app, so
        undistort, measure and the bar tool stay on the primary camera, where
        the millimetres they produce mean something.
        """
        name = self._secondary_camera()
        if not name:
            self.secondary_pane.setVisible(False)
            return
        self.secondary_pane.setVisible(True)
        k = self._secondary_index(name, j)
        if k < 0:
            self.secondary_label.setText(f"{name}: no image on this trigger")
            self.secondary_view.clear()
            return
        im = self.result.parsed.cameras[name]
        path = im.path(k)
        self.secondary_label.setText(f"{name}   {path.name}   [{k + 1}/{len(im)}]")
        self.secondary_view.show_file(path)

    def _update_image_state_label(self, undistorted: bool):
        """Say plainly what is on screen. Every mm reading depends on this."""
        if undistorted:
            self.image_state_label.setText("UNDISTORTED")
            self.image_state_label.setStyleSheet(styles.IMAGE_STATE_OK)
            self.image_state_label.setToolTip(
                "Lens distortion removed — measurements and the ground/pixel model are valid.")
        else:
            cal = self.result.parsed.calibration if self.result else None
            if self._undistort_error:
                why = "RAW — calibration does not match this image"
            elif cal is None:
                why = "RAW — no calibration"
            else:
                why = "RAW — measurements unreliable"
            self.image_state_label.setText(why)
            self.image_state_label.setStyleSheet(styles.IMAGE_STATE_RAW)
            self.image_state_label.setToolTip(
                self._undistort_error or
                "Lens distortion is still present. Ground positions and mm readings "
                "will be wrong, worst towards the edges. Turn Undistort on.")



    # ground truth -----------------------------------------------------------









    # export -----------------------------------------------------------------
    def _on_export_action_triggered(self):
        if self.result is None:
            return
        if self.result.export_blocked:
            failed = "\n".join(c.message for c in self.result.report.failed)
            QMessageBox.warning(self, "Export blocked", f"Failed QC checks:\n{failed}")
            return
        default = Path(self.result.run.root) / "Exports" / \
            f"{self.result.run.run_id}_alignment.csv"
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export alignment table", str(default), "CSV files (*.csv)")
        if not chosen:
            return
        try:
            out = export_csv(self.result, Path(chosen))
        except (RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.readout_label.setText(f"exported {out}")

    # batch ------------------------------------------------------------------
    def _on_batch_action_triggered(self):
        if self.run_root is None:
            QMessageBox.information(self, "Batch", "Open a run root first.")
            return
        self._start_preflight()

    def _start_preflight(self):
        """PRE-CHECK, OFF THE GUI THREAD: decide, before writing anything, which
        runs can be done and which cannot. It parses the full postprocessed
        export for every run, so it must never run on the GUI thread."""
        self.batch_action.setEnabled(False)
        self.readout_label.setText("batch pre-check: inspecting every run…")
        worker = Worker(preflight, self.run_root,
                        calibrations_dir=self.calibrations_dir, overrides=self.overrides)
        worker.signals.finished.connect(self._on_preflight_finished)
        worker.signals.error.connect(self._on_batch_error)
        self.thread_pool.start(worker)

    def _on_preflight_finished(self, pf):
        """Show the plan and let the user choose: process, locate, or cancel."""
        if not pf.runs:
            self._cancel_batch("no runs found in this folder")
            QMessageBox.information(self, "Batch", "No runs found in this folder.")
            return
        n_ok = len(pf.ready)
        n_bad = len(pf.not_ready)
        n_img = sum(r.n_images for r in pf.ready)
        n_img_skip = sum(r.n_images for r in pf.not_ready)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if n_bad else QMessageBox.Question)
        box.setWindowTitle("Pre-check — what the batch will do")
        head = (f"{n_ok} run(s) ready — {n_img} image(s) will be geotagged "
                "in place, Gocator CSVs updated, tables exported.\n")
        if not pf.exif_ok:
            head = ("IMAGES CANNOT BE GEOTAGGED\n" + pf.exif_note +
                    "\n\nGocator CSVs and the alignment tables can still be "
                    "written.\n\n" + head)
        if n_bad:
            head += (f"\n{n_bad} run(s) CANNOT be processed — "
                     f"{n_img_skip} image(s) and their Gocator data will get "
                     "NO GPS and will be left untouched:\n")
            for r in pf.not_ready[:6]:
                head += f"\n  • {r.run_id}: {r.reason}\n      skips {r.files_skipped_text()}"
            if n_bad > 6:
                head += f"\n  … and {n_bad - 6} more"
        # The batch always renames images to their corrected distance into
        # the section (core.rename), so the pre-check has to say so — it is a
        # change to deliverable filenames. Reversible: a rename_manifest.csv
        # is written beside the images and core.rename.undo_renames reads it
        # back. Without the ACS Daily file there is no section start to
        # measure from, and names are left alone.
        from core.daily import find_daily_file
        if find_daily_file(self.run_root) is None:
            head += ("\n\nImage filenames will be LEFT AS THEY ARE — this "
                     "collection has no Daily_ARAN104 file, so there is no "
                     "section start to measure from.")
        else:
            head += ("\n\nImage filenames will be REPLACED by the corrected "
                     "distance into the section (same 12-digit mm names). A "
                     "rename_manifest.csv is written beside the images — it "
                     "is how the rename is undone and how the app keeps "
                     "matching the folder, so do not delete it.")
        box.setText(head)
        box.setDetailedText(pf.summary_text())
        proceed = None
        if n_ok:
            proceed = box.addButton(f"Process {n_ok} ready run(s)",
                                    QMessageBox.AcceptRole)
        locate = box.addButton("Locate missing file…", QMessageBox.ActionRole) \
            if any(r.missing for r in pf.not_ready) else None
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if locate is not None and clicked is locate:
            if not self._locate_one_missing(pf):
                self._cancel_batch("batch cancelled — nothing was written")
                return
            self._start_preflight()       # re-run the pre-check
            return
        if proceed is None or clicked is not proceed:
            self._cancel_batch("batch cancelled — nothing was written")
            return
        self._start_batch()

    def _cancel_batch(self, message: str):
        self.batch_progress_timer.stop()
        self.batch_action.setEnabled(True)
        self.readout_label.setText(message)

    def _start_batch(self):
        self._batch_stage = None
        self.batch_progress_timer.start()
        self.readout_label.setText("batch: processing…")

        def report_progress(stage, run_id, i, total):
            # called from the worker thread — a single attribute store only.
            # The GUI thread reads it on the timer tick below.
            self._batch_stage = (stage, run_id, i, total)

        def job():
            return process_collection(
                self.run_root, calibrations_dir=self.calibrations_dir,
                overrides=self.overrides, progress=report_progress)

        worker = Worker(job)
        worker.signals.finished.connect(self._on_batch_finished)
        worker.signals.error.connect(self._on_batch_error)
        self.thread_pool.start(worker)

    def _on_batch_progress_timer_timeout(self):
        stage = self._batch_stage
        if stage is None:
            return
        kind, label, i, total = stage
        if kind == "run":
            self.readout_label.setText(f"batch: run {i + 1}/{total} — {label}")
        elif kind == "gocator":
            self.readout_label.setText(f"batch: {label} — {i}/{total} profiles tagged")
        else:
            self.readout_label.setText(f"batch: {label} — {i}/{total} images tagged")

    def _locate_one_missing(self, pf) -> bool:
        """Prompt for the first missing input. False if the user backs out."""
        gap = next((m for r in pf.not_ready for m in r.missing), None)
        if gap is None:
            return True
        desc = disc.KEY_DESCRIPTIONS.get(gap.key, gap.key)
        title = f"Locate for {gap.run_id}: {desc}"
        if gap.key == disc.KEY_IMAGES:
            chosen = QFileDialog.getExistingDirectory(self, title)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, title)
        if not chosen:
            return False
        self.overrides.set(str(self.run_root), gap.run_id, gap.key, Path(chosen))
        return True

    def _on_batch_finished(self, report):
        self.batch_progress_timer.stop()
        self.batch_action.setEnabled(True)
        skipped = [o for o in report.outcomes if o.status == "skipped"]
        # Everything about this batch goes to <root>/Processed: the readable
        # report, the complete fail/warn list, and what is still outstanding.
        written = write_reports(report)
        good = [v for k, v in sorted(written.items()) if not k.startswith("error")]
        bad = [v for k, v in sorted(written.items()) if k.startswith("error")]
        saved = ("\n\nSaved to:\n" + "\n".join(good)) if good else ""
        if bad:
            saved += "\n\n(" + "; ".join(bad) + ")"
        self.readout_label.setText(
            f"batch done: {len(report.written)} written, {len(skipped)} skipped, "
            f"{len(report.needs_attention) - len(skipped)} flagged")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Batch report")
        dialog.setIcon(QMessageBox.Warning if report.needs_attention else QMessageBox.Information)
        msg = f"{len(report.written)} run(s) written."
        if skipped:
            msg += (f"\n\n{len(skipped)} run(s) SKIPPED — no GPS written:\n"
                    + "\n".join(f"  • {o.run_id}: {'; '.join(o.failures)}" for o in skipped[:6]))
        other = len(report.needs_attention) - len(skipped)
        if other:
            msg += f"\n\n{other} run(s) had failures — see details."
        counts = report.n_issues
        if counts:
            msg += ("\n\nLogged: "
                    + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
                    + " — every one is listed in the issues CSV.")
        dialog.setText(msg + saved)
        dialog.setDetailedText(report.to_text())
        dialog.exec()
        if self.result is not None:
            self._on_run_selected(self.result.run.run_id)  # refresh current run

    def _on_batch_error(self, tb: str):
        self.batch_progress_timer.stop()
        self.batch_action.setEnabled(True)
        self._on_worker_error(tb)

    # errors -----------------------------------------------------------------
    def _on_worker_error(self, tb: str):
        QMessageBox.critical(self, "Error", tb[-2000:])
        self.readout_label.setText("error — see dialog")
