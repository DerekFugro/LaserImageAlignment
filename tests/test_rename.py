"""Rename-to-section-distance: naming, Daily parsing, distances, manifest, undo."""
import csv
from types import SimpleNamespace

import numpy as np
import pytest

from core.daily import DailyEntry, find_daily_file, parse_daily
from core.rename import (MANIFEST_NAME, rename_camera_folder,
                         section_distance_mm, station_name, undo_renames)


# ---------------------------------------------------------------- station_name

def test_station_name_keeps_the_original_convention():
    # 12 zero-padded digits of millimetres — indistinguishable in form from
    # the ACS name it replaces. That is the point.
    assert station_name(749) == "000000000749.jpg"
    assert station_name(1554.4) == "000000001554.jpg"


def test_station_name_matches_the_width_it_is_given():
    assert station_name(750, digits=9) == "000000750.jpg"


def test_station_name_signs_anything_before_the_section_start():
    # a real measurement in the other direction, said plainly
    assert station_name(-178) == "-000000000178.jpg"
    assert station_name(-178, digits=9) == "-000000178.jpg"


def test_station_name_refuses_what_it_cannot_write():
    assert station_name(1234, digits=3) is None        # wider than the field


# ---------------------------------------------------------------- parse_daily

DAILY_HEADER = ("FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed\n")


def _write_daily(tmp_path, rows):
    day = tmp_path / "20260824"
    day.mkdir(exist_ok=True)
    p = day / "Daily_ARAN104_20260824.csv"
    p.write_text(DAILY_HEADER + "".join(rows), encoding="utf-8-sig")
    return p


def test_parse_daily_keys_by_stamp_and_reads_columns(tmp_path):
    p = _write_daily(tmp_path, [
        "Video [20260824.101724] run,200180,North,12.400,13.100,45.100000,-75.500000,22\n",
        ",,,,,,,\n",  # blank spacer row ACS writes
        "Video [20260824.101313] run,201170,South,13.100,12.400,45.106000,-75.501000,21\n",
    ])
    d = parse_daily(p)
    assert set(d) == {"20260824.101724", "20260824.101313"}
    e = d["20260824.101724"]
    assert e.section == "200180" and e.ascending
    assert e.from_km == pytest.approx(12.400)
    r = d["20260824.101313"]
    assert not r.ascending          # To < From: chainage runs down the section
    assert r.lat_beg == pytest.approx(45.106)


def test_parse_daily_skips_bad_rows(tmp_path):
    p = _write_daily(tmp_path, [
        "no stamp here,200180,N,1,2,45,-75,0\n",
        "Video [20260824.101724] run,200180,N,notanumber,2,45,-75,0\n",
    ])
    assert parse_daily(p) == {}


def test_find_daily_file(tmp_path):
    p = _write_daily(tmp_path, [])
    assert find_daily_file(tmp_path) == p
    assert find_daily_file(tmp_path / "nope") is None


# --------------------------------------------------------- section_distance_mm

def _straight_north_nav(lat0=45.0, lon0=-75.0, n=101, step_m=1.0):
    """1 m/s due north; along_m == seconds elapsed."""
    lat = lat0 + np.arange(n) * step_m / 111320.0
    nav = SimpleNamespace(
        lat_deg=lat,
        lon_deg=np.full(n, lon0),
        along_m=np.arange(n, dtype=float) * step_m,
        epoch_s=np.arange(n, dtype=float),
    )
    nav.dist_at = lambda t, nav=nav: np.interp(
        np.asarray(t, dtype=float), nav.epoch_s, nav.along_m)
    return nav


def test_distance_counts_from_the_section_start(tmp_path):
    nav = _straight_north_nav()
    # section start = the nav point 10 m in
    entry = DailyEntry("r", "200180", "N", 12.400, 13.100,
                       lat_beg=float(nav.lat_deg[10]), lon_beg=-75.0)
    al = SimpleNamespace(utc_s=np.array([10.0, 20.0, np.nan]))
    mm = section_distance_mm(al, nav, entry, arm_m=None)
    assert mm[0] == pytest.approx(0, abs=1)
    assert mm[1] == pytest.approx(10_000, abs=1)
    assert np.isnan(mm[2])


