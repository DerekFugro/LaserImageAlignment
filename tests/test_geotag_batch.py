"""Geotagging, Gocator GPS injection, and batch processing.

These write to files, so every test works on tmp_path copies.
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path

import numpy as np
import pytest

piexif = pytest.importorskip("piexif")
cv2 = pytest.importorskip("cv2")

from core.batch import process_collection  # noqa: E402
from core.calibration import PAVE_CAM_BEHIND_LASER_M  # noqa: E402
from core.discovery import OverrideStore  # noqa: E402
from core.formats import GocatorIndex  # noqa: E402
from core.geotag import geotag_image, inject_gocator_gps, read_image_gps  # noqa: E402

LAT, LON, ALT = 43.4363281, -80.3152719, 265.125
UTC = 1787003805.25


def make_jpeg(path: Path, with_exif: bool = False):
    img = np.full((64, 64, 3), 128, dtype=np.uint8)
    cv2.imwrite(str(path), img)
    if with_exif:
        exif = {"0th": {piexif.ImageIFD.Software: "ARAN PhotoDeveloper",
                        piexif.ImageIFD.DateTime: "2026:08:17 17:56:05"},
                "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        piexif.insert(piexif.dump(exif), str(path))


class TestGeotagImage:
    def test_roundtrip_precision(self, tmp_path):
        p = tmp_path / "img.jpg"
        make_jpeg(p)
        geotag_image(p, LAT, LON, ALT, UTC, heading_deg=137.5, speed_ms=1.2)
        got = read_image_gps(p)
        assert got["lat_deg"] == pytest.approx(LAT, abs=1e-7)   # ~1 cm
        assert got["lon_deg"] == pytest.approx(LON, abs=1e-7)
        assert got["alt_m"] == pytest.approx(ALT, abs=0.01)
        assert got["heading_deg"] == pytest.approx(137.5, abs=0.01)

    def test_pixels_untouched(self, tmp_path):
        p = tmp_path / "img.jpg"
        make_jpeg(p)
        before = cv2.imread(str(p))
        geotag_image(p, LAT, LON, ALT, UTC)
        after = cv2.imread(str(p))
        assert np.array_equal(before, after)  # no re-encoding

    def test_preserves_existing_exif(self, tmp_path):
        p = tmp_path / "img.jpg"
        make_jpeg(p, with_exif=True)
        geotag_image(p, LAT, LON, ALT, UTC)
        exif = piexif.load(str(p))
        assert exif["0th"][piexif.ImageIFD.Software].decode().startswith("ARAN")
        assert read_image_gps(p) is not None

    def test_idempotent(self, tmp_path):
        p = tmp_path / "img.jpg"
        make_jpeg(p)
        geotag_image(p, LAT, LON, ALT, UTC)
        size1 = p.stat().st_size
        geotag_image(p, LAT + 0.0001, LON, ALT, UTC)   # re-run with new position
        assert p.stat().st_size == size1               # no tag duplication
        assert read_image_gps(p)["lat_deg"] == pytest.approx(LAT + 0.0001, abs=1e-7)

    def test_refuses_without_position(self, tmp_path):
        p = tmp_path / "img.jpg"
        make_jpeg(p)
        with pytest.raises(ValueError):
            geotag_image(p, float("nan"), LON, ALT, UTC)

    def test_no_temp_left_behind(self, tmp_path):
        p = tmp_path / "img.jpg"
        make_jpeg(p)
        geotag_image(p, LAT, LON, ALT, UTC)
        assert list(tmp_path.glob("*.tmp")) == []


class TestGocatorInjection:
    def make_csv(self, path: Path, n=5):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("frameIndex,timestamp,ptpTimestamp,encoder,numberOfProfilePoints,"
                     "numberOfValidPoints,bridgedValue,status,x0,z0,x1,z1\n")
            for i in range(1, n + 1):
                fh.write(f"{i},{i*1000},{1787003805000000+i*50000},{i*200},2,2,-1.0,0,"
                         f"-10.0,-1.{i},10.0,-1.{i}\n")

    def test_injects_and_reparses(self, tmp_path):
        p = tmp_path / "g.csv"
        self.make_csv(p, 5)
        lat = np.full(5, LAT); lon = np.full(5, LON); elev = np.full(5, ALT)
        assert inject_gocator_gps(p, lat, lon, elev) == 5
        header = p.read_text().splitlines()[0].split(",")
        assert header[8:11] == ["latitude", "longitude", "elevation"]
        assert header[11] == "x0"
        idx = GocatorIndex.build(p)          # parser handles the widened metadata
        assert idx.meta_cols == 11
        assert len(idx) == 5
        prof = idx.read_profile(0)
        assert prof.shape == (2, 2)
        assert prof[0, 0] == pytest.approx(-10.0)

    def test_idempotent_rerun(self, tmp_path):
        p = tmp_path / "g.csv"
        self.make_csv(p, 4)
        z = np.full(4, LAT), np.full(4, LON), np.full(4, ALT)
        inject_gocator_gps(p, *z)
        inject_gocator_gps(p, np.full(4, LAT + 0.001), np.full(4, LON), np.full(4, ALT))
        header = p.read_text().splitlines()[0]
        assert header.count("latitude") == 1     # not duplicated
        row = p.read_text().splitlines()[1].split(",")
        assert float(row[8]) == pytest.approx(LAT + 0.001, abs=1e-9)

    def test_nan_positions_written_blank(self, tmp_path):
        p = tmp_path / "g.csv"
        self.make_csv(p, 3)
        lat = np.array([LAT, np.nan, LAT]); lon = np.full(3, LON); elev = np.full(3, ALT)
        inject_gocator_gps(p, lat, lon, elev)
        rows = [l.split(",") for l in p.read_text().splitlines()[1:]]
        assert rows[1][8] == ""     # no fabricated position
        assert rows[0][8] != ""

    def test_blank_line_does_not_shift_positions(self, tmp_path):
        """A stray blank line is skipped by the indexer, so it must not move the
        position-array index either (it used to abort the whole injection)."""
        p = tmp_path / "g.csv"
        self.make_csv(p, 5)
        lines = p.read_text().splitlines()
        p.write_text("\n".join(lines[:3] + [""] + lines[3:]) + "\n", encoding="utf-8")
        idx = GocatorIndex.build(p)
        assert len(idx) == 5                      # indexer skips the blank line
        lat = LAT + np.arange(5) * 0.001
        assert inject_gocator_gps(p, lat, np.full(5, LON), np.full(5, ALT)) == 5
        rows = [l.split(",") for l in p.read_text().splitlines()[1:] if l.strip()]
        assert len(rows) == 5
        for i, row in enumerate(rows):
            assert int(row[0]) == i + 1                      # frameIndex order kept
            assert float(row[8]) == pytest.approx(lat[i])    # position not shifted

    def test_count_mismatch_leaves_file_intact(self, tmp_path):
        p = tmp_path / "g.csv"
        self.make_csv(p, 5)
        original = p.read_text()
        with pytest.raises(ValueError):
            inject_gocator_gps(p, np.full(2, LAT), np.full(2, LON), np.full(2, ALT))
        assert p.read_text() == original
        assert list(tmp_path.glob("*.tmp")) == []


class TestBatch:
    @pytest.fixture()
    def workdir(self, synth_run_root, tmp_path):
        # copy so the batch can write in place without touching the fixture
        dest = tmp_path / "collection"
        shutil.copytree(synth_run_root, dest, symlinks=False)
        return dest

    def test_writes_everything_and_reports(self, workdir, synth_cal_dir, tmp_path):
        report = process_collection(
            workdir, calibrations_dir=synth_cal_dir,
            overrides=OverrideStore(tmp_path / "ov.json"))
        assert len(report.outcomes) == 1
        o = report.outcomes[0]
        assert o.status == "written", o.failures
        # images: all but the unmatched pre-collection one, per camera
        rear = sorted((workdir / "Images" / o.run_id / "Rear").glob("*.jpg"))
        n_cams = len(o.cameras)
        # ROW has no calibrated lever arm, so it still tags every matched image.
        assert o.cameras["ROW"] == len(rear) - 1
        # Rear DOES have one, so its position is walked back along the trajectory
        # to the centre of its footprint. An image whose centre falls before the
        # nav window is refused rather than extrapolated, which legitimately
        # costs the leading frame(s) — fewer, but every one that survives means
        # what it says.
        assert o.cameras["Rear"] <= o.cameras["ROW"]
        assert o.images_tagged == sum(o.cameras.values())
        assert o.images_skipped == n_cams + (o.cameras["ROW"] - o.cameras["Rear"])
        tagged = [p for p in rear if read_image_gps(p) is not None]
        assert len(tagged) == o.cameras["Rear"]
        # gocator csvs got GPS columns
        for csv_path in (workdir / "GoCatorData" / o.run_id).glob("*.csv"):
            assert csv_path.read_text().splitlines()[0].split(",")[8] == "latitude"
        assert o.profiles_tagged > 0
        # export table written
        assert Path(o.csv_path).is_file()
        assert "batch report" in report.to_text()

    def test_all_cameras_geotagged(self, workdir, synth_cal_dir, tmp_path):
        report = process_collection(
            workdir, calibrations_dir=synth_cal_dir,
            overrides=OverrideStore(tmp_path / "ov.json"))
        o = report.outcomes[0]
        assert o.status == "written", o.failures
        assert set(o.cameras) == {"Rear", "ROW"}          # both discovered
        assert o.cameras["Rear"] > 0 and o.cameras["ROW"] > 0
        for cam in ("Rear", "ROW"):
            imgs = sorted((workdir / "Images" / o.run_id / cam).glob("*.jpg"))
            tagged = [p for p in imgs if read_image_gps(p) is not None]
            assert len(tagged) == o.cameras[cam]

        # Same trigger, DIFFERENT stamped position — and that is the point.
        # BOTH cameras are stamped at the centre of their own image footprint
        # (Rear 0.928 m behind the laser, ROW 1.69 m — ROW is oblique and looks
        # further back). The separation between the two stamps for the same
        # trigger must therefore be the DIFFERENCE of the two arms. If it ever
        # comes back near zero, an arm has silently stopped being applied; if
        # it comes back near a full arm, one camera has silently lost its own.
        from core.calibration import ROW_CAM_BEHIND_LASER_M
        r = read_image_gps(sorted((workdir / "Images" / o.run_id / "Rear").glob("*.jpg"))[5])
        w = read_image_gps(sorted((workdir / "Images" / o.run_id / "ROW").glob("*.jpg"))[5])
        mlat = 111132.0
        mlon = 111320.0 * math.cos(math.radians(w["lat_deg"]))
        sep_m = math.hypot((r["lat_deg"] - w["lat_deg"]) * mlat,
                           (r["lon_deg"] - w["lon_deg"]) * mlon)
        assert sep_m == pytest.approx(
            ROW_CAM_BEHIND_LASER_M - PAVE_CAM_BEHIND_LASER_M, abs=0.05)
        # csv carries a camera column with both
        import csv as _csv
        rows = list(_csv.DictReader(open(o.csv_path)))
        assert {r["camera"] for r in rows} == {"Rear", "ROW"}

    def test_rerun_is_safe(self, workdir, synth_cal_dir, tmp_path):
        store = OverrideStore(tmp_path / "ov.json")
        first = process_collection(workdir, calibrations_dir=synth_cal_dir, overrides=store)
        second = process_collection(workdir, calibrations_dir=synth_cal_dir, overrides=store)
        assert second.outcomes[0].status == "written", second.outcomes[0].failures
        assert second.outcomes[0].images_tagged == first.outcomes[0].images_tagged
        assert second.outcomes[0].profiles_tagged == first.outcomes[0].profiles_tagged

    def test_no_exif_writer_holds_the_images_once(self, workdir, synth_cal_dir,
                                                 tmp_path, monkeypatch):
        """Preflight already knows piexif is unavailable. The batch used to
        ignore that and try every photograph anyway - one ImportError line
        per image, every run 'flagged' - while the Gocator CSVs and the
        table were in fact written. Now the images are HELD with that one
        reason and the rest of the run is delivered."""
        import core.batch as batch
        monkeypatch.setattr(batch, "ensure_piexif",
                            lambda: (False, "piexif is not installed"))
        store = OverrideStore(tmp_path / "ov.json")
        report = process_collection(workdir, calibrations_dir=synth_cal_dir, overrides=store)
        o = report.outcomes[0]
        assert o.status == "partial", (o.status, o.failures)
        assert "piexif" in o.held["images"]
        assert o.images_tagged == 0
        assert o.profiles_tagged > 0
        assert not any(f.startswith("geotag ") for f in o.failures)
        for p in (workdir / "Images" / "20260816.150000" / "Rear").glob("*.jpg"):
            assert read_image_gps(p) is None

    def test_missing_export_skips_that_run_and_lists_the_files(self, workdir, synth_cal_dir, tmp_path):
        """No postprocessed export -> that run is SKIPPED, its files untouched,
        and the report says exactly which files got no GPS."""
        from core.batch import preflight

        nav = next(workdir.rglob("ascii-output.txt"))
        nav.unlink()
        store = OverrideStore(tmp_path / "ov.json")

        pf = preflight(workdir, calibrations_dir=synth_cal_dir, overrides=store)
        assert len(pf.not_ready) == 1 and not pf.ready
        r = pf.not_ready[0]
        assert "nav_export" in r.reason
        assert "Rear image(s)" in r.files_skipped_text()
        assert "Gocator CSV(s)" in r.files_skipped_text()
        assert "WILL BE SKIPPED" in pf.summary_text()

        report = process_collection(workdir, calibrations_dir=synth_cal_dir, overrides=store)
        o = report.outcomes[0]
        assert o.status == "skipped"
        assert any("NOT WRITTEN (no GPS)" in n for n in o.notes)
        assert "SKIPPED" in report.to_text()
        # that run's files are untouched
        for cam in ("Rear", "ROW"):
            for p in (workdir / "Images" / "20260816.150000" / cam).glob("*.jpg"):
                assert read_image_gps(p) is None
        for g in (workdir / "GoCatorData" / "20260816.150000").glob("*.csv"):
            assert "latitude" not in g.read_text().splitlines()[0]

    def test_good_run_still_processed_when_another_is_incomplete(self, workdir, synth_cal_dir, tmp_path):
        """A run missing data must NOT stop the complete runs (Derek, 2026-08-19)."""
        import shutil

        second = workdir / "Images" / "20260816.160000" / "Rear"
        second.mkdir(parents=True)
        src = sorted((workdir / "Images" / "20260816.150000" / "Rear").glob("*.jpg"))[:3]
        for i, f in enumerate(src):
            shutil.copy(f, second / f"{(i + 1) * 750:012d}.jpg")   # images only, no SBG/Gocator
        report = process_collection(workdir, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        by = {o.run_id: o for o in report.outcomes}
        assert by["20260816.150000"].status == "written"       # complete run processed
        assert by["20260816.160000"].status == "skipped"       # incomplete run skipped
        assert by["20260816.150000"].images_tagged > 0
        # the skipped run's images got nothing
        for p in second.glob("*.jpg"):
            assert read_image_gps(p) is None

    def test_export_without_time_coverage_is_caught(self, workdir, synth_cal_dir, tmp_path):
        """Export exists but does not span the run -> skipped as 'no coverage'."""
        from core.batch import preflight

        nav = next(workdir.rglob("ascii-output.txt"))
        lines = nav.read_text(encoding="utf-8").splitlines()
        head = lines[:6]
        nav.write_text("\n".join(head + lines[6:600]) + "\n",   # truncate window
                       encoding="utf-8")
        pf = preflight(workdir, calibrations_dir=synth_cal_dir,
                       overrides=OverrideStore(tmp_path / "ov.json"))
        assert len(pf.not_ready) == 1
        assert not pf.not_ready[0].coverage_ok
        assert "does NOT cover" in pf.not_ready[0].reason


class TestCalibrationIsOptionalForGPS:
    """Writing GPS needs PTP + triggers + export only. A broken/absent
    calibration must WARN, never block a production write."""

    @pytest.fixture()
    def workdir(self, synth_run_root, tmp_path):
        dest = tmp_path / "collection"
        shutil.copytree(synth_run_root, dest, symlinks=False)
        return dest

    def test_broken_calibration_still_writes_gps(self, workdir, synth_cal_dir, tmp_path):
        cal = next(synth_cal_dir.rglob("PAVE_*.yaml"))
        cal.write_text("serial_number: 'X'\n", encoding="utf-8")   # no board/matrix
        report = process_collection(workdir, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        o = report.outcomes[0]
        assert o.status == "written", o.failures
        assert o.images_tagged > 0
        assert any("calibration" in w for w in o.warnings)

    def test_no_calibration_yaml_at_all_still_writes_gps(self, workdir, synth_cal_dir, tmp_path):
        """No PAVE_*.yaml anywhere - only the lever-arm file, which IS
        required (the arms are in every written position; the intrinsics
        are for the viewer)."""
        for y in synth_cal_dir.rglob("PAVE_*.yaml"):
            y.unlink()
        report = process_collection(workdir, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov2.json"))
        o = report.outcomes[0]
        assert o.status == "written", o.failures
        assert o.images_tagged > 0

    def test_but_no_lever_arm_file_writes_nothing(self, workdir, tmp_path):
        """The other half of the rule: an empty calibrations folder has no
        arms, and no arms means no positions (Derek, 2026-09-02)."""
        empty = tmp_path / "nocal"; empty.mkdir()
        report = process_collection(workdir, calibrations_dir=empty,
                                    overrides=OverrideStore(tmp_path / "ov2.json"))
        o = report.outcomes[0]
        assert o.status == "skipped"
        assert o.images_tagged == 0 and o.profiles_tagged == 0


class TestFailureReporting:
    def test_repeated_failures_collapse_to_one_line(self):
        from core.batch import BatchReport, RunOutcome, _collapse

        msgs = [f"geotag Rear/{i:012d}.jpg: No module named 'piexif'" for i in range(166)]
        out = _collapse(msgs)
        assert len(out) == 2                      # summary + one example
        assert out[0].startswith("166 file(s) failed")
        assert "piexif" in out[0]
        rep = BatchReport(root="x", started_utc="t")
        rep.outcomes.append(RunOutcome(run_id="r", ok=False, status="flagged",
                                       failures=msgs))
        text = rep.to_text()
        assert text.count("No module named") <= 4   # not 166 lines

    def test_distinct_failures_are_kept(self):
        from core.batch import _collapse

        out = _collapse(["a.jpg: disk full", "b.jpg: permission denied"])
        assert len(out) == 2

    def test_ensure_piexif_reports_available(self):
        from core.geotag import ensure_piexif

        ok, note = ensure_piexif()
        assert ok and "piexif" in note


class TestBatchReportWording:
    """The report is the only record of what a batch did. It has to be read
    correctly at a glance, by someone who was not watching it run."""

    def test_a_flagged_run_still_reports_the_images_it_holds(self):
        """Regression (Derek, 2026-08-26): run 20260824.101724 failed QC and
        printed "0 images", which reads as "this run HAS no images". It held
        76. The number that was zero was the number TAGGED, not the number
        present — and those are opposite conclusions about the data."""
        from core.batch import BatchReport, RunOutcome

        out = RunOutcome(
            run_id="20260824.101724", ok=False, status="flagged",
            cameras_found={"Rear": 38, "ROW": 38}, gocator_found=2,
            failures=["content.gocator_R_ptp: r = 0.746"],
        )
        out.notes.append(f"NOT WRITTEN (no GPS): {out.found_text()}")
        text = BatchReport(root="r", started_utc="t", outcomes=[out]).to_text()

        assert out.images_found == 76
        assert "0 of 76 images tagged" in text
        assert "38 Rear image(s)" in text and "2 Gocator CSV(s)" in text
        assert "NOT WRITTEN" in text

    def test_a_written_run_shows_tagged_against_present(self):
        """The same line must stay honest when it worked: 2 images had no
        trigger, so 86 of 88 were tagged — not "86 images"."""
        from core.batch import BatchReport, RunOutcome

        out = RunOutcome(
            run_id="20260824.100157", ok=True, status="written",
            images_tagged=86, images_skipped=2,
            cameras={"Rear": 43, "ROW": 43},
            cameras_found={"Rear": 44, "ROW": 44}, gocator_found=2,
        )
        text = BatchReport(root="r", started_utc="t", outcomes=[out]).to_text()

        assert "86 of 88 images tagged" in text
        assert "2 images skipped" in text


class TestIssueLog:
    """Every fail, error and warning has to be recorded, and land in Processed/."""

    @pytest.fixture()
    def workdir(self, synth_run_root, tmp_path):
        import shutil
        dst = tmp_path / "workP"
        shutil.copytree(synth_run_root, dst)
        return dst

    def test_reports_go_to_processed_and_the_folder_is_created(self, workdir,
                                                               synth_cal_dir, tmp_path):
        from core.batch import PROCESSED_DIR, write_reports
        store = OverrideStore(tmp_path / "ov.json")
        report = process_collection(workdir, calibrations_dir=synth_cal_dir,
                                    overrides=store)
        assert not (workdir / PROCESSED_DIR).exists()      # not made until asked
        written = write_reports(report)
        d = workdir / PROCESSED_DIR
        assert d.is_dir()
        assert not [k for k in written if k.startswith("error")], written
        assert list(d.glob("batch_report_*.txt")) and list(d.glob("issues_*.csv"))

    def test_every_warning_and_failure_is_a_row(self, workdir, synth_cal_dir,
                                                tmp_path):
        import csv as _csv
        from core.batch import write_reports
        store = OverrideStore(tmp_path / "ov.json")
        report = process_collection(workdir, calibrations_dir=synth_cal_dir,
                                    overrides=store)
        written = write_reports(report)
        with open(written["issues"], newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        n_expected = sum(len(o.failures) + len(o.warnings) + len(o.held)
                         for o in report.outcomes)
        assert len(rows) == n_expected
        assert n_expected > 0, "the synthetic run should warn about something"
        assert {r["severity"] for r in rows} <= {"FAIL", "ERROR", "WARN", "HELD"}
        assert all(r["run_id"] for r in rows)

    def test_a_skipped_run_lists_each_missing_input(self, workdir, synth_cal_dir,
                                                    tmp_path):
        """Skipped runs never reach process_run, so they used to log nothing at
        all — yet they are exactly what has to be fixed."""
        next(workdir.rglob("eventOutA.txt")).unlink()
        store = OverrideStore(tmp_path / "ov.json")
        report = process_collection(workdir, calibrations_dir=synth_cal_dir,
                                    overrides=store)
        rows = [r for r in report.issue_rows() if r["run_status"] == "skipped"]
        assert rows, "a skipped run must still be logged"
        assert any("event" in r["check_id"] for r in rows), rows

    def test_repeated_failures_are_all_kept_even_though_the_text_collapses(self):
        from core.batch import BatchReport, RunOutcome
        rep = BatchReport(root="r", started_utc="2026-08-30T00:00:00+00:00")
        rep.outcomes.append(RunOutcome(
            run_id="x", ok=False, status="flagged",
            failures=[f"geotag Rear/{i:03d}.jpg: broken" for i in range(200)]))
        assert len(rep.issue_rows()) == 200          # nothing summarised away
        assert "200 file(s) failed" in rep.to_text()  # text stays readable
