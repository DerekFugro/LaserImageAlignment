"""An empty Daily, a filled Daily, and QC_Video per day (2026-11-20).

BuggyProcessed 20261013: the crew copied a header-only Daily. Given it, the
batch read it as "no Daily", checked all 369 run folders on the drive (Sep 29 -
Oct 17), wrote none, and still printed "AFFECTED COLLECTION: none - every
deliverable was written"."""
from pathlib import Path

import cli
from core import discovery as disc
from core.batch import BatchReport, RunOutcome, process_collection
from core.daily import find_qc_video_file, parse_daily
from tests.test_daily_selection import DAILY_HEADER, _day, _row


def _run_folders(root: Path, *stamps):
    for s in stamps:
        (root / "Images" / s / "Rear").mkdir(parents=True, exist_ok=True)


def test_header_only_daily_lists_nothing(tmp_path):
    empty = _day(tmp_path, "20261013", [])
    _run_folders(tmp_path, "20261013.104109", "20261014.094844")
    assert disc.empty_daily(tmp_path, empty) == empty
    assert disc.daily_stamps(tmp_path, empty) == set()
    assert disc.discover_runs(tmp_path, daily_path=empty) == []


def test_a_daily_with_rows_is_not_empty(tmp_path):
    full = _day(tmp_path, "20261014", [_row("20261014.094844", status="X")])
    assert disc.empty_daily(tmp_path, full) is None      # all X is not empty


def test_batch_refuses_it_and_says_so(tmp_path):
    empty = _day(tmp_path, "20261013", [])
    _run_folders(tmp_path, "20261013.104109")
    rep = process_collection(tmp_path, daily_path=empty, write_images=False,
                             write_gocator=False, write_csv=False)
    assert rep.aborted and not rep.outcomes and "lists no runs" in rep.abort_reason


def test_cli_exits_2_on_it(tmp_path, capsys):
    empty = _day(tmp_path, "20261013", [])
    _run_folders(tmp_path, "20261013.104109")
    for cmd in ("process", "check", "runs"):
        assert cli.main([cmd, str(tmp_path), "--daily", str(empty)]) == cli.EXIT_CANNOT_RUN
        assert "lists no runs" in capsys.readouterr().err


def test_affected_line_when_nothing_was_written():
    rep = BatchReport(root="x", started_utc="t")
    rep.outcomes = [RunOutcome(run_id=f"2026101{i}.000000", ok=False, status="skipped")
                    for i in range(3)]
    text = rep.affected_text()
    assert "none" not in text.lower().split(":")[1][:6]
    assert "3 run(s) skipped" in text and "nothing was written" in text


def test_qc_video_is_read_for_the_run_s_own_day(tmp_path):
    for day in ("20261013", "20261014"):
        (tmp_path / day).mkdir()
        (tmp_path / day / "QC_Video.csv").write_text("Session Name\n", encoding="utf-8")
    assert find_qc_video_file(tmp_path, "20261014.094844").parent.name == "20261014"
    assert find_qc_video_file(tmp_path, "20261015.090000") is None   # not day one's


def test_rows_added_by_the_orchestrator_are_known(tmp_path):
    p = tmp_path / "20261015" / "Daily_FILLED_20261015.csv"
    p.parent.mkdir()
    p.write_text("FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed,Status,Comments\n"
                 "[20261015.144323],REBUILT,North,0,0.31,43.1,-80.1,1,C,"
                 "Added by orchestrator - not in the ACS Daily; section unknown\n"
                 "[20261015.090853],307480,North,0,0.2,43.1,-80.1,1,C,\n",
                 encoding="utf-8-sig")
    rows = parse_daily(p)
    assert rows["20261015.144323"].added_by_orchestrator
    assert not rows["20261015.090853"].added_by_orchestrator


def test_an_unreadable_daily_still_keeps_to_its_own_day(tmp_path):
    (tmp_path / "20261013").mkdir()
    gone = tmp_path / "20261013" / "Daily_ARAN01_20261013.csv"     # not there
    _run_folders(tmp_path, "20261013.104109", "20261014.094844")
    found = {r.run_id for r in disc.discover_runs(tmp_path, daily_path=gone)}
    assert found == {"20261013.104109"}


def test_the_report_names_only_this_day_s_folders(tmp_path):
    """Each day's report listed ~330 folders of OTHER days as "ignored"
    (BuggyProcessed). Only this day's unlisted folders matter (Derek,
    2026-11-20)."""
    daily = _day(tmp_path, "20261014", [_row("20261014.094844")])
    _run_folders(tmp_path, "20261013.104109", "20261014.094844", "20261014.092105",
                 "20261015.090853")
    (tmp_path / "SBGData" / "261014" / "20261014.092157 DataLogger").mkdir(parents=True)
    rep = process_collection(tmp_path, daily_path=daily, write_images=False,
                             write_gocator=False, write_csv=False)
    assert rep.ignored == ["20261014.092105"]      # not the logger-only .092157


def test_a_filled_daily_outside_the_day_folder_knows_its_day(tmp_path):
    filled = tmp_path / "Processed" / "_orchestrator" / "filled" / "Daily_FILLED_20261014.csv"
    assert disc._day_of(filled) == "20261014"