def test_distance_counts_up_on_a_descending_section_too(tmp_path):
    """LRS chainage may run down the section; distance INTO it still counts up,
    which is what the old odometer filename meant as well."""
    nav = _straight_north_nav()
    entry = DailyEntry("r", "201170", "S", 13.100, 12.400,
                       lat_beg=float(nav.lat_deg[10]), lon_beg=-75.0)
    # a real run's images START at the section start and go on from there
    al = SimpleNamespace(utc_s=np.array([10.0, 30.0]))
    # cart at 30 m; footprint 0.928 m behind -> 29.072 m; 19.072 m past start
    mm = section_distance_mm(al, nav, entry, arm_m=0.928)
    assert mm[1] == pytest.approx(19_072, abs=1)


def test_distance_anchor_uses_this_runs_pass_not_the_whole_session():
    """Back-and-forth session: the cart passes the section start many times.
    The anchor must come from THIS run's pass, not a global nearest point."""
    # out 100 m north (t 0-100), back south (t 100-200), out again (t 200-300)
    t = np.arange(301, dtype=float)
    d = np.concatenate([np.arange(101.0), 100 - np.arange(1, 101.0),
                        np.arange(1, 101.0)])
    lat = 45.0 + d / 111320.0
    nav = SimpleNamespace(lat_deg=lat, lon_deg=np.full(301, -75.0),
                          along_m=t.copy(), epoch_s=t)
    nav.dist_at = lambda tt, nav=nav: np.interp(
        np.asarray(tt, dtype=float), nav.epoch_s, nav.along_m)
    entry = DailyEntry("r", "200180", "N", 12.400, 13.100,
                       lat_beg=float(45.0 + 10.0 / 111320.0), lon_beg=-75.0)
    # images during the THIRD pass: the run starts at its 10 m point (t=210)
    # and runs on to 20 m (t=220). The anchor is never searched before a run's
    # first trigger, so the run has to actually contain its section start.
    al = SimpleNamespace(utc_s=np.array([210.0, 220.0]))
    mm = section_distance_mm(al, nav, entry, arm_m=None)
    # anchor must be this pass's 10 m point (along_m=210), NOT pass one or two
    assert mm[0] == pytest.approx(0, abs=1)
    assert mm[1] == pytest.approx(10_000, abs=1)


def test_distance_refuses_a_bad_anchor():
    nav = _straight_north_nav()
    # section start nowhere near the trajectory (~1.1 km east)
    entry = DailyEntry("r", "200180", "N", 12.4, 13.1,
                       lat_beg=45.0, lon_beg=-75.01)
    al = SimpleNamespace(utc_s=np.array([10.0, 20.0]))
    assert np.all(np.isnan(section_distance_mm(al, nav, entry, arm_m=None)))


# ------------------------------------------------------- rename + undo

def _folder_with(tmp_path, names):
    d = tmp_path / "Rear"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"jpeg")
    return d


ENTRY = DailyEntry("20260824.101724", "200180", "N", 12.4, 13.1, 45.0, -75.0)
A, B = "000000000750.jpg", "000000001500.jpg"


