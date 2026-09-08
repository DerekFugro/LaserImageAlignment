"""Run discovery — in particular, pairing a run with its SBG log folder.

Written after 20260824_revruns (Derek, 2026-08-26): the collection system named
one run 20260824.100258 while the SBG logger named the same physical run
"20260824.100259 DataLogger". Matched on the exact string, that ONE run was
discovered as TWO — images with no triggers, triggers with no images — and both
were skipped by the batch, so 86 images and 2 Gocator CSVs silently received no
GPS. The batch report cheerfully said "13 runs" for a 12-run day.

The pairing is deliberately narrow. Runs start about a minute apart, so a few
seconds of tolerance cannot reach a neighbour; and where it cannot be sure, it
must leave the run unpaired (a loud, checkable failure) rather than hand a run
the WRONG run's triggers, which would write wrong positions into real images.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import discovery as disc


def make_run_root(tmp_path, collection_stamps, sbg_stamps):
    """A run root with image/Gocator folders under one set of stamps and SBG
    log folders under another — the two systems named independently."""
    root = tmp_path / "root"
    for stamp in collection_stamps:
        date, tm = stamp.split(".")
        rear = root / "Images" / stamp / "Rear"
        rear.mkdir(parents=True)
        (rear / "000000000750.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
        goc = root / "GoCatorData" / stamp
        goc.mkdir(parents=True)
        for side in "LR":
            (goc / f"{date}T{tm}{side}.csv").write_text("header\n", encoding="utf-8")
    for stamp in sbg_stamps:
        logger = root / "SBGData" / "260824" / f"{stamp} DataLogger"
        logger.mkdir(parents=True)
        (logger / "eventOutA.txt").write_text("0\n", encoding="utf-8")
        (logger / "utcTime.txt").write_text("0\n", encoding="utf-8")
        (logger / "DmiStationEx 001.csv").write_text("0\n", encoding="utf-8")
    return root


def runs_for(tmp_path, collection_stamps, sbg_stamps):
    root = make_run_root(tmp_path, collection_stamps, sbg_stamps)
    empty_cal = tmp_path / "no_calibrations"
    empty_cal.mkdir()
    return {r.run_id: r for r in disc.discover_runs(root, calibrations_dir=empty_cal)}


def test_a_one_second_skew_is_still_one_run(tmp_path):
    """The 20260824 case: 100258 in Images/Gocator, 100259 in SBGData."""
    runs = runs_for(tmp_path, ["20260824.100258"], ["20260824.100259"])

    assert list(runs) == ["20260824.100258"], \
        "one physical run must not be discovered as two"
    run = runs["20260824.100258"]
    assert run.get(disc.KEY_EVENT_A) is not None
    assert run.get(disc.KEY_UTC_TIME) is not None
    assert run.get(disc.KEY_IMAGES) is not None
    assert disc.KEY_EVENT_A not in run.missing()


def test_the_run_keeps_the_collection_systems_name(tmp_path):
    """run_id names the export CSV, the Ortho folders and every saved bar
    measurement. Following the SBG clock instead would orphan all of them."""
    runs = runs_for(tmp_path, ["20260824.100258"], ["20260824.100259"])
    run = runs["20260824.100258"]

    assert run.run_id == "20260824.100258"
    assert run.sbg_stamp == "20260824.100259"
    assert run.sbg_skew_s == 1


def test_an_exact_match_reports_no_skew(tmp_path):
    runs = runs_for(tmp_path, ["20260824.100258"], ["20260824.100258"])
    run = runs["20260824.100258"]

    assert run.sbg_stamp == "20260824.100258"
    assert run.sbg_skew_s == 0


def test_an_exact_match_wins_over_a_near_neighbour(tmp_path):
    """Two runs two seconds apart, both loggers present. Greedy nearest-first
    must not let the first run steal the second run's logger."""
    runs = runs_for(tmp_path,
                    ["20260824.100258", "20260824.100260"],
                    ["20260824.100258", "20260824.100260"])

    assert runs["20260824.100258"].sbg_stamp == "20260824.100258"
    assert runs["20260824.100260"].sbg_stamp == "20260824.100260"
    assert all(r.sbg_skew_s == 0 for r in runs.values())


