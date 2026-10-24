"""PTP steps undone against the laser's own sensor clock (2026-10-24).

Every PTP step seen so far came in a +X, +2X, -X pattern at the start of a
run (20261016.124201 R: +62, +124, -62 ms at rows 7, 13, 33), or as one
forward step that never broke "strictly increasing" (20261005.100110 L:
+115 ms). The sensor clock (`timestamp` column) runs on unbroken through all
of them, so it shows where the PTP time sat off its steady line.
"""
import numpy as np
import pytest

from core.formats import GocatorIndex, ParseError

HEADER = ("frameIndex,timestamp,ptpTimestamp,encoder,numberOfProfilePoints,"
          "numberOfValidPoints,bridgedValue,status,x0,z0\n")
N = 600
STEP_US = 21_000
NOTCH = 136


def write(tmp_path, ptp, ts=None, frames=None, enc=None, name="20261016T124201R.csv"):
    ts = (17_920_000_000 + (np.arange(N) * STEP_US * 1.02)).astype(np.int64) if ts is None else ts
    frames = np.arange(N) if frames is None else frames
    enc = np.arange(N) * NOTCH if enc is None else enc
    p = tmp_path / name
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER)
        for k in range(N):
            fh.write(f"{frames[k]},{int(ts[k])},{int(ptp[k])},{int(enc[k])},2,2,0,0,1.0,2.0\n")
    return p


def clock():
    return 1792168914000000 + np.arange(N, dtype=np.int64) * STEP_US


def stepped(steps):
    """steps: [(row, +ms)] - the PTP time jumps by +ms from that row on."""
    ptp = clock()
    for row, ms in steps:
        ptp[row:] += int(ms * 1000)
    return ptp


def test_a_clean_file_is_untouched(tmp_path):
    ptp = clock()
    idx = GocatorIndex.build(write(tmp_path, ptp))
    assert idx.ptp_step is None and idx.ptp_suspect is None
    assert np.array_equal(idx.ptp_us, ptp)


def test_the_124201_pattern_is_put_back_on_one_clock(tmp_path):
    # +62 at row 7, +124 at row 13, -62 at row 33: rows 0-32 end up
    # 124 / 62 / -62 ms off the steady part from row 33 on
    ptp = stepped([(7, 62), (13, 124), (33, -62)])
    idx = GocatorIndex.build(write(tmp_path, ptp))
    s = idx.ptp_step
    assert s["method"] == "sensor clock" and s["moved_rows"] == 33
    assert s["step_frames"] == [7, 13, 33]
    true = clock() + (ptp[33] - clock()[33])        # the steady clock
    assert np.max(np.abs(idx.ptp_us - true)) <= 50   # every row, to 0.05 ms


def test_a_forward_only_step_is_found_and_undone(tmp_path):
    """20261005.100110 L: one forward step, the file always loaded and its
    first profiles were 115 ms off with no warning."""
    ptp = stepped([(40, 115)])
    idx = GocatorIndex.build(write(tmp_path, ptp))
    assert idx.ptp_step["moved_rows"] == 40
    assert np.max(np.abs(idx.ptp_us - (clock() + 115_000))) <= 50


def test_a_step_where_the_sensor_clock_also_jumped_is_not_moved(tmp_path):
    ptp = stepped([(40, 115)])
    ts = (17_920_000_000 + (np.arange(N) * STEP_US * 1.02)).astype(np.int64)
    ts[40:] += 115_000                       # the laser itself paused
    idx = GocatorIndex.build(write(tmp_path, ptp, ts=ts))
    assert idx.ptp_step is None and idx.ptp_suspect is None   # both clocks agree: no PTP step
    assert np.array_equal(idx.ptp_us, ptp)


def test_a_step_with_missing_frames_warns_and_moves_nothing(tmp_path):
    ptp = stepped([(40, 115)])
    frames = np.arange(N)
    frames[40:] += 3
    idx = GocatorIndex.build(write(tmp_path, ptp, frames=frames))
    assert idx.ptp_step is None
    assert "frames missing" in idx.ptp_suspect["why"]
    assert np.array_equal(idx.ptp_us, ptp)


def test_a_step_in_the_middle_is_not_moved(tmp_path):
    """Half the run on each side: no steady part to trust, so nothing moves."""
    ptp = stepped([(300, 40)])
    idx = GocatorIndex.build(write(tmp_path, ptp))
    assert idx.ptp_step is None and "too many to move" in idx.ptp_suspect["why"]


def test_when_the_sensor_repair_declines_the_old_repair_runs_as_before(tmp_path):
    """No file may be refused that loaded before: a mid-run backward step the
    sensor repair will not move gets the older one-step repair, as it did,
    and the warning says the sensor clock disagreed."""
    ptp = stepped([(300, -40)])
    idx = GocatorIndex.build(write(tmp_path, ptp))
    assert idx.ptp_step["jump_us"] == -40_000 and "method" not in idx.ptp_step
    assert idx.ptp_suspect is not None


def test_a_big_mid_run_backward_step_is_still_refused(tmp_path):
    ptp = stepped([(300, -400)])             # mid-run, backwards, 0.4 s
    with pytest.raises(ParseError, match="not strictly increasing"):
        GocatorIndex.build(write(tmp_path, ptp))