def test_rename_writes_manifest_then_renames(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    out = rename_camera_folder(d, [A, B], np.array([749.0, 1554.0]),
                               ENTRY, ENTRY.run_id, "Rear")
    assert out.renamed == 2 and out.skipped == 0 and not out.errors
    assert (d / "000000000749.jpg").exists()
    assert (d / "000000001554.jpg").exists()
    assert not (d / A).exists()
    with open(d / MANIFEST_NAME, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["original_name"] == A
    assert rows[0]["new_name"] == "000000000749.jpg"
    assert rows[0]["distance_mm"] == "749"
    assert rows[0]["section"] == "200180"


def test_new_name_may_be_an_old_name_and_nothing_is_clobbered(tmp_path):
    """The real hazard of keeping the format: the distance name of one image
    can be the odometer name of another. Both files must survive."""
    d = _folder_with(tmp_path, [A, B])
    # 000000001500 -> 000000000750, the name A currently holds; A -> something else
    out = rename_camera_folder(d, [A, B], np.array([10.0, 750.0]),
                               ENTRY, ENTRY.run_id, "Rear")
    assert out.renamed == 2 and not out.errors
    assert (d / "000000000010.jpg").exists()
    assert (d / "000000000750.jpg").exists()
    assert len(list(d.glob("*.jpg"))) == 2


def test_rename_skips_unplaced_but_signs_before_the_start(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    out = rename_camera_folder(d, [A, B], np.array([np.nan, -200.0]),
                               ENTRY, ENTRY.run_id, "Rear")
    assert out.renamed == 1 and out.skipped == 1
    assert (d / A).exists()                           # no position: left alone
    assert (d / "-000000000200.jpg").exists()         # before the start: signed
    with open(d / MANIFEST_NAME, newline="") as fh:
        rows = list(csv.DictReader(fh))
    notes = [r["note"] for r in rows]
    assert any(n.startswith("no position") for n in notes)
    assert "-200" in [r["distance_mm"] for r in rows]


def test_a_signed_name_survives_a_second_pass_and_an_undo(tmp_path):
    d = _folder_with(tmp_path, [A])
    rename_camera_folder(d, [A], np.array([-200.0]), ENTRY, ENTRY.run_id, "Rear")
    out2 = rename_camera_folder(d, ["-000000000200.jpg"], np.array([-200.0]),
                                ENTRY, ENTRY.run_id, "Rear")
    assert out2.renamed == 1 and not out2.errors      # already correct: no-op
    assert (d / "-000000000200.jpg").exists()
    undone, problems = undo_renames(d)
    assert undone == 1 and not problems and (d / A).exists()


def test_rename_is_idempotent(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    mm = np.array([749.0, 1554.0])
    rename_camera_folder(d, [A, B], mm, ENTRY, ENTRY.run_id, "Rear")
    after = sorted(p.name for p in d.glob("*.jpg"))
    # a second batch re-scans the folder: same distances, names already right
    out2 = rename_camera_folder(d, after, mm, ENTRY, ENTRY.run_id, "Rear")
    assert out2.renamed == 2 and not out2.errors
    assert sorted(p.name for p in d.glob("*.jpg")) == after


def test_two_images_wanting_one_name_is_an_error_not_a_loss(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    out = rename_camera_folder(d, [A, B], np.array([500.0, 500.0]),
                               ENTRY, ENTRY.run_id, "Rear")
    assert out.errors and out.renamed == 1 and out.skipped == 1
    assert len(list(d.glob("*.jpg"))) == 2            # nothing vanished


def test_dry_run_touches_nothing(tmp_path):
    d = _folder_with(tmp_path, [A])
    out = rename_camera_folder(d, [A], np.array([749.0]), ENTRY,
                               ENTRY.run_id, "Rear", dry_run=True)
    assert out.renamed == 1
    assert (d / A).exists()
    assert not (d / "000000000749.jpg").exists()
    assert not (d / MANIFEST_NAME).exists()


def test_undo_round_trip(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([10.0, 750.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    undone, problems = undo_renames(d)
    assert undone == 2 and not problems
    assert (d / A).exists() and (d / B).exists()
    assert not (d / MANIFEST_NAME).exists()
    assert (d / (MANIFEST_NAME + ".undone")).exists()   # record kept
    assert not list(d.glob("*.__renaming__"))


def test_undo_without_manifest_is_a_noop(tmp_path):
    d = _folder_with(tmp_path, [A])
    undone, problems = undo_renames(d)
    assert undone == 0 and problems


# ------------------------------------------- ImageSet.scan on a renamed folder

def test_imageset_scan_reads_manifest_counters(tmp_path):
    """After renaming, the names carry distances, not odometer counters —
    and they look identical. Matching MUST come from the manifest."""
    from core.formats import ImageSet
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([1554.0, 749.0]),  # order flips
                         ENTRY, ENTRY.run_id, "Rear")
    s = ImageSet.scan(d)
    assert list(s.counter_mm) == [750, 1500]          # original counters
    assert s.files == ["000000001554.jpg", "000000000749.jpg"]


# --------------------------------------------- batch always renames

def test_process_collection_always_renames(synth_run_root, synth_cal_dir, tmp_path):
    from core.batch import process_collection
    from core.discovery import OverrideStore
    root = synth_run_root
    day = root / "20260816"
    day.mkdir()
    (day / "Daily_ARAN104_20260816.csv").write_text(
        DAILY_HEADER +
        "Video [20260816.150000] run,200180,North,0.000,0.700,43.436300,-80.315200,20\n",
        encoding="utf-8-sig")
    store = OverrideStore(tmp_path / "ov.json")
    report = process_collection(root, calibrations_dir=synth_cal_dir,
                                overrides=store)
    o = next(x for x in report.outcomes if x.run_id == "20260816.150000")
    assert o.status in ("written", "partial")
    assert any(n.startswith("RENAME Rear:") for n in o.notes)
    assert any(n.startswith("RENAME ROW:") for n in o.notes)
    rear = root / "Images" / "20260816.150000" / "Rear"
    assert (rear / MANIFEST_NAME).exists()
    n_jpg = len(list(rear.glob("*.jpg")))
    with open(rear / MANIFEST_NAME, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert any(r["new_name"] for r in rows)           # something was renamed
    # nothing lost, and the app can still scan the folder afterwards
    from core.formats import ImageSet
    assert len(ImageSet.scan(rear)) == n_jpg
    assert not list(rear.glob("*.__renaming__"))


def test_batch_without_a_daily_file_leaves_names_alone(synth_run_root,
                                                       synth_cal_dir, tmp_path):
    """No Daily file, no section start — the batch must say so and rename
    nothing, rather than inventing an anchor."""
    from core.batch import process_collection
    from core.discovery import OverrideStore
    root = synth_run_root
    before = sorted(p.name for p in
                    (root / "Images" / "20260816.150000" / "Rear").glob("*.jpg"))
    store = OverrideStore(tmp_path / "ov.json")
    report = process_collection(root, calibrations_dir=synth_cal_dir,
                                overrides=store)
    o = next(x for x in report.outcomes if x.run_id == "20260816.150000")
    assert any("RENAME skipped" in n for n in o.notes)
    rear = root / "Images" / "20260816.150000" / "Rear"
    assert sorted(p.name for p in rear.glob("*.jpg")) == before
    assert not (rear / MANIFEST_NAME).exists()


def test_anchor_does_not_move_when_a_leading_image_is_recovered():
    """Regression. Recovering the pre-collection trigger adds one image at the
    FRONT of a run. That must not shift any other image's distance — when it
    did (an anchor picked from GPS jitter while parked), every filename in the
    run moved 390 mm on 20260824."""
    nav = _straight_north_nav()
    entry = DailyEntry("r", "200180", "N", 12.4, 13.1,
                       lat_beg=float(nav.lat_deg[10]), lon_beg=-75.0)
    later = np.array([10.0, 20.0, 30.0])
    without = section_distance_mm(SimpleNamespace(utc_s=later), nav, entry, None)
    with_lead = section_distance_mm(
        SimpleNamespace(utc_s=np.concatenate([[9.0], later])), nav, entry, None)
    assert with_lead[1:] == pytest.approx(without, abs=1e-6)


def test_anchor_ignores_jitter_the_cart_made_while_parked():
    """The parking spot can sit within tens of metres of a section start, and
    a parked GPS jitters. None of that may win over the real approach."""
    n = 200
    d = np.concatenate([np.zeros(100), np.arange(1, 101, dtype=float)])
    jitter = np.concatenate([np.random.default_rng(0).normal(0, 0.02, 100),
                             np.zeros(100)])
    lat = 45.0 + (d + jitter) / 111320.0
    nav = SimpleNamespace(lat_deg=lat, lon_deg=np.full(n, -75.0),
                          along_m=d.copy(), epoch_s=np.arange(n, dtype=float))
    nav.dist_at = lambda t, nav=nav: np.interp(
        np.asarray(t, dtype=float), nav.epoch_s, nav.along_m)
    entry = DailyEntry("r", "200180", "N", 12.4, 13.1,
                       lat_beg=float(45.0 + 20.0 / 111320.0), lon_beg=-75.0)
    al = SimpleNamespace(utc_s=np.array([119.0, 129.0]))     # 20 m and 30 m
    mm = section_distance_mm(al, nav, entry, arm_m=None)
    assert mm[0] == pytest.approx(0, abs=50)
    assert mm[1] == pytest.approx(10_000, abs=50)


def test_a_daily_row_from_another_section_is_refused():
    nav = _straight_north_nav()
    entry = DailyEntry("r", "200180", "N", 12.4, 13.1,
                       lat_beg=45.0, lon_beg=-75.001)      # ~80 m off the path
    al = SimpleNamespace(utc_s=np.array([10.0, 20.0]))
    assert np.all(np.isnan(section_distance_mm(al, nav, entry, None)))


# ------------------------------------------------- BeforeCollection

from core.rename import BEFORE_DIR, move_before_collection  # noqa: E402


def test_negative_names_are_moved_out_of_the_camera_folder(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([-672.0, 56.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    n, problems = move_before_collection(d)
    assert n == 1 and not problems
    assert not (d / "-000000000672.jpg").exists()
    assert (d / BEFORE_DIR / "-000000000672.jpg").is_file()
    assert (d / "000000000056.jpg").is_file()          # inside the section: stays


def test_the_manifest_says_where_they_went(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([-672.0, 56.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    move_before_collection(d)
    with open(d / MANIFEST_NAME, newline="") as fh:
        rows = {r["new_name"]: r for r in csv.DictReader(fh)}
    assert BEFORE_DIR in rows["-000000000672.jpg"]["note"]
    assert rows["-000000000672.jpg"]["original_name"] == A   # lineage intact
    assert rows["000000000056.jpg"]["note"] == ""


def test_nothing_negative_means_no_folder_is_made(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([56.0, 806.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    n, problems = move_before_collection(d)
    assert n == 0 and not problems
    assert not (d / BEFORE_DIR).exists()


def test_moving_twice_is_a_noop(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([-672.0, 56.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    move_before_collection(d)
    n, problems = move_before_collection(d)
    assert n == 0 and not problems
    assert len(list((d / BEFORE_DIR).glob("*.jpg"))) == 1


def test_dry_run_moves_nothing(tmp_path):
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([-672.0, 56.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    n, _ = move_before_collection(d, dry_run=True)
    assert n == 1
    assert (d / "-000000000672.jpg").is_file()
    assert not (d / BEFORE_DIR).exists()


def test_scan_ignores_the_set_aside_images(tmp_path):
    """ImageSet.scan must not see BeforeCollection/ as images, and must not
    trip over it as a directory entry either."""
    from core.formats import ImageSet
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([-672.0, 56.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    move_before_collection(d)
    s = ImageSet.scan(d)
    assert s.files == ["000000000056.jpg"]
    assert list(s.counter_mm) == [1500]


def test_undo_reaches_into_before_collection(tmp_path):
    """A set-aside image still belongs to the run — undo must bring it back,
    not report it missing and strand it under a name nothing understands."""
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([-672.0, 56.0]),
                         ENTRY, ENTRY.run_id, "Rear")
    move_before_collection(d)
    undone, problems = undo_renames(d)
    assert undone == 2 and not problems
    assert (d / A).is_file() and (d / B).is_file()
    assert not (d / BEFORE_DIR).exists()          # emptied and removed


def test_anchor_survives_images_being_set_aside(tmp_path):
    """Regression: setting the before-section images aside shortens the list
    of images on disk. If the anchor window came from those images it would
    shift, renaming everything again and pushing yet more images out — a
    ratchet that moved 3 more images on the second pass over 20260824."""
    nav = _straight_north_nav()
    entry = DailyEntry("r", "200180", "N", 12.4, 13.1,
                       lat_beg=float(nav.lat_deg[20]), lon_beg=-75.0)
    triggers = SimpleNamespace(utc_s=np.arange(10.0, 41.0, 10.0))
    full = SimpleNamespace(utc_s=np.arange(10.0, 41.0, 10.0), triggers=triggers)
    before = section_distance_mm(full, nav, entry, None)
    # the first two images move to BeforeCollection; the triggers do not change
    after = section_distance_mm(
        SimpleNamespace(utc_s=full.utc_s[2:], triggers=triggers), nav, entry, None)
    assert after == pytest.approx(before[2:], abs=1e-6)


def test_the_anchor_never_looks_before_the_run_starts():
    """The minutes before a run are where the PPK solution is still settling —
    it accumulates metres of position noise while the cart stands still (8.05 m
    on 20260824). The anchor search must not be able to reach into it."""
    n = 200
    # parked for 100 samples, drifting sideways by up to 3 m of pure noise,
    # then the run drives cleanly past the section start
    rng = np.random.default_rng(1)
    parked = rng.normal(0, 3.0, 100) / 111320.0
    lat = np.concatenate([45.0 + parked,
                          45.0 + np.arange(1, 101, dtype=float) / 111320.0])
    nav = SimpleNamespace(lat_deg=lat, lon_deg=np.full(n, -75.0),
                          along_m=np.concatenate([np.zeros(100),
                                                  np.arange(1, 101, dtype=float)]),
                          epoch_s=np.arange(n, dtype=float))
    nav.dist_at = lambda t, nav=nav: np.interp(
        np.asarray(t, dtype=float), nav.epoch_s, nav.along_m)
    # the section starts 20 m along, which the run passes at t=119
    entry = DailyEntry("r", "200180", "N", 12.4, 13.1,
                       lat_beg=float(45.0 + 20.0 / 111320.0), lon_beg=-75.0)
    triggers = SimpleNamespace(utc_s=np.arange(119.0, 140.0))
    al = SimpleNamespace(utc_s=np.array([119.0, 129.0]), triggers=triggers)
    mm = section_distance_mm(al, nav, entry, arm_m=None)
    # if the parked noise were reachable it would anchor at along_m 0 and put
    # the first image ~20 m into the section instead of at its start
    assert mm[0] == pytest.approx(0, abs=100)
    assert mm[1] == pytest.approx(10_000, abs=100)


def test_an_unplaced_image_keeps_its_lineage_on_a_second_pass(tmp_path):
    """The regression. Pass 1 places both images and renames them. Pass 2
    cannot place them (a bad anchor, a different export - anything that
    returns NaN). The files keep their pass-1 names, so the manifest has to
    say so. It wrote a blank new_name instead: true_origins() skips blank
    rows, so ImageSet.scan read the section distances as odometer counters
    and undo_renames had nothing to put back."""
    from core.formats import ImageSet
    from core.rename import true_origins
    d = _folder_with(tmp_path, [A, B])
    rename_camera_folder(d, [A, B], np.array([100.0, 850.0]), ENTRY, ENTRY.run_id, "Rear")
    after = sorted(p.name for p in d.glob("*.jpg"))
    assert after == ["000000000100.jpg", "000000000850.jpg"]
    out2 = rename_camera_folder(d, after, np.array([np.nan, np.nan]),
                                ENTRY, ENTRY.run_id, "Rear")
    assert out2.skipped == 2
    assert sorted(p.name for p in d.glob("*.jpg")) == after      # nothing moved
    assert true_origins(d) == {"000000000100.jpg": A, "000000000850.jpg": B}
    assert list(ImageSet.scan(d).counter_mm) == [750, 1500]     # odometer, not distance
    undone, problems = undo_renames(d)
    assert undone == 2 and not problems
    assert (d / A).exists() and (d / B).exists()


def test_a_first_pass_unplaced_row_is_still_blank(tmp_path):
    """On the first pass the file IS on its original name, and a blank
    new_name is the truthful record of 'nothing happened to it'."""
    d = _folder_with(tmp_path, [A])
    rename_camera_folder(d, [A], np.array([np.nan]), ENTRY, ENTRY.run_id, "Rear")
    with open(d / MANIFEST_NAME, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["original_name"] == A and rows[0]["new_name"] == ""


def test_batch_rename_uses_the_arm_from_the_lever_arm_file(
        synth_run_root, synth_cal_dir, tmp_path):
    """The rename step must use the arm the EXIF was written from - the
    run's lever-arm file. It used to re-read the arm with no calibrations_dir
    and fall back to the built-in constant, so editing the file moved every
    stamped position but no filename (Derek's 20260821 batch: ROW names sat
    762 mm behind Rear while the file said 24 mm)."""
    from core.batch import process_collection
    from core.calibration import LEVER_ARMS_FILENAME
    from core.discovery import OverrideStore
    f = synth_cal_dir / LEVER_ARMS_FILENAME
    f.write_text(f.read_text(encoding="utf-8").replace("X  -1.69", "X  -1.00"),
                 encoding="utf-8")
    root = synth_run_root
    day = root / "20260816"
    day.mkdir()
    (day / "Daily_ARAN104_20260816.csv").write_text(
        DAILY_HEADER +
        "Video [20260816.150000] run,200180,North,0.000,0.700,43.436300,-80.315200,20\n",
        encoding="utf-8-sig")
    report = process_collection(root, calibrations_dir=synth_cal_dir,
                                overrides=OverrideStore(tmp_path / "ov.json"))
    o = report.outcomes[0]
    assert o.status == "written", o.failures
    imgs = root / "Images" / "20260816.150000"
    rear = {r["original_name"]: r for r in csv.DictReader(open(imgs / "Rear" / MANIFEST_NAME, newline=""))}
    row = {r["original_name"]: r for r in csv.DictReader(open(imgs / "ROW" / MANIFEST_NAME, newline=""))}
    diffs = {int(row[k]["distance_mm"]) - int(rear[k]["distance_mm"])
             for k in rear if k in row and rear[k]["distance_mm"] and row[k]["distance_mm"]}
    assert diffs, "no shared images to compare"
    # ROW is 1.00 m behind, Rear 0.928: names must differ by 72 mm, not 762
    assert all(abs(d + 72) <= 1 for d in diffs), diffs
