"""The viewer places scans on a photo by GPS: (scan position - photo centre
position) along the heading. Derek, 2026-10-09."""
from __future__ import annotations

import numpy as np
import pytest

from core.alignment import EARTH_M_PER_DEG_LAT, along_across_m
from tests.test_gui_smoke import _wait_until, qapp  # noqa: F401  (fixture)

LAT0, LON0 = 37.6561, -77.4793


def offset(north_m, east_m):
    return (LAT0 + north_m / EARTH_M_PER_DEG_LAT,
            LON0 + east_m / (EARTH_M_PER_DEG_LAT * np.cos(np.radians(LAT0))))


@pytest.mark.parametrize("heading, north, east, along, across", [
    (0.0, 1.0, 0.0, 1.0, 0.0),        # ahead, heading north
    (0.0, 0.0, 1.0, 0.0, 1.0),        # east is RIGHT when heading north
    (90.0, 0.0, 1.0, 1.0, 0.0),       # ahead, heading east
    (90.0, 1.0, 0.0, 0.0, -1.0),      # north is LEFT when heading east
    (180.0, 0.47, 0.0, -0.47, 0.0),   # behind, heading south
    (332.7, 0.0, 0.0, 0.0, 0.0),
])
def test_along_across(heading, north, east, along, across):
    lat, lon = offset(north, east)
    a, c = along_across_m(LAT0, LON0, heading, np.array([lat]), np.array([lon]))
    assert a[0] == pytest.approx(along, abs=1e-6)
    assert c[0] == pytest.approx(across, abs=1e-6)


def test_heading_wrap_is_continuous():
    lat, lon = offset(1.0, 0.0)
    a1, _ = along_across_m(LAT0, LON0, 359.9, np.array([lat]), np.array([lon]))
    a2, _ = along_across_m(LAT0, LON0, 0.1, np.array([lat]), np.array([lon]))
    assert a1[0] == pytest.approx(a2[0], abs=1e-6)


def test_viewer_uses_gps_axis(qapp, synth_run_root, synth_cal_dir, tmp_path):
    """With a trajectory, the viewer puts the photo centre at 0 on a GPS axis
    and the scan readout says so; the lasers sit about the lever arm's 0.36 m
    either side of the photo centre."""
    from core import discovery as disc
    from gui.main_window import MainWindow
    from tests.test_gui_smoke import _wait_until

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
    assert win._view_mode == "gps"
    assert win._cam_centre_m == 0.0
    row, ground = win._cursor_scan()
    assert abs(ground) < win._along_track_half_span_m()
    assert "from photo centre (by GPS" in win.readout_label.text()
    # the two lasers' scans under the photo centre are the lever arms apart
    # sideways (0.715 m). (The synthetic yaw is not the track direction, so a
    # single side's across value also carries some along-track motion; the
    # L/R difference does not.)
    arms = win.result.parsed.lever_arms
    sl, sr = win.profile_view.shown.get("L"), win.profile_view.shown.get("R")
    if sl is not None and sr is not None:
        gap = win._view_across["R"][sr[1]] - win._view_across["L"][sl[1]]
        assert gap == pytest.approx(arms["R"][1] - arms["L"][1], abs=0.05)
    win.close()
