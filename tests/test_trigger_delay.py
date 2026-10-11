"""Camera trigger delay (Derek, 2026-10-11).

The 2026-10-10 yardstick runs: photo n = trigger n-1 taken 0.32 s later fits
all 25 stick passes (5-11 km/h) within 17 mm RMS; photo n = trigger n with no
delay leaves a miss that grows with speed. The delay lives in the lever-arm
file, per camera; a camera without one keeps photo n = trigger n."""
from __future__ import annotations

import numpy as np

from core.alignment import (PtpOffsetResult, TriggerData, align_images,
                            match_images_to_triggers)
from core.calibration import parse_lever_arms, trigger_delay_s
from core.formats import ImageSet

FILE = """## Rear camera

X  -0.6858
Y   0
Z   0

## ROW camera

X  +1.90

## Rear camera trigger delay

0.32

## Rear

1.36
"""


def _images(ordinals):
    c = np.asarray([750 * n for n in ordinals], dtype=np.int64)
    return ImageSet(dir="x", files=[f"{v:012d}.jpg" for v in c], counter_mm=c)


def _triggers(n, n_prepended=0, speed=2.0):
    t = np.arange(n) * (0.75 / speed)
    d = t * speed
    return TriggerData(sbg_us=np.arange(n, dtype=np.int64), utc_s=t, dist_m=d,
                       spacing_m=np.diff(d), spacing_ok_frac=1.0, message="",
                       n_prepended=n_prepended)


class _Nav:
    """Straight north at a steady speed, so distance = speed x time."""

    def __init__(self, speed=2.0):
        self.v = speed

    def dist_at(self, t):
        return np.asarray(t, dtype=np.float64) * self.v

    def heading_at(self, t):
        return np.zeros(len(np.atleast_1d(t)))

    def locate(self, t):
        t = np.asarray(t, dtype=np.float64)
        return {"lat_deg": 45.0 + t * self.v / 111_320.0,
                "lon_deg": np.full(t.shape, -75.0), "alt_m": np.zeros(t.shape)}


def test_delay_is_read_and_arms_are_untouched():
    arms = parse_lever_arms(FILE)
    assert trigger_delay_s(arms, "Rear") == 0.32
    assert trigger_delay_s(arms, "ROW") is None
    assert arms["Rear"] == (-0.6858, 0.0, 0.0)
    assert arms["ROW"] == (1.9, 0.0, 0.0)


def test_lagged_photo_1_unplaced_without_the_recovered_trigger():
    m = match_images_to_triggers(_images(range(1, 11)), _triggers(10), photo_lag=1)
    assert int(m.trigger_for_image[0]) == -1
    assert list(m.trigger_for_image[1:4]) == [0, 1, 2]


def test_lagged_photo_1_takes_the_recovered_trigger():
    m = match_images_to_triggers(_images(range(1, 11)), _triggers(11, n_prepended=1),
                                 photo_lag=1)
    assert list(m.trigger_for_image[:3]) == [0, 1, 2]


def test_camera_without_delay_skips_the_recovered_trigger():
    m = match_images_to_triggers(_images(range(1, 11)), _triggers(11, n_prepended=1))
    assert list(m.trigger_for_image[:3]) == [1, 2, 3]


def test_photo_is_placed_at_trigger_time_plus_delay():
    trg = _triggers(10)
    ims = _images(range(1, 10))
    m = match_images_to_triggers(ims, trg)
    ptp = PtpOffsetResult(offset_s=37.0, r=1.0, passed=True, n_used=0, message="",
                          sweep_offsets=np.empty(0), sweep_r=np.empty(0))
    al = align_images(ims, trg, m, ptp, _Nav(), delay_s=0.32)
    assert np.allclose(al.utc_s, trg.utc_s[:9] + 0.32)
    # 0.32 s at 2 m/s = 640 mm further on
    assert np.allclose(al.dmi_dist_m, trg.dist_m[:9] + 0.64)


# --- photo 1 is the camera start-up frame (Derek, 2026-10-11) ---------------
# The camera log for the 2026-10-10 stick runs: acquisition started 1.5-10 s
# after the last trigger before logging and before the first logged one, with
# no trigger in between. Photo 1 has no trigger: no GPS, BeforeCollection.

def test_startup_frame_is_unplaced_and_flagged_in_the_message():
    m = match_images_to_triggers(_images(range(1, 11)), _triggers(10), photo_lag=1)
    assert int(m.trigger_for_image[0]) == -1
    assert "start-up frame" in m.message
    assert "beyond the last trigger" not in m.message


def test_startup_frame_found_only_for_a_delayed_camera():
    from core.batch import _startup_frames

    class _Al:
        images = _images(range(1, 6))
        utc_s = np.array([np.nan, 1.0, 2.0, 3.0, 4.0])

    arms = parse_lever_arms(FILE)
    assert _startup_frames(_Al, arms, "Rear") == [0]
    assert _startup_frames(_Al, arms, "ROW") == []


def test_startup_frame_renamed_and_set_aside(tmp_path):
    import csv
    from core.daily import DailyEntry
    from core.rename import (BEFORE_DIR, MANIFEST_NAME, move_before_collection,
                             rename_camera_folder)
    names = [f"{750 * n:012d}.jpg" for n in range(1, 4)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    entry = DailyEntry("run", "300000", "N", 0.0, 0.1, 37.6, -77.4)
    r = rename_camera_folder(tmp_path, names, [np.nan, 100.0, 850.0], entry,
                             "run", "Rear", startup={names[0]})
    assert r.final_names[0] == "-startup_000000000750.jpg"
    n, probs = move_before_collection(tmp_path)
    assert n == 1 and not probs
    assert (tmp_path / BEFORE_DIR / "-startup_000000000750.jpg").is_file()
    rows = list(csv.DictReader(open(tmp_path / MANIFEST_NAME, encoding="utf-8")))
    row = next(x for x in rows if x["original_name"] == names[0])
    assert "start-up frame" in row["note"]
    # a re-run scan still finds its odometer counter through the manifest
    from core.formats import ImageSet
    from core.rename import restore_set_aside
    restore_set_aside(tmp_path)
    s = ImageSet.scan(tmp_path)
    assert 750 in list(s.counter_mm)


def test_clear_gps_removes_an_old_stamp(tmp_path):
    import pytest
    piexif = pytest.importorskip("piexif")
    cv2 = pytest.importorskip("cv2")
    from core.geotag import clear_gps, geotag_image
    p = tmp_path / "a.jpg"
    cv2.imwrite(str(p), np.full((16, 16, 3), 128, dtype=np.uint8))
    geotag_image(p, 37.6, -77.4, 50.0, 1_791_000_000.0)
    assert piexif.load(str(p))["GPS"]
    assert clear_gps(p) is True
    assert not piexif.load(str(p))["GPS"]
    assert clear_gps(p) is False
