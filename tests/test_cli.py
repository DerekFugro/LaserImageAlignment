"""The command line, tested the way something else would use it: by EXIT CODE.

Derek, 2026-09-01: the app should be drivable from a command line. The point
of one is that a scheduler, a batch file or another process can branch on the
answer without reading English, so the exit code is the contract and these
tests assert on it first and the printing second.

    0   every run was written
    1   it ran, but something needs a person
    2   it could not run at all

1 and 2 are deliberately different: "nine runs written, one skipped because
its export is missing" is a normal end to a collection day, and must not look
the same as "that folder is not a collection".
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

import cli
from core import discovery as disc

RUN = "20260816.150000"


def daily(root, status="C"):
    day = root / "20260816"
    day.mkdir(exist_ok=True)
    (day / "Daily_ARAN104_20260816.csv").write_text(
        "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Status\n"
        f"Video [{RUN}] run,200180,North,0.000,0.700,43.436345,-80.315200,{status}\n",
        encoding="utf-8-sig")
    return root


def run_cli(argv, tmp_path, cal_dir):
    """Always with our own overrides sidecar - a test must never read or write
    the real one in the user's app data folder."""
    return cli.main(argv + ["--calibrations", str(cal_dir),
                            "--overrides", str(tmp_path / "ov.json")])


class TestExitCodes:
    def test_a_clean_collection_returns_zero(self, synth_run_root, synth_cal_dir,
                                             tmp_path, capsys):
        daily(synth_run_root)
        code = run_cli(["process", str(synth_run_root)], tmp_path, synth_cal_dir)
        out = capsys.readouterr().out
        assert code == cli.EXIT_OK, out
        assert "1 written" in out or "(1 written" in out

    def test_a_folder_that_is_not_a_collection_returns_two(self, tmp_path,
                                                           synth_cal_dir, capsys):
        empty = tmp_path / "nothing"
        empty.mkdir()
        assert run_cli(["process", str(empty)], tmp_path,
                       synth_cal_dir) == cli.EXIT_CANNOT_RUN
        assert "no runs found" in capsys.readouterr().err

    def test_a_path_that_does_not_exist_returns_two(self, tmp_path, synth_cal_dir,
                                                    capsys):
        assert run_cli(["process", str(tmp_path / "nope")], tmp_path,
                       synth_cal_dir) == cli.EXIT_CANNOT_RUN
        assert "not a folder" in capsys.readouterr().err

    def test_a_run_that_cannot_be_processed_returns_one_not_two(
            self, synth_run_root, synth_cal_dir, tmp_path, capsys):
        """The distinction that makes the exit code worth having. The export is
        gone, so the run cannot be placed - but the collection is still a
        collection and the tool still ran."""
        daily(synth_run_root)
        nav = next(synth_run_root.rglob("ascii-output.txt"))
        nav.rename(nav.with_suffix(".moved"))
        code = run_cli(["check", str(synth_run_root)], tmp_path, synth_cal_dir)
        assert code == cli.EXIT_NEEDS_ATTENTION
        assert "WILL BE SKIPPED" in capsys.readouterr().out


class TestItWritesWhatTheGuiWrites:
    def test_process_writes_gps_the_rename_and_the_reports(
            self, synth_run_root, synth_cal_dir, tmp_path, capsys):
        daily(synth_run_root)
        before = sorted(p.name for p in (synth_run_root / "Images" / RUN / "Rear").glob("*.jpg"))
        assert run_cli(["process", str(synth_run_root)], tmp_path,
                       synth_cal_dir) == cli.EXIT_OK
        cam = synth_run_root / "Images" / RUN / "Rear"
        after = sorted(p.name for p in cam.glob("*.jpg"))
        assert after != before, "the batch renames every placed image"
        assert (cam / "rename_manifest.csv").is_file()
        proc = synth_run_root / "Processed"
        assert (proc / "run_mapping.csv").is_file()
        assert list(proc.glob("batch_report_*.txt"))
        assert list((synth_run_root / "Exports").glob("*_alignment.csv"))


class TestDryRun:
    def test_nothing_is_written_into_the_collection(self, synth_run_root,
                                                    synth_cal_dir, tmp_path, capsys):
        """--no-images --no-gocator --no-csv has to mean it. A dry run that
        renamed the images would be worse than no dry run at all, because the
        person running it believes nothing moved."""
        daily(synth_run_root)
        images = synth_run_root / "Images" / RUN
        goc = synth_run_root / "GoCatorData" / RUN
        before_names = sorted(p.name for p in images.rglob("*"))
        before_goc = {p: p.read_bytes() for p in goc.glob("*.csv")}

        run_cli(["process", str(synth_run_root), "--no-images", "--no-gocator",
                 "--no-csv"], tmp_path, synth_cal_dir)

        assert sorted(p.name for p in images.rglob("*")) == before_names
        assert {p: p.read_bytes() for p in goc.glob("*.csv")} == before_goc
        assert "DRY RUN" in capsys.readouterr().out

    def test_the_reports_are_still_written(self, synth_run_root, synth_cal_dir,
                                           tmp_path):
        """"What WOULD have happened" is worth keeping - and the batch report
        says which it was."""
        daily(synth_run_root)
        run_cli(["process", str(synth_run_root), "--no-images", "--no-gocator",
                 "--no-csv"], tmp_path, synth_cal_dir)
        assert list((synth_run_root / "Processed").glob("batch_report_*.txt"))

    def test_the_saved_report_does_not_claim_it_wrote_anything(
            self, synth_run_root, synth_cal_dir, tmp_path):
        """The file on disk, not the console. A dry run's report otherwise read
        exactly like a successful one and ended "every deliverable was
        written" - the opposite of the truth, in the document somebody opens a
        week later to ask what this batch did."""
        daily(synth_run_root)
        run_cli(["process", str(synth_run_root), "--no-images", "--no-gocator",
                 "--no-csv"], tmp_path, synth_cal_dir)
        text = next((synth_run_root / "Processed")
                    .glob("batch_report_*.txt")).read_text(encoding="utf-8")
        assert "DRY RUN" in text
        assert "every deliverable was written" not in text


