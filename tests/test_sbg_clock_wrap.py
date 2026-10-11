"""SBG clock roll-over: the DataLogger's 32-bit microsecond clock wraps every
2**32 us (71.6 min). Seen for real on ARANSW1 20261008.111537."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from core.formats import (
    SBG_CLOCK_WRAP_US, ParseError, UtcTable, parse_event_triggers, unwrap_sbg_us,
)
from tests.conftest import write_sbg_table

W = SBG_CLOCK_WRAP_US
T0 = datetime(2026, 10, 8, 15, 15, 0, tzinfo=timezone.utc)


def write_triggers(path, ts):
    write_sbg_table(path, ["timestamp", "status"], ["(us)", "(na)"],
                    [[str(int(t)), "0x0000"] for t in ts])


def write_utc(path, sbg_list, start=T0):
    rows = []
    for i, s in enumerate(sbg_list):
        t = datetime.fromtimestamp(start.timestamp() + i, tz=timezone.utc)
        rows.append([str(int(s)), str(t.year), str(t.month), str(t.day), str(t.hour),
                     str(t.minute), str(t.second), "0"])
    write_sbg_table(path, ["timestamp", "year", "month", "day", "hour", "minute",
                           "second", "nanosecond"], ["(us)"] + ["(na)"] * 7, rows)


def wrapped(values):
    return [int(v) % W for v in values]


class TestUnwrap:
    def test_no_wrap_is_untouched(self):
        ts, n = unwrap_sbg_us([1, 2, 3])
        assert n == 0 and ts.tolist() == [1, 2, 3]

    def test_one_wrap(self):
        ts, n = unwrap_sbg_us([W - 200_000, W - 100_000, 50_000])
        assert n == 1 and np.all(np.diff(ts) > 0)
        assert ts[-1] - ts[-2] == 150_000

    def test_small_backward_step_is_not_a_wrap(self):
        ts, n = unwrap_sbg_us([1_000_000, 900_000])
        assert n == 0 and ts.tolist() == [1_000_000, 900_000]


class TestTriggers:
    def test_wrapped_triggers_parse_and_count_up(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        true = [W - 600_000 + k * 400_000 for k in range(5)]
        write_triggers(p, wrapped(true))
        ts, n = parse_event_triggers(p, with_wraps=True)
        assert n == 1
        assert np.diff(ts).tolist() == [400_000] * 4

    def test_a_real_reversal_still_fails(self, tmp_path):
        p = tmp_path / "eventOutA.txt"
        write_triggers(p, [5_000_000, 4_000_000])
        with pytest.raises(ParseError, match="not strictly increasing"):
            parse_event_triggers(p)


class TestUtcAcrossWrap:
    def table(self, tmp_path, first_us):
        p = tmp_path / "utcTime.txt"
        write_utc(p, wrapped([first_us + k * 1_000_000 for k in range(10)]))
        return UtcTable.parse(p)

    def test_table_spanning_wrap_maps_both_sides(self, tmp_path):
        first = W - 3_500_000                      # wraps between rows 3 and 4
        utc = self.table(tmp_path, first)
        assert utc.n_wraps == 1
        for k in (1.5, 3.5, 6.25):
            raw = int(first + k * 1_000_000) % W   # as the trigger file logs it
            assert utc.sbg_to_utc([raw])[0] == pytest.approx(T0.timestamp() + k, abs=1e-6)

    def test_triggers_across_wrap_match_table(self, tmp_path):
        first = W - 3_500_000
        utc = self.table(tmp_path, first)
        p = tmp_path / "eventOutA.txt"
        true_k = [1.2, 2.9, 4.1, 7.7]
        write_triggers(p, wrapped([first + k * 1_000_000 for k in true_k]))
        got = utc.sbg_to_utc(parse_event_triggers(p))
        assert got == pytest.approx([T0.timestamp() + k for k in true_k], abs=1e-6)

    def test_trigger_file_starting_after_the_wrap(self, tmp_path):
        # utcTime starts before the roll-over, eventOutA only after it: the
        # trigger file unwraps from its own (post-wrap) first row and must be
        # lifted a whole wrap onto the table
        first = W - 3_500_000
        utc = self.table(tmp_path, first)
        p = tmp_path / "eventOutA.txt"
        true_k = [4.5, 5.5, 8.0]
        write_triggers(p, wrapped([first + k * 1_000_000 for k in true_k]))
        got = utc.sbg_to_utc(parse_event_triggers(p))
        assert got == pytest.approx([T0.timestamp() + k for k in true_k], abs=1e-6)
