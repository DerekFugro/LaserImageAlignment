"""Status X in the ACS Daily file means DO NOT PROCESS.

Derek's rule, 2026-09-01, for F:\\Sidewalk\\099_CollectedData\\001_RoutedCollected
\\20260821_Routed - Copy. That day's Daily file has ten runs: eight 'C' and two
'x'.

    [20260821.125306]  910001  x   From 0  To 1.331  CollLength 0.03199
    [20260821.125440]  100130  x   From 0  To 0.462  CollLength 0.03099

Both are about 32 m against sections of 1331 m and 462 m - false starts. The
column is written lower case in the file and upper case in the spec, so the
test pins both.
"""
from __future__ import annotations

import csv

import pytest

from core import discovery as disc
from core.batch import MAPPING_COLUMNS, process_collection, write_run_mapping_csv
from core.daily import parse_daily
from core.discovery import OverrideStore

# The real 20260821 header and rows, trimmed to the columns parse_daily reads.
HEADER = "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Status\n"
ROWS = {
    "20260821.125306": "[20260821.125306],910001,6,0,1.331,43.43686603,-80.31707151,x\n",
    "20260821.125440": "[20260821.125440],100130,6,0,0.462,43.43680769,-80.31743631,x\n",
    "20260821.130056": "[20260821.130056],200200,6,0,0.081,43.43700000,-80.31700000,C\n",
    "20260821.130927": "[20260821.130927],201210,6,0.45,0.351,43.43710000,-80.31710000,C\n",
}


def daily(tmp_path, rows=None, day="20260821"):
    d = tmp_path / day
    d.mkdir(exist_ok=True)
    text = HEADER + "".join(ROWS[k] for k in (rows if rows is not None else ROWS))
    p = d / f"Daily_ARAN104_{day}.csv"
    p.write_text(text, encoding="utf-8-sig")
    return p


class TestParsing:
    def test_the_status_is_carried_on_the_entry(self, tmp_path):
        e = parse_daily(daily(tmp_path))
        assert e["20260821.125306"].status == "x"
        assert e["20260821.130056"].status == "C"

    def test_excluded_reads_the_x(self, tmp_path):
        e = parse_daily(daily(tmp_path))
        assert e["20260821.125306"].excluded is True
        assert e["20260821.125440"].excluded is True
        assert e["20260821.130056"].excluded is False

    def test_excluded_rows_are_kept_not_dropped(self, tmp_path):
        """Dropping them would be easier and worse - run_mapping.csv would
        then say "no folders on disk" about a run that was deliberately set
        aside."""
        assert len(parse_daily(daily(tmp_path))) == 4

    @pytest.mark.parametrize("value", ["x", "X", " x ", "X "])
    def test_case_and_whitespace_do_not_matter(self, tmp_path, value):
        p = daily(tmp_path, rows=["20260821.130056"])
        p.write_text(p.read_text(encoding="utf-8-sig").replace(",C\n", f",{value}\n"),
                     encoding="utf-8-sig")
        assert parse_daily(p)["20260821.130056"].excluded is True

    @pytest.mark.parametrize("value", ["C", "", "c", "N"])
    def test_only_x_excludes(self, tmp_path, value):
        """A blank Status is not an exclusion. Every collection before this
        column was filled in would otherwise stop dead."""
        p = daily(tmp_path, rows=["20260821.130056"])
        p.write_text(p.read_text(encoding="utf-8-sig").replace(",C\n", f",{value}\n"),
                     encoding="utf-8-sig")
        assert parse_daily(p)["20260821.130056"].excluded is False

    def test_a_daily_file_with_no_status_column_still_parses(self, tmp_path):
        """Every Daily file written before 20260821 in these collections."""
        d = tmp_path / "20260816"
        d.mkdir()
        p = d / "Daily_ARAN104_20260816.csv"
        p.write_text("FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg\n"
                     "[20260816.150000],200180,North,0.0,0.7,43.4363,-80.3152\n",
                     encoding="utf-8-sig")
        e = parse_daily(p)["20260816.150000"]
        assert e.status == "" and e.excluded is False