class TestRunsCommand:
    def test_it_says_what_will_run(self, synth_run_root, synth_cal_dir, tmp_path,
                                   capsys):
        daily(synth_run_root)
        assert run_cli(["runs", str(synth_run_root)], tmp_path,
                       synth_cal_dir) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert f"[{RUN}]" in out, "run ids are bracketed, as the Daily file writes them"
        assert "PROCESS" in out
        assert "1 run(s) would be processed" in out

    def test_an_excluded_run_is_named_and_explained(self, synth_run_root,
                                                    synth_cal_dir, tmp_path, capsys):
        """Status X. The exit code is 1, not 0: nothing would be processed, and
        a scheduler should not read that as success."""
        daily(synth_run_root, status="x")
        code = run_cli(["runs", str(synth_run_root)], tmp_path, synth_cal_dir)
        out = capsys.readouterr().out
        assert "Status X" in out
        assert "0 run(s) would be processed" in out
        assert code == cli.EXIT_NEEDS_ATTENTION

    def test_it_writes_nothing(self, synth_run_root, synth_cal_dir, tmp_path):
        daily(synth_run_root)
        run_cli(["runs", str(synth_run_root)], tmp_path, synth_cal_dir)
        assert not (synth_run_root / "Processed").exists()


class TestLocate:
    def test_it_supplies_a_missing_input_for_every_run(self, synth_run_root,
                                                       synth_cal_dir, tmp_path,
                                                       capsys):
        """The Locate... button, for a shell. The reason an export is missing is
        almost always that the whole collection's Qinertia output lives
        somewhere else, so one flag answers it for the whole day."""
        daily(synth_run_root)
        nav = next(synth_run_root.rglob("ascii-output.txt"))
        moved = tmp_path / "elsewhere" / "ascii-output.txt"
        moved.parent.mkdir(parents=True)
        moved.write_bytes(nav.read_bytes())
        nav.unlink()

        assert run_cli(["check", str(synth_run_root)], tmp_path,
                       synth_cal_dir) == cli.EXIT_NEEDS_ATTENTION
        capsys.readouterr()
        code = run_cli(["check", str(synth_run_root),
                        "--locate", f"nav_export={moved}"], tmp_path, synth_cal_dir)
        out = capsys.readouterr().out
        assert "located nav_export for 1 run(s)" in out
        assert code == cli.EXIT_OK, out

    def test_it_is_remembered_for_the_next_call(self, synth_run_root, synth_cal_dir,
                                                tmp_path, capsys):
        """Same sidecar the viewer uses, so a path located here is known there."""
        daily(synth_run_root)
        nav = next(synth_run_root.rglob("ascii-output.txt"))
        moved = tmp_path / "elsewhere" / "ascii-output.txt"
        moved.parent.mkdir(parents=True)
        moved.write_bytes(nav.read_bytes())
        nav.unlink()
        run_cli(["check", str(synth_run_root), "--locate", f"nav_export={moved}"],
                tmp_path, synth_cal_dir)
        capsys.readouterr()
        assert run_cli(["check", str(synth_run_root)], tmp_path,
                       synth_cal_dir) == cli.EXIT_OK

    def test_a_bad_key_is_refused_before_anything_runs(self, synth_run_root,
                                                       synth_cal_dir, tmp_path,
                                                       capsys):
        """EXIT_CANNOT_RUN, not 1. Nothing was attempted, so it must not look
        like "ran, and something needs a person"."""
        code = run_cli(["check", str(synth_run_root), "--locate", "nav=x"],
                       tmp_path, synth_cal_dir)
        assert code == cli.EXIT_CANNOT_RUN
        assert "unknown input" in capsys.readouterr().err

    def test_a_path_that_is_not_there_is_refused(self, synth_run_root, synth_cal_dir,
                                                 tmp_path, capsys):
        code = run_cli(["check", str(synth_run_root),
                        "--locate", f"nav_export={tmp_path / 'nope.txt'}"],
                       tmp_path, synth_cal_dir)
        assert code == cli.EXIT_CANNOT_RUN
        assert "no such file" in capsys.readouterr().err


class TestItIsHeadless:
    def test_no_qt_is_imported_by_the_cli(self):
        """The whole reason this is possible: core/ has never imported Qt. If
        that changes, this app stops working on a machine with no display and
        this test is the alarm."""
        import subprocess
        import sys
        code = ("import cli, sys; "
                "print(any(m.startswith('PySide6') for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parent.parent))
        assert out.stdout.strip() == "False", out.stdout + out.stderr
