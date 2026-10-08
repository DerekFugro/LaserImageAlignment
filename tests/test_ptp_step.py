"""One PTP clock step in a Gocator file is corrected; anything else is refused.

20261007.120330 left laser: ptpTimestamp jumped back 13.85 ms at frame 150
while frames and encoder ran on unbroken. Refusing the file cost the whole
laser over a 14 mm clock correction (Derek, 2026-10-08: correct it).
"""
import numpy as np
import pytest

from core.formats import GocatorIndex, ParseError

HEADER = ("frameIndex,timestamp,ptpTimestamp,encoder,numberOfProfilePoints,"
          "numberOfValidPoints,bridgedValue,status,x0,z0\n")
T0 = 1791391123000000
STEP_US = 9200
NOTCH = 145


def write(tmp_path, ptp, enc=None, frames=None):
    n = len(ptp)
    enc = np.arange(n) * NOTCH if enc is None else enc
    frames = np.arange(n) if frames is None else frames
    p = tmp_path / "20261007T120330L.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER)
        for k in range(n):
            fh.write(f"{frames[k]},0,{int(ptp[k])},{int(enc[k])},2,2,0,0,1.0,2.0\n")
    return p


def clock(n=400):
    return T0 + np.arange(n, dtype=np.int64) * STEP_US


def test_one_early_step_is_corrected_onto_the_main_clock(tmp_path):
    ptp = clock()
    ptp[150:] -= 13_850                     # the clock steps back 13.85 ms
    idx = GocatorIndex.build(write(tmp_path, ptp))
    assert np.all(np.diff(idx.ptp_us) > 0)
    s = idx.ptp_step
    assert s["frame"] == 150 and s["side"] == "before" and s["moved_rows"] == 150
    assert s["jump_us"] == -13_850
    # the bulk of the run keeps its own timestamps; the short head moved
    assert idx.ptp_us[150] == ptp[150] and idx.ptp_us[0] == ptp[0] - 13_850
    assert np.all(np.diff(idx.ptp_us) == STEP_US)


def test_a_late_step_moves_the_short_tail_instead(tmp_path):
    ptp = clock()
    ptp[380:] -= 13_850
    idx = GocatorIndex.build(write(tmp_path, ptp))
    assert idx.ptp_step["side"] == "after" and idx.ptp_step["moved_rows"] == 20
    assert idx.ptp_us[0] == ptp[0]


def test_a_clean_file_has_no_step(tmp_path):
    assert GocatorIndex.build(write(tmp_path, clock())).ptp_step is None


def test_two_steps_are_refused(tmp_path):
    ptp = clock()
    ptp[100:] -= 13_850
    ptp[250:] -= 13_850
    with pytest.raises(ParseError, match="not strictly increasing"):
        GocatorIndex.build(write(tmp_path, ptp))


def test_a_big_step_is_refused(tmp_path):
    ptp = clock()
    ptp[150:] -= 80_000                     # 80 ms is not a lock correction
    with pytest.raises(ParseError, match="not strictly increasing"):
        GocatorIndex.build(write(tmp_path, ptp))


def test_a_step_where_the_laser_also_lost_frames_is_refused(tmp_path):
    ptp = clock()
    ptp[150:] -= 13_850
    frames = np.arange(400)
    frames[150:] += 5                       # frames missing across the step
    with pytest.raises(ParseError, match="not strictly increasing"):
        GocatorIndex.build(write(tmp_path, ptp, frames=frames))


def test_a_step_where_the_encoder_jumped_is_refused(tmp_path):
    ptp = clock()
    ptp[150:] -= 13_850
    enc = np.arange(400) * NOTCH
    enc[150:] += 10 * NOTCH                 # the wheel did not tick through it
    with pytest.raises(ParseError, match="not strictly increasing"):
        GocatorIndex.build(write(tmp_path, ptp, enc=enc))
