"""Missing images mode (Derek, 2026-11-10).

Bad SBG pulse logs around standstills (20261031.101107, 20261101.093228): holes with no
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
        with pytest.raises(ParseError, match="looks broken"):
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


class TestBackwardsLimit:
    """2% of the pulses, or 5, whichever is more (Batch 6/7, 2026-11-17)."""

    def _ts(self, n, back_at):
        good = [1_000_000 + 500_000 * k for k in range(n)]
        out = []
        for i, v in enumerate(good):
            out.append(v)
            if i in back_at:
                out.append(v - 3_000_000)
        return out

    def test_short_run_two_at_the_end(self, tmp_path):
        # 20261027.090833: 2 of 184, refused under the old 1% limit
        p = tmp_path / "eventOutA.txt"
        _write(p, self._ts(182, {179, 180}))
        ts, _, dropped = parse_event_triggers(p, drop_backwards=True)
        assert len(dropped) == 2 and len(ts) == 182

    def test_a_block_of_sixteen(self, tmp_path):
        # 20261030.092846: 16 of 1108
        p = tmp_path / "eventOutA.txt"
        _write(p, self._ts(1092, set(range(850, 866))))
        ts, _, dropped = parse_event_triggers(p, drop_backwards=True)
        assert len(dropped) == 16 and len(ts) == 1092

    def test_over_two_percent_refused(self, tmp_path):
        # 20261030.100722: 1011 of 42417 (2.4%) - kept as a smaller copy
        p = tmp_path / "eventOutA.txt"
        _write(p, self._ts(976, set(range(100, 1000, 40))))   # 23 of 999 = 2.3%
        with pytest.raises(ParseError, match="looks broken"):
            parse_event_triggers(p, drop_backwards=True)


class TestTooSoonRuleSetAside:
    """The guarded fallback in _pairing (built for 20261030.092846, whose real
    cause turned out to be a pulse with a wrong eventOutA time - see
    TestEchoMendsBackwardPulses). Made-up data: the camera photographed a
    pulse 0.094 s after the one
    before (the 0.13 s rule had passed it over), while other pulses 0.10-0.136 s
    apart got no photo. The camera log decides which."""

    def _run(self, monkeypatch, photographed_short):
        import core.pipeline as pl
        from core.alignment import camera_trigger_indices
        from core.qc import QCReport
        rng = np.random.default_rng(3)
        t, out, short, missed = 100.0, [], None, []
        for i in range(700):
            t += 0.45 + 0.1 * rng.random()
            out.append(t)
            if i == 39:                       # a pulse 0.094 s later
                t += 0.094
                out.append(t)
                short = len(out) - 1
            if i in (250, 480):               # pulses 0.100 / 0.136 s later,
                t += 0.1 if i == 250 else 0.136   # no photo (092846: 159, 371)
                out.append(t)
                missed.append(len(out) - 1)
        t = np.array(out)
        no_photo = list(missed) + ([] if photographed_short else [short])
        real = np.delete(np.arange(len(t)), no_photo)
        frames = [(o, float(t[real[o - 2]]) - 500.0)
                  for o in range(100, 10_000, 100) if o - 2 < len(real)]
        log = cl.CameraLog(frames={("R1", "Rear"): frames})
        monkeypatch.setattr(cl, "load", lambda p: log)
        monkeypatch.setattr(pl.cal, "trigger_delay_s", lambda arms, c: 0.0)
        run = type("Run", (), {"run_id": "R1", "get": lambda self, k: "x.log"})()
        parsed = type("P", (), {"cameras": {"Rear": list(range(len(real) + 1))},
                                "lever_arms": None})()
        trg = type("T", (), {"utc_s": t})()
        usable, _ = camera_trigger_indices(t)
        report = QCReport("R1")
        lags, by_cam, spans = pl._pairing(run, parsed, trg, report, usable)
        return lags, by_cam, report, short, missed, usable, len(t)

    def test_camera_log_overrules_the_rule(self, monkeypatch):
        lags, by_cam, report, short, missed, usable, n = self._run(monkeypatch, True)
        assert short not in usable                     # the rule passed it over
        assert lags == {"Rear": 1}
        assert sorted(set(range(n)) - set(by_cam["Rear"].tolist())) == sorted(missed)
        row = next(c for c in report.checks if c.check_id == "align.camera.Rear.missing_images")
        assert row.values["from_every_pulse"] and short in row.values["photographed_too_soon"]
        pairing = next(c for c in report.checks if c.check_id == "align.camera.Rear.pairing")
        assert pairing.values["method"] == "camera log" and pairing.values["k"] == -1

    def test_rule_right_nothing_changes(self, monkeypatch):
        # the short pulse really got no photo: the rule fits, no retry needed
        lags, by_cam, report, short, missed, usable, n = self._run(monkeypatch, False)
        assert lags == {"Rear": 1}
        assert not any(c.values.get("from_every_pulse") for c in report.checks
                       if c.check_id.endswith(".missing_images"))


class TestCameraLogMustAgree:
    """A pairing decided by the camera start is not a PASS when the camera
    log does not fit it (20261030.162920: start n-1, log 392 ms off)."""

    def test_frame_error(self):
        t = _pulses(500)
        fr = _frames(lambda o: t[o - 2] if o - 2 < len(t) else None)   # n-1
        assert cl.frame_error_s(fr, t, -1) < 1e-9
        assert cl.frame_error_s(fr, t, 0) > cl.FIT_MAX_ERR_S
        assert cl.frame_error_s(fr[:1], t, -1) is None

    def _pairing(self, monkeypatch, k_true):
        import core.pipeline as pl
        from core.qc import QCReport
        t = _pulses(500)
        fr = _frames(lambda o: t[o - 1 + k_true] if 0 <= o - 1 + k_true < len(t) else None)
        log = cl.CameraLog(frames={("R1", "Rear"): fr})
        monkeypatch.setattr(cl, "load", lambda p: log)
        monkeypatch.setattr(pl.cal, "trigger_delay_s", lambda arms, c: 0.0)
        # the camera start "proves" n-1, whatever the log says
        monkeypatch.setattr(cl, "decide", lambda *a, **kw: cl.OffsetResult(
            -1, "camera start", True, "cameras were ready 2.0 s before - photo n = trigger n-1"))
        monkeypatch.setattr(pl, "_missing_images", lambda *a, **kw: (None, None))
        run = type("Run", (), {"run_id": "R1", "get": lambda self, k: "x.log"})()
        parsed = type("P", (), {"cameras": {"Rear": [0] * 450}, "lever_arms": None})()
        trg = type("T", (), {"utc_s": t})()
        report = QCReport("R1")
        pl._pairing(run, parsed, trg, report, None)
        return next(c for c in report.checks if c.check_id == "align.camera.Rear.pairing")

    def test_log_agrees_stays_pass(self, monkeypatch):
        row = self._pairing(monkeypatch, -1)
        assert row.severity.name == "PASS" and "does not agree" not in row.message

    def test_log_disagrees_is_warn(self, monkeypatch):
        row = self._pairing(monkeypatch, 0)
        assert row.severity.name == "WARN" and "does not agree" in row.message
        assert row.values["camera_log_err_s"] > 0.015


class TestCameraLogMustMakeSense:
    """A camera-log PASS that can't be right physically is a WARN."""

    def _row(self, monkeypatch, k_log, start_k=None):
        import core.pipeline as pl
        from core.qc import QCReport
        t = _pulses(500)
        fr = _frames(lambda o: t[o - 1 + k_log] if 0 <= o - 1 + k_log < len(t) else None)
        monkeypatch.setattr(cl, "load", lambda p: cl.CameraLog(frames={("R1", "Rear"): fr}))
        monkeypatch.setattr(pl.cal, "trigger_delay_s", lambda arms, c: 0.0)
        monkeypatch.setattr(cl, "from_start", lambda *a: None if start_k is None else
                            cl.OffsetResult(start_k, "camera start", True,
                                            "cameras were ready 2.0 s before the first "
                                            "trigger's picture - photo n = trigger n-1"))
        run = type("Run", (), {"run_id": "R1", "get": lambda self, k: "x.log"})()
        parsed = type("P", (), {"cameras": {"Rear": [0] * 450}, "lever_arms": None})()
        trg = type("T", (), {"utc_s": t})()
        report = QCReport("R1")
        pl._pairing(run, parsed, trg, report, None)
        return next(c for c in report.checks if c.check_id == "align.camera.Rear.pairing")

    def test_normal(self, monkeypatch):
        row = self._row(monkeypatch, -1, start_k=-1)
        assert row.values["method"] == "camera log" and row.severity.name == "PASS"

    def test_two_startup_frames_is_warn(self, monkeypatch):
        row = self._row(monkeypatch, -2)
        assert row.values["method"] == "camera log" and row.values["k"] == -2
        assert row.severity.name == "WARN" and "start-up frame" in row.message

    def test_start_disagrees_is_warn(self, monkeypatch):
        row = self._row(monkeypatch, 0, start_k=-1)
        assert row.values["method"] == "camera log" and row.severity.name == "WARN"
        assert "camera start says" in row.message

    def test_moving_start_is_fine(self, monkeypatch):
        # no confident start (cameras ready after the first pulses): k=+3 is normal
        row = self._row(monkeypatch, 3)
        assert row.severity.name == "PASS"


