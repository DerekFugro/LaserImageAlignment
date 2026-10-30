"""ROW is tagged with the ground it shows (Derek, 2026-10-30).

A camera looking ahead sees ground in front of it. With a "view distance" in
the lever-arm file its GPS is that ground, so an object the Rear (looking
down) and the ROW camera both see carries the same GPS in both."""
from __future__ import annotations

import numpy as np

from core.alignment import (PtpOffsetResult, align_images,
                            match_images_to_triggers)
from core.calibration import (parse_lever_arms, parse_lever_arms_checked,
                              trigger_delay_s, view_distance_m)
from tests.test_trigger_delay import FILE, _images, _Nav, _triggers

FILE_VIEW = FILE + """
## ROW camera view distance

Prose with numbers like 9.2 and 12-13 is ignored.

6.61
"""


def test_view_distance_is_read_and_nothing_else_moves():
    arms, warns = parse_lever_arms_checked(FILE_VIEW)
    assert not warns
    assert view_distance_m(arms, "ROW") == 6.61
    assert view_distance_m(arms, "Rear") is None
    assert arms["ROW"] == (1.9, 0.0, 0.0)          # the arm is untouched
    assert trigger_delay_s(arms, "Rear") == 0.32
    assert view_distance_m(parse_lever_arms(FILE), "ROW") is None


def _ptp():
    return PtpOffsetResult(offset_s=37.0, r=1.0, passed=True, n_used=0,
                           sweep_offsets=np.empty(0), sweep_r=np.empty(0), message="")


def test_gps_moves_ahead_by_the_view_distance_and_camera_is_kept():
    nav = _Nav(speed=2.0)                         # straight north
    ims, trg = _images(range(1, 11)), _triggers(10)
    m = match_images_to_triggers(ims, trg, photo_lag=1)
    plain = align_images(ims, trg, m, _ptp(), nav, arm_xyz=(1.9, 0, 0))
    view = align_images(ims, trg, m, _ptp(), nav, arm_xyz=(1.9, 0, 0), view_m=6.61)
    ok = np.isfinite(plain.lat_deg)
    moved = (view.lat_deg[ok] - plain.lat_deg[ok]) * 111_320.0
    assert np.allclose(moved, 6.61, atol=1e-3)            # forward, north
    assert np.allclose(view.lon_deg[ok], plain.lon_deg[ok])
    assert np.allclose(view.alt_m[ok], plain.alt_m[ok])   # altitude unchanged
    assert np.allclose(view.camera_lat_deg[ok], plain.lat_deg[ok])
    assert view.view_m == 6.61
    assert plain.camera_lat_deg is None and plain.view_m == 0.0
