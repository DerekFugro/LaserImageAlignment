"""GUI smoke tests (offscreen): the window builds, loads a run, navigates,
and the Locate flow re-processes. Uses the synthetic run root."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pyside = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(qapp, predicate, timeout_ms=30000):
    from PySide6.QtCore import QDeadlineTimer, QThreadPool
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_window_loads_run_and_navigates(qapp, synth_run_root, synth_cal_dir, tmp_path, monkeypatch):
    from core import discovery as disc
    from gui.main_window import MainWindow

    win = MainWindow()
    win.calibrations_dir = synth_cal_dir
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)

    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)
    assert not win.result.export_blocked
    assert win.export_action.isEnabled()

    start = win.current_image
    win._on_next_action_triggered()
    assert win.current_image == start + 1
    win._on_prev_action_triggered()
    assert win.current_image == start

    # QC panel has rows
    assert win.qc_panel.rows_layout.count() > 10

    # pop-out: BOTH cameras move to the window and back, in the same order
    assert win.image_window is None
    win.popout_action.setChecked(True)
    qapp.processEvents()
    assert win.image_window is not None
    assert win.image_view.window() is win.image_window
    assert win.secondary_view.window() is win.image_window, \
        "the popped-out window must carry the secondary camera too"
    assert win.main_splitter.count() == 2  # left panels + profiles remain
    win.popout_action.setChecked(False)
    qapp.processEvents()
    assert win.image_window is None
    assert win.main_splitter.count() == 3
    assert win.main_splitter.widget(1) is win.image_stack
    # secondary ABOVE primary, docked and popped out alike
    assert win.image_stack.indexOf(win.secondary_pane) == 0
    assert win.image_stack.indexOf(win.image_view) == 1

    # closing the pop-out window docks the view back
    win.popout_action.setChecked(True)
    qapp.processEvents()
    win.image_window.close()
    qapp.processEvents()
    assert win.image_window is None
    assert not win.popout_action.isChecked()
    assert win.main_splitter.widget(1) is win.image_stack
    win.close()


def test_locate_flow_reprocesses(qapp, synth_run_root, synth_cal_dir, tmp_path, monkeypatch):
    from core import discovery as disc
    from gui.main_window import MainWindow

    # MainWindow no longer reads DEFAULT_CALIBRATIONS_DIR directly - it asks
    # core.config, which would otherwise hand it whatever lia.ini on THIS
    # machine says. Patching the constant here silently stopped working when
    # that indirection went in; the env var is the layer that outranks the INI.
    monkeypatch.setenv("LIA_CALIBRATIONS", str(synth_cal_dir))
    monkeypatch.setattr(disc, "DEFAULT_CALIBRATIONS_DIR", synth_cal_dir)
    # move the nav export out of the tree
    nav = next(synth_run_root.rglob("ascii-output.txt"))
    moved = tmp_path / "elsewhere" / "ascii-output.txt"
    moved.parent.mkdir()
    nav.rename(moved)

    win = MainWindow()
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)
    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)
    assert win.result.export_blocked  # nav export missing -> FAIL

    # simulate the user locating the file
    win._on_locate_requested(disc.KEY_NAV_EXPORT, str(moved))
    assert _wait_until(qapp, lambda: win.result is not None and not win.result.export_blocked)
    # override persisted
    fresh = disc.OverrideStore(tmp_path / "ov.json")
    run = win.result.run
    assert fresh.get(run.root, run.run_id, disc.KEY_NAV_EXPORT) == moved
    win.close()


def test_batch_precheck_runs_off_the_gui_thread(qapp, synth_run_root, synth_cal_dir,
                                                tmp_path, monkeypatch):
    """The pre-check parses the whole postprocessed export, so it must run in a
    worker; the GUI stays responsive and the dialog only appears afterwards."""
    from PySide6.QtWidgets import QMessageBox
    from core import discovery as disc
    from gui import main_window as mw

    win = mw.MainWindow()
    win.calibrations_dir = synth_cal_dir
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.run_root = synth_run_root

    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self) or 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)  # = Cancel

    win._on_batch_action_triggered()
    assert not win.batch_action.isEnabled()      # returns immediately, no dialog yet
    assert shown == []
    assert _wait_until(qapp, lambda: bool(shown))
    assert _wait_until(qapp, lambda: win.batch_action.isEnabled())
    assert "cancelled" in win.readout_label.text()
    assert not win.batch_progress_timer.isActive()
    win.close()


def test_batch_progress_reaches_the_status_line(qapp, tmp_path):
    from gui.main_window import MainWindow

    win = MainWindow()
    win._batch_stage = ("run", "20260818.142721", 1, 5)
    win._on_batch_progress_timer_timeout()
    assert "run 2/5" in win.readout_label.text()
    win._batch_stage = ("images", "20260818.142721 Rear", 100, 593)
    win._on_batch_progress_timer_timeout()
    assert "100/593 images" in win.readout_label.text()
    win._batch_stage = ("gocator", "20260818.142721 L", 2000, 2545)
    win._on_batch_progress_timer_timeout()
    assert "2000/2545 profiles" in win.readout_label.text()
    win.close()




def test_scan_cursor_points_at_a_scan_in_both_views(
        qapp, synth_run_root, synth_cal_dir, tmp_path):
    """The cursor is the whole mechanism: it names ONE scan, shows it orange on
    the plot AND orange on the image, and stepping it walks that line along the
    picture ~40 px at a time. Regression guard for the first version, which
    moved the plot window and the marks together so nothing appeared to move."""
    from core import discovery as disc
    from gui.main_window import MainWindow

    win = MainWindow()
    win.calibrations_dir = synth_cal_dir
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)
    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)
    win.current_image = len(win.result.parsed.images) // 2
    win._show_current_image()
    assert _wait_until(qapp, lambda: win.profile_view.shown.get("L") is not None)

    assert win._scan_cursor == 0
    scan0, ground0 = win._cursor_scan()
    marks0 = win._scan_overlay_lines(win._cam_centre_m)
    assert sum(1 for m in marks0 if m[1]) == 1          # exactly one labelled
    assert f"SCAN {scan0}" in [m[1] for m in marks0]

    win._nudge_scans(+10)
    assert win._scan_cursor == 10
    scan10, ground10 = win._cursor_scan()
    assert scan10 == scan0 + 10                          # a different, named scan
    # ten scans really is ten profile spacings of ground (the synthetic fixture
    # uses its own spacing, so measure it rather than assuming the real 23.98)
    d = win.result.gocator_l_dist_m
    spacing = float(d[scan0 + 1] - d[scan0])
    assert (ground10 - ground0) == pytest.approx(10 * spacing, rel=0.05)
    # and the orange line moved down the image by that much
    cal = win.result.parsed.calibration
    y0 = next(m[0] for m in marks0 if m[1])
    y10 = next(m[0] for m in win._scan_overlay_lines(win._cam_centre_m) if m[1])
    assert (y10 - y0) == pytest.approx(
        (ground10 - ground0) * 1000 / cal.mm_per_px_y(), rel=0.02)
    assert f"cursor SCAN {scan10}" in win.readout_label.text()

    win._nudge_scans(-1)
    assert win._cursor_scan()[0] == scan0 + 9
    win._nudge_scans(0, absolute=True)
    assert win._cursor_scan()[0] == scan0
    win.close()




def test_undistort_is_on_by_default_and_survives_construction(qapp):
    """It was 'default on' for hours and never actually was: _ensure_undistorter
    unchecked it during __init__, because no run is loaded yet so there is no
    calibration. Every ground-truth click got logged with undistorted=False."""
    from gui.main_window import MainWindow

    win = MainWindow()
    assert win.undistort_action.isChecked(), "default-on must survive construction"
    assert "RAW" in win.image_state_label.text()      # honest: nothing shown yet
    win.close()


def test_undistort_turns_itself_off_only_for_a_run_with_no_calibration(
        qapp, synth_run_root, tmp_path):
    from core import discovery as disc
    from gui.main_window import MainWindow

    win = MainWindow()
    win.calibrations_dir = tmp_path / "no-calibrations-here"
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)
    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)
    assert win.result.parsed.calibration is None
    assert _wait_until(qapp, lambda: not win.undistort_action.isChecked())
    assert "no calibration" in win.image_state_label.text()
    win.close()


def test_row_camera_is_shown_above_the_rear_one(qapp, synth_run_root, synth_cal_dir,
                                                tmp_path):
    """Derek, 2026-09-01: ROW on top, Rear below, and the pop-out the same.

    The point is to LOOK at what ROW caught. It is a plain picture viewer:
    ROW's oblique ground-plane projection is not implemented in this app, so
    measure, undistort and the bar tool stay on the primary camera where the
    millimetres they produce mean something.
    """
    from core import discovery as disc
    from gui.main_window import MainWindow

    win = MainWindow()
    win.calibrations_dir = synth_cal_dir
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)
    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)

    assert win._secondary_camera() == "ROW"
    assert win._primary_camera() == "Rear"
    assert win.secondary_pane.isVisibleTo(win.image_stack), \
        "the run has a ROW folder, so the pane must be showing"
    assert "ROW" in win.secondary_label.text()
    assert win.image_stack.indexOf(win.secondary_pane) == 0   # on top
    assert win.image_stack.indexOf(win.image_view) == 1       # rear below
    win.close()


def test_the_two_cameras_are_paired_by_trigger_not_by_ordinal(qapp, synth_run_root,
                                                              synth_cal_dir, tmp_path):
    """The off-by-one this is built to avoid.

    Both cameras fire on the same trigger, but they are matched to the trigger
    train independently, so their ORDINALS only agree while both hold the same
    number of pictures. Take ONE image off the front of ROW - which is exactly
    what filing a pre-section frame into BeforeCollection does - and every pair
    after it is off by one: a ROW frame 0.75 m from the Rear one it is supposed
    to sit beside, with nothing on screen saying so.

    So the ordinals here must NOT be equal, and the triggers must be.
    """
    from core import discovery as disc
    from gui.main_window import MainWindow

    row_dir = synth_run_root / "Images" / "20260816.150000" / "ROW"
    first = sorted(row_dir.glob("*.jpg"))[0]
    first.unlink()                       # ROW now holds one fewer than Rear

    win = MainWindow()
    win.calibrations_dir = synth_cal_dir
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)
    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)

    res = win.result
    prim, sec = win._primary_camera(), win._secondary_camera()
    assert len(res.parsed.cameras[sec]) == len(res.parsed.cameras[prim]) - 1

    checked = 0
    for j in range(len(res.parsed.images)):
        k = win._secondary_index(sec, j)
        if k < 0:
            continue
        # the pair really is one trigger, whatever the two ordinals are
        assert (res.camera_matches[prim].trigger_for_image[j]
                == res.camera_matches[sec].trigger_for_image[k])
        checked += 1
        if k != j:
            break
    else:
        raise AssertionError(
            "every pair had k == j, so this fixture cannot tell trigger-pairing "
            "from ordinal-pairing and the test proves nothing")
    assert checked
    win.close()


def test_a_run_with_only_one_camera_hides_the_pane(qapp, synth_run_root,
                                                   synth_cal_dir, tmp_path):
    """Nothing on the cart but the rear camera: no empty grey box on top."""
    import shutil

    from core import discovery as disc
    from gui.main_window import MainWindow

    shutil.rmtree(synth_run_root / "Images" / "20260816.150000" / "ROW")
    win = MainWindow()
    win.calibrations_dir = synth_cal_dir
    win.overrides = disc.OverrideStore(tmp_path / "ov.json")
    win.open_root(synth_run_root)
    assert _wait_until(qapp, lambda: len(win.runs) == 1)
    win.run_panel.run_list.setCurrentRow(0)
    assert _wait_until(qapp, lambda: win.result is not None)

    assert win._secondary_camera() == ""
    assert not win.secondary_pane.isVisibleTo(win.image_stack)
    win.close()