class TestRowsTellOneStory:
    """Report rows must not contradict each other (QC review 2026-11-17)."""

    def test_too_soon_row_names_the_photographed_pulse(self, monkeypatch):
        import core.pipeline as pl
        from core.qc import QCReport
        # the made-up shape above, but let the pipeline add the too-soon row
        rng = np.random.default_rng(3)
        x, out, short = 100.0, [], None
        missed = []
        for i in range(700):
            x += 0.45 + 0.1 * rng.random()
            out.append(x)
            if i == 39:
                x += 0.094
                out.append(x)
                short = len(out) - 1
            if i in (250, 480):
                x += 0.1 if i == 250 else 0.136
                out.append(x)
                missed.append(len(out) - 1)
        ts = np.array(out)
        real = np.delete(np.arange(len(ts)), missed)
        frames = [(o, float(ts[real[o - 2]]) - 500.0)
                  for o in range(100, 10_000, 100) if o - 2 < len(real)]
        monkeypatch.setattr(cl, "load", lambda p: cl.CameraLog(frames={("R1", "Rear"): frames}))
        monkeypatch.setattr(pl.cal, "trigger_delay_s", lambda arms, c: 0.0)
        run = type("Run", (), {"run_id": "R1", "get": lambda self, k: "x.log"})()
        parsed = type("P", (), {"cameras": {"Rear": [0] * (len(real) + 1)},
                                "lever_arms": None})()
        trg = type("T", (), {"utc_s": ts})()
        report = QCReport("R1")
        usable = pl._camera_usable_triggers(parsed, trg, report)
        pl._pairing(run, parsed, trg, report, usable)
        row = next(c for c in report.checks if c.check_id == "align.trigger_too_soon")
        assert "EXCEPT Rear" in row.message and f"trigger {short + 1}" in row.message
        assert row.values["photographed_by_camera"] == {"Rear": [short]}

    def test_a_rejected_fix_leaves_no_passed_over(self, monkeypatch):
        import core.pipeline as pl
        from core.qc import QCReport, _passed_over
        t = _pulses(300)
        calls = []

        def decide(*a, **kw):
            calls.append(1)
            if len(calls) == 2:        # after the fix: the log disagrees
                return cl.OffsetResult(0, "camera log", True, "camera log: n+0")
            return cl.OffsetResult(-1, "assumed", False, "pairing NOT confirmed")
        monkeypatch.setattr(cl, "decide", decide)
        monkeypatch.setattr(cl, "load", lambda p: cl.CameraLog())
        monkeypatch.setattr(pl.cal, "trigger_delay_s", lambda arms, c: 0.0)
        monkeypatch.setattr(pl, "_missing_images", lambda *a, **kw: (
            (np.delete(np.arange(len(t)), [50, 60]), -1, [(2, 99)]),
            ("2 pulse(s) took no photo", {"passed_over": [50, 60], "spans": [(2, 99)]})))
        run = type("Run", (), {"run_id": "R1", "get": lambda self, k: "x.log"})()
        parsed = type("P", (), {"cameras": {"Rear": [0] * 290}, "lever_arms": None})()
        trg = type("T", (), {"utc_s": t})()
        report = QCReport("R1")
        lags, by_cam, spans = pl._pairing(run, parsed, trg, report, None)
        rows = [c for c in report.checks if c.check_id == "align.camera.Rear.missing_images"]
        assert len(rows) == 1 and "did not agree afterwards" in rows[0].message
        assert _passed_over(report, "Rear") == 0 and by_cam == {} and spans == {}


