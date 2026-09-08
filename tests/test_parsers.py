"""Unit tests for core.formats parsers on synthetic fixtures."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from core.formats import (
    DmiTable, GocatorIndex, ImageSet, NavTable, ParseError, UtcTable,
    parse_event_triggers,
)
from tests.conftest import T0_UTC, SBG0_US, write_sbg_table


class TestEventTriggers:
    def test_parses_synthetic_run(self, synth_run_root):
        path = next(synth_run_root.rglob("eventOutA.txt"))
        ts = parse_event_triggers(path)
        assert ts.dtype == np.int64
        assert np.all(np.diff(ts) > 0)

    def test_rejects_non_monotonic(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        write_sbg_table(p, ["timestamp", "status"], ["(us)", "(na)"],
                        [["100", "0x0"], ["50", "0x0"]])
        with pytest.raises(ParseError, match="not strictly increasing"):
            parse_event_triggers(p)

    def test_rejects_wrong_first_column(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        write_sbg_table(p, ["bogus", "status"], ["(us)", "(na)"], [["1", "0x0"]])
        with pytest.raises(ParseError, match="expected first column"):
            parse_event_triggers(p)

    def test_rejects_garbage_line_with_line_number(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        write_sbg_table(p, ["timestamp", "status"], ["(us)", "(na)"],
                        [["100", "0x0"], ["garbage", "0x0"]])
        with pytest.raises(ParseError, match=r":4: non-numeric"):
            parse_event_triggers(p)


class TestUtcTable:
    def test_roundtrip(self, synth_run_root):
        path = next(synth_run_root.rglob("utcTime.txt"))
        utc = UtcTable.parse(path)
        # SBG0_US maps to T0_UTC exactly
        assert utc.sbg_to_utc(SBG0_US) == pytest.approx(T0_UTC, abs=1e-6)
        # halfway between rows interpolates linearly
        assert utc.sbg_to_utc(SBG0_US + 500_000) == pytest.approx(T0_UTC + 0.5, abs=1e-6)

    def test_edge_extrapolation(self, synth_run_root):
        utc = UtcTable.parse(next(synth_run_root.rglob("utcTime.txt")))
        before = utc.sbg_to_utc(int(utc.sbg_us[0]) - 1_000_000)
        assert before == pytest.approx(float(utc.epoch_s[0]) - 1.0, abs=1e-3)

    def test_utc_date_anchor(self, synth_run_root):
        utc = UtcTable.parse(next(synth_run_root.rglob("utcTime.txt")))
        assert utc.utc_date.date() == datetime.fromtimestamp(
            T0_UTC, tz=timezone.utc).date()


class TestDmiTable:
    def test_parses_and_interpolates(self, synth_run_root):
        path = next(synth_run_root.rglob("DmiStationEx *.csv"))
        dmi = DmiTable.parse(path)
        assert dmi.total_dist_m > 100
        # standstill first 5 s
        assert dmi.dist_at(T0_UTC + 2.0) == pytest.approx(0.0, abs=0.01)
        assert dmi.speed_at(T0_UTC + 50.0) == pytest.approx(1.5, abs=0.02)

    def test_allows_mm_jitter_rejects_reversal(self, tmp_path):
        p = tmp_path / "DmiStationEx x.csv"
        p.write_text("Dmi (m), Speed (m/s), UTC TS (s), TimeOfWeek (s)\n"
                     "1.000, 0.0, 100.0, 0\n0.998, 0.0, 100.1, 0\n1.001, 0.0, 100.2, 0\n")
        dmi = DmiTable.parse(p)  # 2 mm jitter OK (verified real behaviour)
        assert dmi.total_dist_m == pytest.approx(1.001)
        p.write_text("Dmi (m), Speed (m/s), UTC TS (s), TimeOfWeek (s)\n"
                     "5.000, 0.0, 100.0, 0\n4.000, 0.0, 100.1, 0\n")
        with pytest.raises(ParseError, match="reverses"):
            DmiTable.parse(p)

    def test_rejects_wrong_header(self, tmp_path):
        p = tmp_path / "DmiStationEx x.csv"
        p.write_text("a,b,c\n1,2,3\n")
        with pytest.raises(ParseError, match="unexpected header"):
            DmiTable.parse(p)


class TestNavTable:
    def anchor(self):
        return datetime.fromtimestamp(T0_UTC, tz=timezone.utc)

    def test_parses_and_locates(self, synth_run_root):
        path = next(synth_run_root.rglob("ascii-output.txt"))
        nav = NavTable.parse(path, anchor_utc_date=self.anchor())
        assert nav.max_gap_s < 1.0
        assert nav.gps_minus_utc_s == pytest.approx(18.0, abs=0.1)
        loc = nav.locate(T0_UTC + 50.0)
        assert 43.4 < float(loc["lat_deg"]) < 43.5
        assert -80.4 < float(loc["lon_deg"]) < -80.3

    def test_never_extrapolates(self, synth_run_root):
        nav = NavTable.parse(next(synth_run_root.rglob("ascii-output.txt")),
                             anchor_utc_date=self.anchor())
        loc = nav.locate(nav.epoch_s[0] - 10.0)
        assert np.isnan(float(loc["lat_deg"]))
        loc = nav.locate(nav.epoch_s[-1] + 10.0)
        assert np.isnan(float(loc["lat_deg"]))

    @staticmethod
    def _write_nav(path, rows):
        path.write_text(
            "v\np\nc\n\n"
            "GPS Time\tUTC Time\tYaw\tLatitude\tLongitude\tAltitude MSL\n"
            "(S)\t(H)\t(°)\t(°)\t(°)\t(m)\n" + "".join(rows),
            encoding="utf-8")
        return path

    def test_the_export_tail_is_not_read(self, tmp_path):
        """Qinertia writes its last rows as the solution shuts down — on
        20260824 the final one is (0,0) with a NaN altitude, 8,095 km from
        site. They fall after the last run, so they are simply not read."""
        from core.formats import TRAILING_ROWS_IGNORED
        rows = [f"{i}.0\t12:00:{i:02d}.000\t0.0\t{43.0 + i * 1e-4:.6f}\t-80.0\t100.0\n"
                for i in range(10)]
        rows[-1] = "9.0\t12:00:09.000\t0.0\t0.000000\t0.000000\t100.0\n"
        p = self._write_nav(tmp_path / "ascii-output.txt", rows)
        nav = NavTable.parse(p, anchor_utc_date=self.anchor())
        assert len(nav.epoch_s) == 10 - TRAILING_ROWS_IGNORED
        # the (0,0) row is gone, and so is the good one beside it
        assert not np.any(nav.lat_deg == 0.0)
        assert float(nav.lat_deg[-1]) == pytest.approx(43.0 + 7 * 1e-4, abs=1e-9)
        # and nothing is reported about it: the tail is not a finding
        assert nav.n_dropped_nofix == 0

    def test_an_unconverged_row_inside_the_session_is_still_reported(self, tmp_path):
        """Only the TAIL is ignored. Junk in the middle could sit under a run,
        so it is dropped AND counted."""
        rows = [f"{i}.0\t12:00:{i:02d}.000\t0.0\t{43.0 + i * 1e-4:.6f}\t-80.0\t100.0\n"
                for i in range(10)]
        rows[4] = "4.0\t12:00:04.000\t0.0\t0.000000\t0.000000\t100.0\n"
        p = self._write_nav(tmp_path / "ascii-output.txt", rows)
        nav = NavTable.parse(p, anchor_utc_date=self.anchor())
        assert nav.n_dropped_nofix == 1
        assert not np.any(nav.lat_deg == 0.0)

    def test_a_tiny_export_is_never_trimmed_away(self, tmp_path):
        """Two rows is a fixture or a broken file, not a survey — trimming
        would leave nothing to interpolate."""
        rows = ["1.0\t12:00:01.000\t0.0\t43.0\t-80.0\t100.0\n",
                "2.0\t12:00:02.000\t0.0\t43.1\t-80.0\t100.0\n"]
        p = self._write_nav(tmp_path / "ascii-output.txt", rows)
        nav = NavTable.parse(p, anchor_utc_date=self.anchor())
        assert len(nav.epoch_s) == 2

    def test_midnight_rollover(self, tmp_path):
        p = tmp_path / "ascii-output.txt"
        p.write_text(
            "v\np\nc\n\n"
            "GPS Time\tUTC Time\tYaw\tLatitude\tLongitude\tAltitude MSL\n"
            "(S)\t(H)\t(°)\t(°)\t(°)\t(m)\n"
            "1.0\t23:59:59.000\t0.0\t43.0\t-80.0\t100.0\n"
            "2.0\t00:00:01.000\t0.0\t43.1\t-80.1\t101.0\n",
            # the degree signs are UTF-8, as the real export is, and the parser
            # reads strict UTF-8. Without this, write_text uses the platform
            # encoding and the test fails on Windows only.
            encoding="utf-8")
        anchor = datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc)
        nav = NavTable.parse(p, anchor_utc_date=anchor)
        assert nav.epoch_s[1] - nav.epoch_s[0] == pytest.approx(2.0)

    def make_table(self, yaw_deg):
        n = len(yaw_deg)
        return NavTable(
            epoch_s=T0_UTC + np.arange(n, dtype=float),
            lat_deg=43.0 + np.arange(n) * 1e-5, lon_deg=-80.0 + np.arange(n) * 1e-5,
            alt_m=np.full(n, 300.0), yaw_deg=np.asarray(yaw_deg, dtype=float),
            gps_tow_s=np.arange(n, dtype=float),
        )

    def test_heading_interpolates_across_the_wrap(self):
        """359 deg and 1 deg must average to 0 deg, not 180 deg."""
        nav = self.make_table([359.0, 1.0])
        assert float(nav.heading_at(T0_UTC + 0.5)) % 360.0 == pytest.approx(0.0, abs=0.01)

    def test_heading_matches_plain_interp_away_from_the_wrap(self):
        nav = self.make_table([90.0, 100.0])
        assert float(nav.heading_at(T0_UTC + 0.5)) == pytest.approx(95.0, abs=0.01)

    def test_heading_is_nan_for_nan_query(self):
        nav = self.make_table([10.0, 20.0])
        assert np.isnan(float(nav.heading_at(np.nan)))

    def test_missing_header_fails(self, tmp_path):
        p = tmp_path / "ascii-output.txt"
        p.write_text("no header here\n1\t2\n")
        with pytest.raises(ParseError, match="GPS Time"):
            NavTable.parse(p, anchor_utc_date=self.anchor())


class TestGocatorIndex:
    def test_index_and_profile(self, synth_run_root):
        path = next(synth_run_root.rglob("*L.csv"))
        idx = GocatorIndex.build(path)
        assert len(idx) > 100
        assert np.all(np.diff(idx.ptp_us) > 0)
        prof = idx.read_profile(0)
        assert prof.shape == (3, 2)  # 4 pairs, one blank -> 3 valid
        assert prof[0, 0] == pytest.approx(-10.0)

    def test_bad_header_fails(self, tmp_path):
        p = tmp_path / "g.csv"
        p.write_text("nope\n1,2,3\n")
        with pytest.raises(ParseError, match="unexpected Gocator header"):
            GocatorIndex.build(p)


class TestImageSet:
    def test_scan_and_gaps(self, tmp_path):
        d = tmp_path / "Rear"
        d.mkdir()
        for c in (750, 1500, 3000):  # 2250 missing
            (d / f"{c:012d}.jpg").write_bytes(b"\xff\xd8x")
        s = ImageSet.scan(d)
        assert len(s) == 3
        assert s.sequence_gaps() == [2250]
        assert s.dist_m[0] == pytest.approx(0.75)
        assert s.is_readable_jpeg(0)

    def test_empty_dir_fails(self, tmp_path):
        d = tmp_path / "Rear"
        d.mkdir()
        with pytest.raises(ParseError, match="no numbered"):
            ImageSet.scan(d)
