"""What OTHER processes read: run_mapping.csv and the alignment CSV.

Both exist because a downstream tool needs to find things this app moved or
renamed, and both had a way of quietly lying about it:

  * the SBG logger and the collection system name the same run's folders a
    second or two apart, so every tool that opens a collection has to redo the
    fuzzy pairing - and one that pairs differently reads ANOTHER run's
    triggers without saying so;
  * the alignment CSV was written before the rename, so every filename in it
    was stale by the time the batch finished. On 20260824.100157 all 88 rows
    pointed at names that no longer existed on disk.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from core.batch import (MAPPING_COLUMNS, process_collection, strip_stamp,
                        write_reports)
from core.discovery import OverrideStore
from core.rename import BEFORE_DIR
from tests.test_events import add_events_to_run

RUN_ID = "20260816.150000"


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def collection_with_daily(root, synth, section_offset_m=5.0):
    """The synthetic run, plus the Daily row that lets the rename happen.
    `section_offset_m` puts the section start inside the run, so the leading
    images fall before it and get set aside - the interesting case."""
    day = root / "20260816"
    day.mkdir(exist_ok=True)
    lat = 43.4363 + section_offset_m / 111320.0
    (day / "Daily_ARAN104_20260816.csv").write_text(
        "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed\n"
        f"Video [{RUN_ID}] run,200180,North,0.000,0.700,{lat:.9f},"
        "-80.315200,20\n", encoding="utf-8-sig")
    add_events_to_run(root, synth)
    return root


@pytest.fixture()
def done(synth_run_root, synth, synth_cal_dir, tmp_path):
    """A finished batch: renamed, set aside, reports written."""
    root = collection_with_daily(synth_run_root, synth)
    report = process_collection(root, calibrations_dir=synth_cal_dir,
                                overrides=OverrideStore(tmp_path / "ov.json"))
    return root, report, write_reports(report)


class TestAlignmentCsvNamesTheFilesOnDisk:
    def test_every_final_name_exists(self, done):
        """The regression, stated as the thing that has to be true: take the
        table to the folder and every row finds its picture."""
        root, report, _ = done
        o = next(x for x in report.outcomes if x.run_id == RUN_ID)
        assert o.csv_path, "the run should have exported a table"
        cam_dir = root / "Images" / RUN_ID
        missing = []
        for r in csv.DictReader(open(o.csv_path, newline="", encoding="utf-8")):
            folder = cam_dir / r["camera"]
            if r["set_aside"] == "True":
                folder = folder / BEFORE_DIR
            if not (folder / r["image_file_final"]).is_file():
                missing.append(r["image_file_final"])
        assert not missing, f"{len(missing)} row(s) name a file that is not there"

    def test_the_old_name_is_kept_too(self, done):
        """Both halves matter: `image_file` is what ACS called it, which is
        how anything holding the original names finds the row at all."""
        _, report, _ = done
        o = next(x for x in report.outcomes if x.run_id == RUN_ID)
        rows = list(csv.DictReader(open(o.csv_path, newline="", encoding="utf-8")))
        assert rows
        renamed = [r for r in rows if r["image_file"] != r["image_file_final"]]
        assert renamed, "this run renames images, so the two columns must differ"
        assert all(r["image_file"].endswith(".jpg") for r in rows)

    def test_section_and_distance_travel_with_the_row(self, done):
        """The number in the new filename has to be readable as a number,
        not only parsed back out of the name."""
        _, report, _ = done
        o = next(x for x in report.outcomes if x.run_id == RUN_ID)
        rows = list(csv.DictReader(open(o.csv_path, newline="", encoding="utf-8")))
        placed = [r for r in rows if r["section_distance_mm"]]
        assert placed
        assert {r["section"] for r in placed} == {"200180"}
        for r in placed:
            stem = Path(r["image_file_final"]).stem
            assert int(r["section_distance_mm"]) == int(stem)

    def test_set_aside_marks_the_before_section_images(self, done):
        root, report, _ = done
        o = next(x for x in report.outcomes if x.run_id == RUN_ID)
        rows = list(csv.DictReader(open(o.csv_path, newline="", encoding="utf-8")))
        aside = [r for r in rows if r["set_aside"] == "True"]
        assert aside, "the section starts 5 m in, so some images are before it"
        assert all(r["image_file_final"].startswith("-") for r in aside)
        # they keep their row AND their position: they are still this run's
        # photographs, they are just not in the section
        assert all(r["latitude_deg"] for r in aside)
        on_disk = list((root / "Images" / RUN_ID / "Rear" / BEFORE_DIR).glob("*.jpg"))
        assert on_disk

    def test_a_viewer_export_still_names_the_file_it_has(self, done):
        """export_csv without rename info (the single-run path in the viewer)
        must not leave the column blank - the folder is not mid-rename there,
        so the name it holds IS the final one."""
        from core.pipeline import export_csv
        root, report, _ = done
        o = next(x for x in report.outcomes if x.run_id == RUN_ID)
        out = export_csv(o.result, root / "solo.csv")
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert rows
        assert all(r["image_file_final"] == r["image_file"] for r in rows)


class TestInterruptedRenameRecovery:
    """Kill a batch between the two passes of the rename and images are left
    under `<name>.__renaming__`. Nothing matches them as *.jpg, so the next
    run scans fewer images than it has triggers, the matcher refuses the run
    as inconsistent, and it stays refused for ever.

    Seen for real on 20260824_revruns_FullProcessingRun: run .100258 Rear had
    5 images stranded, 38 were found where 43 exist, and the report said only
    "image/trigger count difference is -5" with nothing on disk to explain it.
    """

    def strand(self, root, n=5):
        """Do to a folder exactly what an interrupted phase 1 does.

        Five, as on .100258 - past the matcher's +/-3, so an unrecovered run
        really is refused rather than merely shifted."""
        from core.rename import TEMP_SUFFIX
        cam = root / "Images" / RUN_ID / "Rear"
        victims = sorted(cam.glob("*.jpg"))[:n]
        for p in victims:
            p.rename(p.with_name(p.name + TEMP_SUFFIX))
        return cam, [p.name for p in victims]

    def test_the_images_come_back(self, synth_run_root, synth, synth_cal_dir,
                                  tmp_path):
        root = collection_with_daily(synth_run_root, synth)
        cam, names = self.strand(root)
        before = len(list(cam.glob("*.jpg")))
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        assert not list(cam.glob("*__renaming__*")), "no temp file may survive"
        assert len(report.recovered) == len(names)
        assert len(list(cam.glob("*.jpg"))) + \
            len(list((cam / BEFORE_DIR).glob("*.jpg"))) == before + len(names)

    def test_the_run_processes_instead_of_being_refused(self, synth_run_root, synth,
                                                        synth_cal_dir, tmp_path):
        """The point of the fix: recovery happens before anything scans, so
        the matcher never sees the short count."""
        root = collection_with_daily(synth_run_root, synth)
        self.strand(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        o = next(x for x in report.outcomes if x.run_id == RUN_ID)
        assert o.status in ("written", "partial"), o.failures
        assert not any("count difference" in f for f in o.failures), o.failures

    def test_it_is_reported_not_silent(self, synth_run_root, synth, synth_cal_dir,
                                       tmp_path):
        """These images were invisible and their run may have been refused -
        that is not something to fix quietly."""
        root = collection_with_daily(synth_run_root, synth)
        self.strand(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        assert "left mid-rename" in report.to_text()

    def test_an_occupied_original_name_is_left_alone(self, synth_run_root, synth,
                                                     synth_cal_dir, tmp_path):
        """A temp file whose real name is taken is a state this code cannot
        reason about. Restoring would overwrite a real image, so it is
        reported and left."""
        from core.rename import TEMP_SUFFIX, recover_interrupted
        root = collection_with_daily(synth_run_root, synth)
        cam = root / "Images" / RUN_ID / "Rear"
        victim = sorted(cam.glob("*.jpg"))[0]
        (cam / (victim.name + TEMP_SUFFIX)).write_bytes(b"stray")
        got, problems = recover_interrupted(cam)
        assert got == []
        assert problems and "already taken" in problems[0]
        assert victim.exists(), "the real image must not be overwritten"

    def test_a_clean_folder_recovers_nothing(self, synth_run_root, synth,
                                             synth_cal_dir, tmp_path):
        root = collection_with_daily(synth_run_root, synth)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        assert report.recovered == []
        assert "left mid-rename" not in report.to_text()

    def test_a_dry_run_finds_them_but_does_not_move_them(
            self, synth_run_root, synth, synth_cal_dir, tmp_path):
        """This walk happens before ANY flag is consulted, so it renamed files
        during --no-images --no-gocator --no-csv while the tool printed
        "nothing was written into the collection" (found 2026-09-02).

        Finding them still matters: stranded images are exactly what makes a
        run scan short and get refused, so a dry run has to say so."""
        root = collection_with_daily(synth_run_root, synth)
        cam, names = self.strand(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"),
                                    write_images=False, write_gocator=False,
                                    write_csv=False)
        still = sorted(p.name for p in cam.glob("*__renaming__*"))
        assert len(still) == len(names), "a dry run must not rename anything"
        assert len(report.recovered) == len(names), "but it must still report them"
        assert "STRANDED" in report.to_text()

    def test_a_status_x_run_is_not_even_repaired(self, synth_run_root, synth,
                                                 synth_cal_dir, tmp_path):
        """Derek's rule is that an X run is not processed, and not processed
        means not touched. This walk ran before the Daily file was read, so it
        reached straight into one."""
        root = collection_with_daily(synth_run_root, synth)
        p = root / "20260816" / "Daily_ARAN104_20260816.csv"
        p.write_text(p.read_text(encoding="utf-8-sig")
                     .replace(",Speed\n", ",Speed,Status\n")
                     .replace(",20\n", ",20,X\n"), encoding="utf-8-sig")
        cam, names = self.strand(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        from core.rename import TEMP_SUFFIX
        assert report.excluded == [RUN_ID]
        assert (sorted(q.name for q in cam.glob(f"*{TEMP_SUFFIX}"))
                == sorted(n + TEMP_SUFFIX for n in names))
        assert report.recovered == []


class TestOnlyWhatTheDailyFileRegisters:
    """Derek's rule (2026-08-31): if it is not in the Daily file, ignore it.

    The Daily file is ACS's record of what was collected. 20260824 grew a
    GoCatorData folder at .101524 - 3387 laser profiles, no images, no logger,
    no Daily row: a section the operator started and abandoned. Treating it as
    a run made it four FAILs on a report that was otherwise clean, which is
    exactly the noise that trains people to stop reading reports.
    """

    def _orphan(self, root):
        """A laser-only folder for a stamp the Daily file does not list."""
        d = root / "GoCatorData" / "20260816.151111"
        d.mkdir(parents=True)
        for side in "LR":
            (d / f"20260816T151111{side}.csv").write_text(
                "frameIndex,timestamp,ptpTimestamp,encoder,"
                "numberOfProfilePoints,numberOfValidPoints,bridgedValue,"
                "status,x0,z0\n1,1,1787000000000000,100,1,1,0,0,-1.0,2.0\n",
                encoding="utf-8")
        return "20260816.151111"

    def test_it_is_not_processed_and_is_not_a_failure(self, synth_run_root, synth,
                                                      synth_cal_dir, tmp_path):
        root = collection_with_daily(synth_run_root, synth)
        orphan = self._orphan(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        assert orphan not in {o.run_id for o in report.outcomes}
        assert not any(r["run_id"] == orphan for r in report.issue_rows())
        assert report.ignored == [orphan]

    def test_the_report_still_says_it_is_there(self, synth_run_root, synth,
                                               synth_cal_dir, tmp_path):
        """Ignoring is not the same as hiding: those folders hold real data,
        and silence would leave someone hunting for where it went."""
        root = collection_with_daily(synth_run_root, synth)
        orphan = self._orphan(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        text = report.to_text()
        assert "no row in the ACS Daily file" in text
        assert orphan in text

    def test_it_is_not_in_the_run_mapping(self, synth_run_root, synth,
                                          synth_cal_dir, tmp_path):
        root = collection_with_daily(synth_run_root, synth)
        orphan = self._orphan(root)
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        written = write_reports(report)
        ids = [strip_stamp(r["run_id"]) for r in csv.DictReader(
            open(written["mapping"], newline="", encoding="utf-8"))]
        assert orphan not in ids
        assert RUN_ID in ids

    def test_no_daily_file_means_no_filtering(self, synth_run_root, synth_cal_dir,
                                              tmp_path):
        """The guard that matters most. A collection whose Daily file has not
        been copied in yet must process normally - filtering on an absent
        register would silently drop every run in it."""
        from core.discovery import daily_stamps, discover_runs
        assert daily_stamps(synth_run_root) is None
        runs = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir)
        assert [r.run_id for r in runs] == [RUN_ID]

    def test_registered_only_false_still_sees_everything(self, synth_run_root, synth,
                                                         synth_cal_dir):
        from core.discovery import discover_runs
        root = collection_with_daily(synth_run_root, synth)
        orphan = self._orphan(root)
        seen = {r.run_id for r in discover_runs(root, calibrations_dir=synth_cal_dir,
                                                registered_only=False)}
        assert orphan in seen


class TestRunMapping:
    def test_it_is_written_with_a_stable_name(self, done):
        """Other processes point at this file, so its name may not carry a
        timestamp the way the batch report does."""
        root, _, written = done
        assert not [k for k in written if k.startswith("error")], written
        p = Path(written["mapping"])
        assert p.name == "run_mapping.csv"
        assert p.parent.name == "Processed"

    def test_it_records_the_logger_folder_and_the_skew(self, done):
        _, report, written = done
        rows = {strip_stamp(r["run_id"]): r
                for r in csv.DictReader(open(written["mapping"], newline="",
                                             encoding="utf-8"))}
        assert set(rows) == {o.run_id for o in report.outcomes}
        r = rows[RUN_ID]
        assert r["sbg_stamp"], "a consumer needs a folder name, never a blank"
        assert r["sbg_exact"] in ("True", "False")
        assert int(r["sbg_skew_s"]) == 0 or r["sbg_exact"] == "False"

    def test_it_names_the_logger_FOLDER_not_just_the_stamp(self, done):
        """"The folder is named a second wrong - where is it?" is answered by
        a path you can paste into Explorer, not by a stamp to go hunting
        with."""
        _, _, written = done
        r = next(x for x in csv.DictReader(open(written["mapping"], newline="",
                                                encoding="utf-8"))
                 if strip_stamp(x["run_id"]) == RUN_ID)
        assert r["sbg_logger_dir"], "every processed run must say where its logger is"
        assert Path(r["sbg_logger_dir"]).is_dir()
        assert Path(r["sbg_logger_dir"]).name.endswith("DataLogger")

    def test_a_skewed_folder_says_so_in_words(self, synth_run_root, synth,
                                              synth_cal_dir, tmp_path):
        """The numeric columns are for programs. `note` is for the person
        skimming the file to find out why a run looks odd."""
        root = collection_with_daily(synth_run_root, synth)
        logger = next(p for p in (root / "SBGData").rglob("* DataLogger")
                      if p.is_dir())
        stamp = logger.name.split(" ")[0]
        skewed = f"{stamp[:-2]}{int(stamp[-2:]) + 1:02d}"
        logger.rename(logger.with_name(f"{skewed} DataLogger"))
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        written = write_reports(report)
        r = next(x for x in csv.DictReader(open(written["mapping"], newline="",
                                                encoding="utf-8"))
                 if strip_stamp(x["run_id"]) == RUN_ID)
        assert "+1 s" in r["note"]
        assert skewed in r["note"]
        assert Path(r["sbg_logger_dir"]).name == f"{skewed} DataLogger"

    def test_a_daily_section_with_no_data_still_gets_a_row(self, synth_run_root,
                                                          synth, synth_cal_dir,
                                                          tmp_path):
        """The gap that mattered most: every other row starts from a folder
        that exists, so a section whose data never arrived would vanish from
        the very file you open to ask what you can run."""
        root = collection_with_daily(synth_run_root, synth)
        day = root / "20260816" / "Daily_ARAN104_20260816.csv"
        day.write_text(day.read_text(encoding="utf-8-sig") +
                       "Video [20260816.160000] run,200999,North,0.000,0.500,"
                       "43.4400,-80.3150,20\n", encoding="utf-8-sig")
        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        written = write_reports(report)
        rows = {strip_stamp(x["run_id"]): x for x in csv.DictReader(
            open(written["mapping"], newline="", encoding="utf-8"))}
        assert "20260816.160000" in rows, "a Daily section may never just vanish"
        ghost = rows["20260816.160000"]
        assert ghost["status"] == "not_found"
        assert ghost["section"] == "200999"
        assert "no folders on disk" in ghost["note"]

    def test_the_stamps_are_bracketed_so_excel_cannot_eat_them(self, done):
        """20260824.100157 is a valid decimal number. Excel parses it as one
        and General format shows "20260824.1", so all twelve rows of a day
        look identical in the tool people actually open the file with. The
        brackets make it text - and they are exactly how the ACS Daily file
        writes the same stamp, so the two agree on how a run is spelled."""
        _, _, written = done
        rows = list(csv.DictReader(open(written["mapping"], newline="",
                                        encoding="utf-8")))
        assert rows
        for r in rows:
            assert r["run_id"].startswith("[") and r["run_id"].endswith("]")
            assert strip_stamp(r["run_id"]) == RUN_ID
            if r["sbg_stamp"]:
                assert r["sbg_stamp"].startswith("[")
            # the thing that broke: bare, every stamp is float-parseable
            assert not _is_number(r["run_id"])
            assert _is_number(strip_stamp(r["run_id"])), \
                "the bare stamp really is a number - that is the whole problem"

    def test_strip_stamp_tolerates_an_unbracketed_value(self):
        """A hand-edited file, or one written before this change, must still
        read back."""
        assert strip_stamp("[20260824.100157]") == "20260824.100157"
        assert strip_stamp("20260824.100157") == "20260824.100157"
        assert strip_stamp(" [20260824.100157] ") == "20260824.100157"
        assert strip_stamp("") == ""

    def test_the_columns_are_the_published_ones(self, done):
        """MAPPING_COLUMNS is a contract with other tools; a silent reorder or
        rename breaks a reader that indexes by position."""
        _, _, written = done
        with open(written["mapping"], newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        assert header == MAPPING_COLUMNS

    def test_a_skewed_folder_is_reported_as_skewed(self, synth_run_root, synth,
                                                   synth_cal_dir, tmp_path):
        """20260824: images said .100258, the log folder said .100259. The
        mapping has to name the folder actually used and say how far off it
        was - that is the whole reason the file exists."""
        root = collection_with_daily(synth_run_root, synth)
        logger = next(p for p in (root / "SBGData").rglob("* DataLogger")
                      if p.is_dir())
        stamp = logger.name.split(" ")[0]
        skewed = f"{stamp[:-2]}{int(stamp[-2:]) + 1:02d}"
        logger.rename(logger.with_name(f"{skewed} DataLogger"))

        report = process_collection(root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        written = write_reports(report)
        rows = {strip_stamp(r["run_id"]): r
                for r in csv.DictReader(open(written["mapping"], newline="",
                                             encoding="utf-8"))}
        r = rows[RUN_ID]
        assert strip_stamp(r["sbg_stamp"]) == skewed
        assert int(r["sbg_skew_s"]) == 1
        assert r["sbg_exact"] == "False"

    def test_skipped_runs_get_a_row_too(self, synth_run_root, synth_cal_dir,
                                        tmp_path):
        """A run nobody could process is exactly the one somebody has to chase.
        Leaving it out would read as 'this run never existed'."""
        for p in (synth_run_root / "SBGData").rglob("eventOutA.txt"):
            p.unlink()
        report = process_collection(synth_run_root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        written = write_reports(report)
        rows = list(csv.DictReader(open(written["mapping"], newline="",
                                        encoding="utf-8")))
        assert [strip_stamp(r["run_id"]) for r in rows] == \
            [o.run_id for o in report.outcomes]
        assert any(r["status"] == "skipped" for r in rows)
        # and it still says where the images it could not place are
        assert all(r["images_dir"] for r in rows)