class TestEchoMendsBackwardPulses:
    """A pulse stamped backwards in eventOutA has a good time in its echo,
    eventInA.txt (same row, ~0.1 ms later). 20261030.092846 row 1: eventOutA
    7.5 s early, echo 2.6 s after the pulse before - a normal, photographed
    pulse. Dropping it shifted photos 2-43 by one pulse."""

    def _files(self, tmp_path, out, echo):
        _write(tmp_path / "eventOutA.txt", out)
        if echo is not None:
            _write(tmp_path / "eventInA.txt", echo)
        return tmp_path / "eventOutA.txt"

    def test_normal_pulse_kept_with_echo_time(self, tmp_path):
        good = [1_000_000 + 2_000_000 * k for k in range(20)]
        out = list(good)
        out[1] = good[0] - 5_000_000                 # 092846 row 1's shape
        echo = [v + 109 for v in good]
        ts, _, dropped, repaired = parse_event_triggers(
            self._files(tmp_path, out, echo), drop_backwards=True, with_repaired=True)
        assert dropped == [] and repaired == [1]
        assert ts.tolist() == good                   # echo minus its steady lag

    def test_burst_pulse_kept_for_the_too_soon_rule(self, tmp_path):
        from core.alignment import camera_trigger_indices
        good = [1_000_000 + 500_000 * k for k in range(10)]
        burst = [good[4] + 5_000 * j for j in range(1, 4)]        # 5 ms burst
        true = sorted(good + burst)
        out = list(true)
        out[6] = true[5] - 3_000_000                 # one burst pulse stamped backwards
        echo = [v + 400 for v in true]
        ts, _, dropped, repaired = parse_event_triggers(
            self._files(tmp_path, out, echo), drop_backwards=True, with_repaired=True)
        assert repaired == [6] and ts.tolist() == true
        usable, skipped = camera_trigger_indices(ts / 1e6)
        assert [i for i, _ in skipped] == [5, 6, 7]  # the burst gets no photo

    def test_no_echo_drops_it(self, tmp_path):
        good = [1_000_000 + 2_000_000 * k for k in range(20)]
        out = list(good)
        out[1] = good[0] - 5_000_000
        ts, _, dropped, repaired = parse_event_triggers(
            self._files(tmp_path, out, None), drop_backwards=True, with_repaired=True)
        assert dropped == [1] and repaired == []

    def test_echo_with_other_row_count_not_used(self, tmp_path):
        good = [1_000_000 + 2_000_000 * k for k in range(20)]
        out = list(good)
        out[1] = good[0] - 5_000_000
        ts, _, dropped, repaired = parse_event_triggers(
            self._files(tmp_path, out, [v + 109 for v in good[:-1]]),
            drop_backwards=True, with_repaired=True)
        assert dropped == [1] and repaired == []