class TestDiscovery:
    def test_daily_stamps_leaves_the_x_runs_out(self, tmp_path):
        daily(tmp_path)
        assert disc.daily_stamps(tmp_path) == {"20260821.130056", "20260821.130927"}

    def test_excluded_stamps_names_them(self, tmp_path):
        daily(tmp_path)
        assert disc.excluded_stamps(tmp_path) == {"20260821.125306", "20260821.125440"}

    def test_no_daily_file_still_means_process_everything(self, tmp_path):
        """The guard that keeps a collection working before its Daily file is
        copied in: None, not an empty set."""
        assert disc.daily_stamps(tmp_path) is None
        assert disc.excluded_stamps(tmp_path) == set()

    def test_a_day_where_every_run_is_x_processes_nothing(self, tmp_path):
        """And is NOT confused with 'no Daily file'. An operator who marked
        the whole day off must not have the whole day run anyway."""
        daily(tmp_path, rows=["20260821.125306", "20260821.125440"])
        assert disc.daily_stamps(tmp_path) == set()


class TestBatch:
    """The end-to-end shape, on the synthetic run the other tests use."""

    def daily_for(self, root, run_id, status):
        day = root / "20260816"
        day.mkdir(exist_ok=True)
        (day / "Daily_ARAN104_20260816.csv").write_text(
            "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Status\n"
            f"Video [{run_id}] run,200180,North,0.000,0.700,43.436345,"
            f"-80.315200,{status}\n", encoding="utf-8-sig")
        return root

    def run(self, root, synth_cal_dir, tmp_path):
        return process_collection(root, calibrations_dir=synth_cal_dir,
                                  overrides=OverrideStore(tmp_path / "ov.json"))

    def test_a_C_run_is_processed(self, synth_run_root, synth_cal_dir, tmp_path):
        root = self.daily_for(synth_run_root, "20260816.150000", "C")
        r = self.run(root, synth_cal_dir, tmp_path)
        assert [o.run_id for o in r.outcomes] == ["20260816.150000"]
        assert r.excluded == []

    def test_an_x_run_is_not_processed(self, synth_run_root, synth_cal_dir, tmp_path):
        root = self.daily_for(synth_run_root, "20260816.150000", "x")
        r = self.run(root, synth_cal_dir, tmp_path)
        assert r.outcomes == []
        assert r.excluded == ["20260816.150000"]

    def test_an_x_run_is_not_reported_as_unregistered(self, synth_run_root,
                                                      synth_cal_dir, tmp_path):
        """`ignored` means "no row in the Daily file". An X run has a row.
        Putting it there would send somebody hunting for a missing row."""
        root = self.daily_for(synth_run_root, "20260816.150000", "x")
        r = self.run(root, synth_cal_dir, tmp_path)
        assert r.ignored == []
        assert "marked Status X" in r.to_text()

    def test_the_mapping_row_says_why(self, synth_run_root, synth_cal_dir, tmp_path):
        """run_mapping.csv is what somebody opens to ask "what can I run".
        For an X run it must say 'told not to', not 'nothing on disk'."""
        root = self.daily_for(synth_run_root, "20260816.150000", "x")
        r = self.run(root, synth_cal_dir, tmp_path)
        out = write_run_mapping_csv(r, tmp_path / "run_mapping.csv")
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "not_processed"
        assert "marked not to be processed" in row["note"]
        assert "no folders on disk" not in row["note"]
        assert row["section"] == "200180"          # still identified
        assert set(row) == set(MAPPING_COLUMNS)

    def test_nothing_was_written_into_the_x_run(self, synth_run_root,
                                                synth_cal_dir, tmp_path):
        """The point of the rule. Not processed means not touched: no rename,
        no manifest, no BeforeCollection."""
        root = self.daily_for(synth_run_root, "20260816.150000", "x")
        before = {p.name for p in (root / "Images" / "20260816.150000").rglob("*")}
        self.run(root, synth_cal_dir, tmp_path)
        after = {p.name for p in (root / "Images" / "20260816.150000").rglob("*")}
        assert after == before
