"""Trigger stand-ins when the DataLogger's eventOutA.txt / utcTime.txt are
unusable (Batch007, Oct 19 - Derek, 2026-11-19). See core/triggersource.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core import discovery as disc
from core.formats import UtcTable
from core.triggersource import (SOURCE_ECHO, SOURCE_EVENTS, fallback_triggers,
                                synthetic_utc)
from tests.conftest import write_sbg_table

T0 = datetime(2026, 10, 19, 12, 57, 57, tzinfo=timezone.utc).timestamp()


class _Run:
    def __init__(self, paths, run_id="20261019.085757"):
        self.paths, self.run_id = paths, run_id

    def get(self, k):
        return self.paths.get(k)


def _utc_table(dl: Path, n_s=60, sbg0=500_000_000):
    rows = []
    for k in range(n_s):
        t = datetime.fromtimestamp(T0 + k, tz=timezone.utc)
        rows.append([str(sbg0 + k * 1_000_000), "0x0", str(t.year), str(t.month), str(t.day),
                     str(t.hour), str(t.minute), str(t.second), "0"])
    write_sbg_table(dl / "utcTime.txt",
                    ["timestamp", "status", "year", "month", "day", "hour", "minute", "second", "nanosecond"],
                    ["(us)", "(na)", "(yyyy)", "(mm)", "(dd)", "(hh)", "(mm)", "(s)", "(ns)"], rows)
    return UtcTable.parse(dl / "utcTime.txt")


def _events_file(p: Path, times):
    lines = ["Qinertia export\n",
             "GPS Time\tUTC Time\tRoll\tPitch\tYaw\tLatitude\tLongitude\tAltitude MSL\tIdentification\tNumber\n",
             "(s)\t(hh:mm:ss)\t(deg)\t(deg)\t(deg)\t(deg)\t(deg)\t(m)\t(string)\t(n)\n"]
    for n, t in enumerate(times, start=1):
        d = datetime.fromtimestamp(t, tz=timezone.utc)
        hms = f"{d.hour:02d}:{d.minute:02d}:{d.second + d.microsecond / 1e6:06.3f}"
        lines.append(f"0\t{hms}\t0\t0\t0\t34.1\t-80.9\t100\tEvent_A_{n}\t{n}\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(lines), encoding="utf-8")
    return p


def test_made_up_clock_gives_back_the_same_utc():
    clock = synthetic_utc(T0 + 3.25, T0 + 40.5)
    t = np.array([T0 + 5.123, T0 + 20.001, T0 + 39.999])
    from core.events import utc_to_sbg_us
    back = clock.sbg_to_utc(np.round(utc_to_sbg_us(clock, t)))
    assert np.allclose(back, t, atol=1e-6)


def test_echo_used_when_eventOutA_unusable(tmp_path):
    dl = tmp_path / "20261019.085757 DataLogger"
    dl.mkdir()
    utc = _utc_table(dl)
    pulses = [500_000_000 + 2_000_000 + 700_000 * k for k in range(40)]
    write_sbg_table(dl / "eventInA.txt", ["timestamp", "status"], ["(us)", "(na)"],
                    [[str(v + 120), "0x0"] for v in pulses])
    (dl / "eventOutA.txt").write_bytes(b"\x00" * 500)          # the Oct 19 shape
    run = _Run({disc.KEY_EVENT_A: dl / "eventOutA.txt", disc.KEY_UTC_TIME: dl / "utcTime.txt"})
    ft, why = fallback_triggers(run, utc)
    assert ft is not None and ft.source == SOURCE_ECHO and not ft.utc_made_up
    assert len(ft.sbg_us) == 40 and int(ft.sbg_us[0]) == pulses[0] + 120


def test_events_cut_to_utcTime_span(tmp_path):
    dl = tmp_path / "SBGData" / "261019" / "20261019.085757 DataLogger"
    dl.mkdir(parents=True)
    utc = _utc_table(dl, n_s=60)
    # session events: 10 before the run, 70 inside, 10 after
    times = [T0 - 10 + 0.7 * k for k in range(14)] + \
            [T0 + 1 + 0.75 * k for k in range(70)] + [T0 + 70 + k for k in range(10)]
    ev = _events_file(tmp_path / "SBGData" / "x_0001" / "export" / "Events-output.txt", times)
    run = _Run({disc.KEY_EVENT_A: dl / "eventOutA.txt", disc.KEY_UTC_TIME: dl / "utcTime.txt",
                disc.KEY_EVENTS_EXPORT: ev})
    ft, why = fallback_triggers(run, utc)
    assert ft is not None and ft.source == SOURCE_EVENTS and not ft.utc_made_up, why
    got = utc.sbg_to_utc(ft.sbg_us)
    want = np.array([t for t in times if T0 <= t <= T0 + 59])
    assert np.allclose(got, want, atol=0.002)        # ms-quoted UTC column


def test_no_window_no_stand_in(tmp_path):
    dl = tmp_path / "20261019.085757 DataLogger"
    dl.mkdir()
    ev = _events_file(tmp_path / "export" / "Events-output.txt", [T0 + k for k in range(5)])
    run = _Run({disc.KEY_EVENT_A: dl / "eventOutA.txt", disc.KEY_EVENTS_EXPORT: ev})
    ft, why = fallback_triggers(run, None)            # no utcTime, no camera log
    assert ft is None and "no camera log" in why
