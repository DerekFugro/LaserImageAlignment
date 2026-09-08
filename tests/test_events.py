"""Qinertia Events-output.txt: parsing, trust checks, trigger recovery."""
from datetime import datetime, timezone

import numpy as np
import pytest

from core.events import (EventTable, find_events_file, leading_trigger_utc_s,
                         utc_to_sbg_us)
from core.formats import ParseError
from tests.conftest import SBG0_US, T0_UTC

ANCHOR = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

HEAD = ("5.1.2457-stable\nproj\nColumn #,\tUnit,\tDescription:\n\n"
        "GPS Time\t    UTC Time\tRoll\tPitch\tYaw\tRoll Std.\tLatitude\t"
        "Longitude\tAltitude MSL\tTimestamp\tIdentification\tNumber\n"
        "(S)\t(HH:MM:SS.SS\t(°)\t(°)\t(°)\t(°)\t(°)\t"
        "(°)\t(m)\t(ms)\t(string)\t(int)\n")


def write_events(path, utc_epochs, lat0=43.4363, ident=True, start_num=0):
    rows = []
    for k, e in enumerate(utc_epochs):
        dt = datetime.fromtimestamp(e, tz=timezone.utc)
        tod = dt.strftime("%H:%M:%S") + f".{int(round(dt.microsecond / 1000)):03d}"
        lat = lat0 + k * 1e-5
        ids = (f"Event_A_{start_num + k}", str(start_num + k)) if ident else ("N/A", "N/A")
        rows.append("\t".join([
            f"{e % 86400 + 18:.3f}", tod, "+0.100", "-0.200", "-16.000", "0.006",
            f"{lat:.9f}", "-80.315200", "265.000", str(k * 1000), ids[0], ids[1]]))
    path.write_text(HEAD + "\n".join(rows) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ parsing

def test_parse_reads_events_and_attitude(tmp_path):
    base = datetime(2026, 8, 16, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    p = write_events(tmp_path / "Events-output.txt", [base, base + 0.75, base + 1.5])
    ev = EventTable.parse(p, anchor_utc_date=ANCHOR)
    assert len(ev) == 3
    assert ev.epoch_s[0] == pytest.approx(base, abs=1e-3)
    assert ev.epoch_s[2] == pytest.approx(base + 1.5, abs=1e-3)
    assert list(ev.number) == [0, 1, 2]
    assert ev.roll_deg[0] == pytest.approx(0.1)
    assert ev.pitch_deg[0] == pytest.approx(-0.2)     # exists nowhere else per-image
    assert ev.yaw_deg[0] == pytest.approx(-16.0)


def test_parse_rejects_a_time_mode_export_with_a_useful_message(tmp_path):
    base = datetime(2026, 8, 16, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    p = write_events(tmp_path / "Events-output.txt", [base, base + 0.02], ident=False)
    with pytest.raises(ParseError) as exc:
        EventTable.parse(p, anchor_utc_date=ANCHOR)
    assert "time-mode export" in str(exc.value)


def test_parse_handles_midnight_rollover(tmp_path):
    day = datetime(2026, 8, 16, tzinfo=timezone.utc).timestamp()
    p = write_events(tmp_path / "Events-output.txt",
                     [day + 86399.0, day + 86399.5, day + 86400.5])
    ev = EventTable.parse(p, anchor_utc_date=ANCHOR)
    assert np.all(np.diff(ev.epoch_s) > 0)
    assert ev.epoch_s[2] - ev.epoch_s[0] == pytest.approx(1.5, abs=1e-3)


def test_find_events_file(tmp_path):
    exp = tmp_path / "SBGData" / "sess" / "export"
    exp.mkdir(parents=True)
    assert find_events_file(tmp_path) is None
    p = write_events(exp / "Events-output.txt", [T0_UTC])
    assert find_events_file(tmp_path) == p


# ----------------------------------------------------------------- nearest

def test_nearest_respects_the_tolerance(tmp_path):
    base = datetime(2026, 8, 16, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    p = write_events(tmp_path / "Events-output.txt", [base, base + 0.75])
    ev = EventTable.parse(p, anchor_utc_date=ANCHOR)
    assert list(ev.nearest([base + 0.001, base + 0.75])) == [0, 1]
    assert list(ev.nearest([base + 0.2])) == [-1]      # 200 ms away: no match


# ------------------------------------------------------- trigger recovery

def _table(tmp_path, n=10, step=0.75, base=None):
    base = base if base is not None else \
        datetime(2026, 8, 16, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    times = [base + k * step for k in range(n)]
    p = write_events(tmp_path / "Events-output.txt", times)
    return EventTable.parse(p, anchor_utc_date=ANCHOR), np.array(times)


def test_recovers_the_trigger_before_the_logged_ones(tmp_path):
    ev, times = _table(tmp_path)
    logged = times[3:]                       # ACS started late by three triggers
    got, note = leading_trigger_utc_s(ev, logged, n_missing=1)
    assert len(got) == 1
    assert got[0] == pytest.approx(times[2], abs=1e-3)
    assert "recovered 1" in note


def test_recovers_several_when_several_are_missing(tmp_path):
    ev, times = _table(tmp_path)
    got, _ = leading_trigger_utc_s(ev, times[4:], n_missing=3)
    assert got == pytest.approx(times[1:4], abs=1e-3)


def test_refuses_when_a_logged_trigger_has_no_event(tmp_path):
    ev, times = _table(tmp_path)
    logged = np.concatenate([times[3:], [times[-1] + 5.0]])   # a trigger not in the file
    got, note = leading_trigger_utc_s(ev, logged, n_missing=1)
    assert len(got) == 0 and "does not cover this run" in note


def test_refuses_when_the_events_are_not_consecutive(tmp_path):
    ev, times = _table(tmp_path)
    logged = times[[3, 5, 6, 7]]             # a gap: the trains disagree
    got, note = leading_trigger_utc_s(ev, logged, n_missing=1)
    assert len(got) == 0 and "consecutive" in note


def test_refuses_when_the_file_starts_at_the_run(tmp_path):
    ev, times = _table(tmp_path)
    got, note = leading_trigger_utc_s(ev, times, n_missing=1)
    assert len(got) == 0 and "precede this run" in note


def test_no_missing_triggers_is_a_noop(tmp_path):
    ev, times = _table(tmp_path)
    got, note = leading_trigger_utc_s(ev, times[3:], n_missing=0)
    assert len(got) == 0 and note == "no missing triggers"


# -------------------------------------------------------------- clock join

def test_utc_to_sbg_us_inverts_the_clock_map(tmp_path):
    from core.formats import UtcTable
    n = 20
    utc = UtcTable(sbg_us=np.arange(n, dtype=np.int64) * 1_000_000 + SBG0_US,
                   epoch_s=T0_UTC + np.arange(n, dtype=float))
    want = T0_UTC + np.array([2.5, 7.25])
    back = utc_to_sbg_us(utc, want)
    assert utc.sbg_to_utc(back) == pytest.approx(want, abs=1e-6)


def test_utc_to_sbg_us_extrapolates_before_the_table(tmp_path):
    """The recovered trigger fires BEFORE utcTime.txt starts — extrapolation
    is the whole point, so it has to stay linear and exact."""
    from core.formats import UtcTable
    n = 20
    utc = UtcTable(sbg_us=np.arange(n, dtype=np.int64) * 1_000_000 + SBG0_US,
                   epoch_s=T0_UTC + np.arange(n, dtype=float))
    want = T0_UTC - np.array([5.0, 12.0])
    back = utc_to_sbg_us(utc, want)
    assert utc.sbg_to_utc(back) == pytest.approx(want, abs=1e-6)
    assert np.all(back < utc.sbg_us[0])


# ------------------------------------------------------ end to end in a run

def add_events_to_run(root, synth, extra=1):
    """Give a synthetic run the events file it was missing: every logged
    trigger, plus `extra` that fired before ACS started.

    The extra ones are placed by DISTANCE, not by time — the camera fires
    every 0.75 m and the cart is still accelerating at the start of a run, so
    a constant time step would put them in the wrong place.
    """
    exp = next(root.rglob("export"))
    trig = synth["trig_utc"]
    before_d = [synth["trig_d"][0] - 0.75 * k for k in range(extra, 0, -1)]
    before = list(np.interp(before_d, synth["d"], synth["utc"]))
    return write_events(exp / "Events-output.txt", before + list(trig))


def _check(res, check_id):
    hit = next((c for c in res.report.checks if c.check_id == check_id), None)
    assert hit is not None, f"no QC check {check_id!r} was recorded"
    return hit


class TestRunRecoversItsFirstImage:
    def test_without_the_file_the_first_image_is_unplaced(self, synth_run_root,
                                                          synth_cal_dir):
        from core.discovery import discover_runs
        from core.pipeline import process_run
        res = process_run(discover_runs(synth_run_root,
                                        calibrations_dir=synth_cal_dir)[0])
        al = res.camera_alignments["Rear"]
        assert not np.isfinite(al.utc_s[0])          # the state we are fixing
        assert np.isnan(al.lat_deg[0])

    def test_with_the_file_every_image_gets_a_position(self, synth_run_root,
                                                       synth_cal_dir, synth):
        from core.discovery import discover_runs
        from core.pipeline import process_run
        add_events_to_run(synth_run_root, synth)
        res = process_run(discover_runs(synth_run_root,
                                        calibrations_dir=synth_cal_dir)[0])
        al = res.camera_alignments["Rear"]
        assert np.all(np.isfinite(al.utc_s)), "every image should now have a time"
        assert np.all(np.isfinite(al.lat_deg))
        assert res.match.n_unmatched == 0
        assert "recovered 1" in _check(res, "align.pre_collection_triggers").message

    def test_the_recovered_image_lands_one_step_before_the_next(self, synth_run_root,
                                                                synth_cal_dir, synth):
        from core.discovery import discover_runs
        from core.pipeline import process_run
        add_events_to_run(synth_run_root, synth)
        res = process_run(discover_runs(synth_run_root,
                                        calibrations_dir=synth_cal_dir)[0])
        al = res.camera_alignments["Rear"]
        step = al.dmi_dist_m[2] - al.dmi_dist_m[1]
        assert al.dmi_dist_m[1] - al.dmi_dist_m[0] == pytest.approx(step, rel=0.05)

    def test_a_file_that_does_not_line_up_is_refused_not_guessed(self, synth_run_root,
                                                                 synth_cal_dir, synth):
        from core.discovery import discover_runs
        from core.pipeline import process_run
        exp = next(synth_run_root.rglob("export"))
        # events from a different session entirely: nothing must be recovered
        write_events(exp / "Events-output.txt",
                     [synth["trig_utc"][0] + 3600 + k * 0.75 for k in range(20)])
        res = process_run(discover_runs(synth_run_root,
                                        calibrations_dir=synth_cal_dir)[0])
        al = res.camera_alignments["Rear"]
        assert not np.isfinite(al.utc_s[0])          # unchanged, not invented
        chk = _check(res, "align.pre_collection_triggers")
        assert chk.severity.name == "WARN"
        assert "does not cover this run" in chk.message


def test_batch_sets_aside_the_before_section_images(synth_run_root, synth,
                                                    synth_cal_dir, tmp_path):
    """End to end: recovered leading images that fall before the section
    start must end up in BeforeCollection/, not in the camera folder."""
    from core.batch import process_collection
    from core.discovery import OverrideStore
    from core.rename import BEFORE_DIR
    root = synth_run_root
    day = root / "20260816"
    day.mkdir()
    # section starts 5 m into the run, so the first images fall before it
    lat_5m = 43.4363 + 5.0 / 111320.0
    (day / "Daily_ARAN104_20260816.csv").write_text(
        "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed\n"
        f"Video [20260816.150000] run,200180,North,0.000,0.700,{lat_5m:.9f},"
        "-80.315200,20\n", encoding="utf-8-sig")
    add_events_to_run(root, synth)
    store = OverrideStore(tmp_path / "ov.json")
    report = process_collection(root, calibrations_dir=synth_cal_dir,
                                overrides=store)
    o = next(x for x in report.outcomes if x.run_id == "20260816.150000")
    rear = root / "Images" / "20260816.150000" / "Rear"
    assert any(n.startswith("BEFORE Rear:") for n in o.notes), o.notes
    aside = list((rear / BEFORE_DIR).glob("*.jpg"))
    assert aside, "images before the section start should be set aside"
    assert not list(rear.glob("-*.jpg")), "none may remain in the camera folder"


def test_a_rerun_after_set_aside_still_matches_every_image(synth_run_root, synth,
                                                           synth_cal_dir, tmp_path):
    """Regression: filing the before-section images away leaves the camera
    folder with fewer images than triggers. The matcher anchors on the tail so
    the pairing is still right, but its sanity limit saw an unexplained -4 and
    refused the whole run — six runs of 20260824 failed that way."""
    from core.batch import process_collection
    from core.discovery import OverrideStore
    from core.rename import BEFORE_DIR
    root = synth_run_root
    day = root / "20260816"
    day.mkdir()
    lat_5m = 43.4363 + 5.0 / 111320.0
    (day / "Daily_ARAN104_20260816.csv").write_text(
        "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed\n"
        f"Video [20260816.150000] run,200180,North,0.000,0.700,{lat_5m:.9f},"
        "-80.315200,20\n", encoding="utf-8-sig")
    add_events_to_run(root, synth)
    store = OverrideStore(tmp_path / "ov.json")
    process_collection(root, calibrations_dir=synth_cal_dir, overrides=store)
    rear = root / "Images" / "20260816.150000" / "Rear"
    assert list((rear / BEFORE_DIR).glob("*.jpg")), "setup: nothing was set aside"

    report = process_collection(root, calibrations_dir=synth_cal_dir, overrides=store)
    o = next(x for x in report.outcomes if x.run_id == "20260816.150000")
    assert o.status in ("written", "partial"), o.failures
    assert not any("count difference" in f for f in o.failures), o.failures
    assert o.result.camera_matches["Rear"].n_unmatched == 0
