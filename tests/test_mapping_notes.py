"""run_mapping.csv notes give the reason first, on one line (orchestrator,
2026-10-23: a blank 'partial' note and 'ran but something failed - see
issues_*.csv' told the review log nothing)."""
import csv

from core.batch import BatchReport, RunOutcome, write_run_mapping_csv


def _notes(tmp_path, *outcomes):
    rep = BatchReport(root=str(tmp_path), started_utc="2026-10-23T00:00:00+00:00")
    rep.outcomes.extend(outcomes)
    out = write_run_mapping_csv(rep, tmp_path / "run_mapping.csv")
    return {r["run_id"]: r["note"] for r in
            csv.DictReader(open(out, newline="", encoding="utf-8"))}


def test_partial_says_what_was_written_and_what_was_held(tmp_path):
    o = RunOutcome(run_id="20261015.165518", ok=False, status="partial")
    o.held = {"gocator_L": "laser L recorded 1 profile(s) for a 496 m run - "
                           "99.99% of its data is missing",
              "gocator_R": "laser R recorded 3 profile(s)"}
    note = _notes(tmp_path, o)["[20261015.165518]"]
    assert note.startswith("images and alignment table written; held: laser L, "
                           "laser R - laser L recorded 1 profile(s)")
    assert "\n" not in note


def test_flagged_gives_the_first_failure(tmp_path):
    o = RunOutcome(run_id="20261016.172652", ok=False, status="flagged")
    o.failures = ["format.gocator_l: F:\\x\\20261016T172652L.csv: no profile rows",
                  "format.gocator_r: F:\\x\\20261016T172652R.csv: no profile rows"]
    note = _notes(tmp_path, o)["[20261016.172652]"]
    assert note.startswith("format.gocator_l: F:\\x\\20261016T172652L.csv: no profile rows")
    assert note.split(";")[0].endswith("- see issues_*.csv")
    assert "ran but something failed" not in note


def test_written_runs_get_no_reason(tmp_path):
    o = RunOutcome(run_id="20261015.162205", ok=True, status="written")
    assert _notes(tmp_path, o)["[20261015.162205]"] in ("", "no row in the ACS Daily file")