def test_one_logger_is_never_given_to_two_runs(tmp_path):
    """Only one logger for two nearby runs: the closer run gets it, the other
    is left plainly missing its triggers rather than quietly sharing them."""
    runs = runs_for(tmp_path,
                    ["20260824.100258", "20260824.100261"],
                    ["20260824.100259"])

    assert runs["20260824.100258"].sbg_stamp == "20260824.100259"
    assert runs["20260824.100261"].sbg_stamp == ""
    assert disc.KEY_EVENT_A in runs["20260824.100261"].missing()


def test_a_logger_beyond_the_tolerance_is_not_pulled_in(tmp_path):
    """A minute away is a different run, not a clock skew."""
    runs = runs_for(tmp_path, ["20260824.100258"], ["20260824.100358"])

    assert set(runs) == {"20260824.100258", "20260824.100358"}
    assert runs["20260824.100258"].sbg_stamp == ""
    assert disc.KEY_EVENT_A in runs["20260824.100258"].missing()


def test_a_logger_with_no_run_is_still_listed(tmp_path):
    """A log folder with no images must stay visible in the run list — an
    unexplained run is a question to answer, not one to hide."""
    runs = runs_for(tmp_path, [], ["20260824.100259"])

    assert list(runs) == ["20260824.100259"]
    assert runs["20260824.100259"].get(disc.KEY_EVENT_A) is not None
    assert disc.KEY_IMAGES in runs["20260824.100259"].missing()


def test_the_skew_crosses_midnight_by_time_not_by_string(tmp_path):
    """23:59:59 and 00:00:00 the next day are one second apart; as strings they
    look nothing alike."""
    runs = runs_for(tmp_path, ["20260824.235959"], ["20260825.000000"])

    assert list(runs) == ["20260824.235959"]
    assert runs["20260824.235959"].sbg_skew_s == 1


def shifted(stamp: str, seconds: int) -> str:
    base = datetime.strptime(stamp, "%Y%m%d.%H%M%S")
    return (base + timedelta(seconds=seconds)).strftime("%Y%m%d.%H%M%S")


@pytest.mark.parametrize("skew", [-3, -1, 0, 1, 3])
def test_skew_is_tolerated_in_both_directions(skew):
    """The logger can start either before or after the collection system."""
    paired = disc.pair_sbg_stamps(["20260824.100300"],
                                  [shifted("20260824.100300", skew)])
    assert paired["20260824.100300"][1] == skew


@pytest.mark.parametrize("skew", [-4, 4, 61, -61])
def test_a_bigger_gap_is_left_alone(skew):
    paired = disc.pair_sbg_stamps(["20260824.100300"],
                                  [shifted("20260824.100300", skew)])
    assert paired == {}


def test_the_pairing_is_reported_so_it_is_never_silent(tmp_path):
    """A fuzzy pairing that nobody is told about is how wrong triggers end up
    written into real images."""
    from core.qc import QCReport, Severity, check_presence

    runs = runs_for(tmp_path, ["20260824.100258"], ["20260824.100259"])
    report = QCReport(run_id="20260824.100258")
    check_presence(runs["20260824.100258"], report)

    check = next(c for c in report.checks if c.check_id == "presence.sbg_stamp_skew")
    assert check.severity == Severity.WARN
    assert "20260824.100259" in check.message


def test_an_exact_pairing_says_nothing(tmp_path):
    """No warning where there is nothing to warn about — a QC panel that cries
    wolf on every run is a QC panel nobody reads."""
    from core.qc import QCReport, check_presence

    runs = runs_for(tmp_path, ["20260824.100258"], ["20260824.100258"])
    report = QCReport(run_id="20260824.100258")
    check_presence(runs["20260824.100258"], report)

    assert not [c for c in report.checks if c.check_id == "presence.sbg_stamp_skew"]
