"""Missing images mode (Derek, 2026-11-10).

A loose encoder power wire (20261031.101107, 20261101.093228): holes with no
pulses, pulses too close for the camera, about one pulse per power drop
stamped seconds BACKWARDS, and - in 093228 - pulses the camera missed that
only the camera log (a line every 100th photo) can place."""
from __future__ import annotations

import numpy as np
import pytest

from core import cameralog as cl
from core.alignment import TriggerData, match_images_to_triggers
from core.formats import ImageSet, ParseError, parse_event_triggers
from tests.conftest import write_sbg_table


def _write(p, ts):
    write_sbg_table(p, ["timestamp", "status"], ["(us)", "(na)"],
                    [[str(t), "0x0"] for t in ts])


class TestBackwardsPulses:
    def test_dropped_and_named(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        good = [1_000_000 + 500_000 * k for k in range(200)]
        ts = good[:94] + [good[93] - 4_300_000] + good[94:]      # 101107's shape
        _write(p, ts)
        got, wraps, dropped = parse_event_triggers(p, drop_backwards=True)
        assert dropped == [94] and wraps == 0
        assert got.tolist() == good

    def test_default_still_refuses(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        _write(p, [5_000_000, 4_000_000, 6_000_000])
        with pytest.raises(ParseError, match="not strictly increasing"):
            parse_event_triggers(p)

    def test_a_broken_file_is_still_refused(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        rng = np.random.default_rng(1)
        _write(p, rng.integers(1, 10**9, 300).tolist())
        with pytest.raises(ParseError, match="too many"):
            parse_event_triggers(p, drop_backwards=True)


def _pulses(n, extra_after=()):
    """Pulse times ~0.5 s apart (varying, like a real walk); `extra_after`
    inserts a pulse 0.2 s after those indices - one the camera missed."""
    rng = np.random.default_rng(0)
    t, out = 100.0, []
    for i in range(n):
        t += 0.45 + 0.1 * rng.random()
        out.append(t)
        if i in extra_after:
            out.append(t + 0.2)
            t += 0.2
    return np.array(out)


def _frames(true_pulse_of_photo, every=100, off=-1234.5):
    """Camera-log lines: (photo ordinal, camera clock) for every 100th photo."""
    return [(o, float(true_pulse_of_photo(o)) + off)
            for o in range(every, 10_000, every) if true_pulse_of_photo(o) is not None]


class TestFollowFrames:
    def test_normal_run_one_k(self):
        t = _pulses(700)
        fr = _frames(lambda o: t[o - 2] if o - 2 < len(t) else None)   # photo n <- pulse n-1
        seq = cl.follow_frames(fr, t)
        assert seq is not None and {k for _, k in seq} == {-1}

    def test_missed_pulses_found_where_they_are(self):
        # 2 pulses the camera missed: one after pulse 250, one after pulse 480
        t = _pulses(700, extra_after=(250, 480))
        real = np.delete(np.arange(len(t)), [251, 482])   # pulses that took photos
        fr = _frames(lambda o: t[real[o - 2]] if o - 2 < len(real) else None)
        seq = cl.follow_frames(fr, t)
        assert [k for _, k in seq] == [-1, -1, 0, 0, 1, 1, 1]
        drop, spans = cl.extra_pulses(seq, t, k_start=-1)
        assert drop == [251, 482]
        assert spans == [(201, 299), (401, 499)]
        # and with them passed over, one pairing fits every line again
        seq2 = cl.follow_frames(fr, np.delete(t, drop))
        assert {k for _, k in seq2} == {-1}

    def test_a_photo_with_no_pulse_is_not_guessed(self):
        seq = [(100, 0), (200, -1)]
        assert cl.extra_pulses(seq, _pulses(300), k_start=0) is None


def test_matcher_counts_along_the_cleaned_pulses():
    ims = ImageSet(dir="x", files=[f"{750 * n:012d}.jpg" for n in range(1, 8)],
                   counter_mm=np.array([750 * n for n in range(1, 8)]))
    t = np.arange(8, dtype=float)
    trg = TriggerData(sbg_us=np.arange(8), utc_s=t, dist_m=t * 0.75,
                      spacing_m=np.diff(t * 0.75), spacing_ok_frac=1.0, message="")
    usable = np.delete(np.arange(8), [3])          # pulse 4 took no photo
    m = match_images_to_triggers(ims, trg, mode="head", photo_lag=1, usable=usable)
    assert m.trigger_for_image.tolist() == [-1, 0, 1, 2, 4, 5, 6]


def test_holes_and_status_in_the_table():
    from core.pipeline import _gaps_and_status

    class _Al:
        pass
    al = _Al()
    al.images = ImageSet(dir="x", files=[f"{750 * n:012d}.jpg" for n in range(1, 6)],
                         counter_mm=np.array([750 * n for n in range(1, 6)]))
    trg = np.array([-1, 0, 1, 2, 3])
    d = np.array([0.0, 0.75, 1.5, 19.0, 19.75])      # 17.5 m between photos 3 and 4
    al.triggers = TriggerData(sbg_us=np.arange(4), utc_s=np.arange(4.0), dist_m=d[1:],
                              spacing_m=np.diff(d[1:]), spacing_ok_frac=1.0, message="")
    al.match = type("M", (), {"trigger_for_image": trg})()
    al.triggers.dist_m = np.array([0.75, 1.5, 19.0, 19.75])
    gap, status = _gaps_and_status(al, [(2, 3)])
    assert gap[3] == pytest.approx(19.0 - 1.5 - 0.75)
    assert gap[[0, 1, 2, 4]].tolist() == [0, 0, 0, 0]
    assert status == ["confirmed", "estimated", "estimated", "confirmed", "confirmed"]


def test_camera_start_an_hour_off_is_not_used():
    log = cl.CameraLog(starts_local=np.array([1000.0]),
                       clock_local=np.array([1000.0]), clock_offset=np.array([3660.0]))
    trig = np.array([1003.0, 1003.5, 1004.0])      # really 3 s after the start
    assert cl.from_start(log, trig, 0.32, 995.0) is None


# --- duplicates: photos of the same spot (van standing still) -------------

def _al_with_dist(d):
    class _Al:
        pass
    al = _Al()
    n = len(d)
    al.images = ImageSet(dir="x", files=[f"{750 * k:012d}.jpg" for k in range(1, n + 1)],
                         counter_mm=np.array([750 * k for k in range(1, n + 1)]))
    al.match = type("M", (), {"trigger_for_image": np.arange(n)})()
    al.dmi_dist_m = np.array(d, dtype=float)
    al.lat_deg = np.arange(n, dtype=float)
    al.lon_deg = np.arange(n, dtype=float) * 10
    al.alt_m = np.zeros(n)
    al.camera_lat_deg = al.camera_lon_deg = None
    return al


def test_duplicates_101107_shape(monkeypatch):
    import core.pipeline as pl
    monkeypatch.setattr(pl, '_picture_shift_px', lambda a, b: 5.0)   # pictures agree
    from core.pipeline import _duplicates
    # 101107 photos 89-93: 886.43, 886.79, 886.82, 886.85, then 893.81
    al = _al_with_dist([886.43, 886.79, 886.82, 886.85, 893.81, 894.56])
    dups = _duplicates(al)
    assert dups == {2: 1, 3: 1}
    assert al.lat_deg[2] == al.lat_deg[3] == al.lat_deg[1]      # the kept photo's GPS
    assert al.lat_deg[4] == 4


def test_a_slow_creep_does_not_chain(monkeypatch):
    import core.pipeline as pl
    monkeypatch.setattr(pl, '_picture_shift_px', lambda a, b: 5.0)
    from core.pipeline import _duplicates
    al = _al_with_dist([0.0, 0.04, 0.08, 0.12, 0.16, 0.20])
    assert _duplicates(al) == {1: 0, 2: 0, 4: 3, 5: 3}


def test_duplicates_folder_lifecycle(tmp_path):
    from core.formats import DUPLICATES_DIR
    from core.rename import move_duplicates, restore_set_aside
    for k in range(1, 6):
        (tmp_path / f"{750 * k:012d}.jpg").write_bytes(b"x")
    n, probs = move_duplicates(tmp_path, ["000000002250.jpg"])
    assert n == 1 and not probs
    assert (tmp_path / DUPLICATES_DIR / "000000002250.jpg").is_file()
    s = ImageSet.scan(tmp_path)
    assert len(s) == 4 and s.n_set_aside == 1                    # still counted
    moved, probs = restore_set_aside(tmp_path)                   # a re-run brings it back
    assert moved == ["000000002250.jpg"] and not (tmp_path / DUPLICATES_DIR).exists()
    assert len(ImageSet.scan(tmp_path)) == 5


def test_pulse_says_duplicate_but_picture_moved(monkeypatch):
    """093228 photo 4: 6 cm by its (estimated) pulse, 454 px by its picture."""
    import core.pipeline as pl
    monkeypatch.setattr(pl, "_picture_shift_px", lambda a, b: 454.0)
    al = _al_with_dist([0.0, 0.75, 0.78, 1.5])
    assert pl._duplicates(al) == {}


def test_row_follows_rear_by_pulse():
    from core.pipeline import _duplicates_by_pulse
    al = _al_with_dist([0.0, 0.75, 0.76, 1.5])
    al.match.trigger_for_image = np.array([-1, 5, 6, 7])
    dups = _duplicates_by_pulse(al, {6: 5})       # Rear: pulse 6's photo repeats pulse 5's
    assert dups == {2: 1} and al.lat_deg[2] == al.lat_deg[1]
