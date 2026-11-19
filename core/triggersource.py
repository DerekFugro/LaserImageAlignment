"""Camera triggers when the run's DataLogger can't give them (Derek, 2026-11-19).

Some DataLogger files arrive empty or filled with zeros (Batch007, Oct 19:
eventOutA.txt on 084148, 125757, 125950, 130300; utcTime.txt too on 153941,
161211, 161636, 172157, 172543) while the photos and the GPS are fine. Until
now such a run stopped with "could not verify coverage: file too short".

The same pulses are in two other places:

1. eventInA.txt - the SBG's echo of each pulse, same row, a steady 0.1-0.7 ms
   after eventOutA (97 loggers, 2026-11-17). Used when eventOutA is unusable
   and utcTime.txt is good.
2. Qinertia's Events-output.txt - one Event_A row per pulse for the whole
   session, with its UTC time. Within 1 ms of eventOutA on 592 pulses
   (orchestrator, 2026-11-19); 0.16 mm / 0.84 mm apart in position on 530
   (core/events.py). It covers the WHOLE session, so it is cut to this run:
     - by utcTime.txt's span when that file is good (it runs exactly while
       the DataLogger does), or
     - by the run's start (its name) and the cameras' stop in the camera log,
       tied to GPS time through the SBG.log lines, when utcTime.txt is
       unusable too. NOT by the lasers' time span (see run_window_utc).
   The camera log then decides which event took which photo, exactly as for
   a normal run (core/cameralog.py) - extra events outside the photos are
   never paired.

Good data is not flagged (Derek, 2026-11-19): the source is said in an INFO
row only; the pairing is PASS or WARN on its own merits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import discovery as disc
from .formats import ECHO_FILENAME, ParseError, UtcTable, parse_event_triggers

SOURCE_ECHO = "eventInA.txt (the SBG's echo of each pulse)"
SOURCE_EVENTS = "Qinertia Events-output.txt"
# rows either side of the cut in a made-up clock table, so nothing that reads
# it sits on its edge
_SYNTH_PAD_S = 30


@dataclass
class FallbackTriggers:
    sbg_us: np.ndarray            # trigger times on the (real or made-up) SBG clock
    utc: UtcTable                 # the clock table they are on
    utc_made_up: bool             # True: utcTime.txt was unusable, see synthetic_utc
    source: str
    note: str
    dropped: list = field(default_factory=list)
    repaired: list = field(default_factory=list)


def synthetic_utc(lo_s: float, hi_s: float) -> UtcTable:
    """A clock table for a run whose utcTime.txt is unusable: 1 row a second,
    'SBG microseconds' simply counted from the first row. Everything that
    joins SBG time to UTC goes through this table, so trigger times put on
    it come back out as the same UTC."""
    e = np.arange(np.floor(lo_s) - _SYNTH_PAD_S, np.ceil(hi_s) + _SYNTH_PAD_S + 1, 1.0)
    sbg = np.round((e - e[0]) * 1e6).astype(np.int64) + 1_000_000
    return UtcTable(sbg_us=sbg, epoch_s=e, path="(made from the trigger times - "
                    "utcTime.txt unusable)", n_wraps=0)


def _datalogger_dir(run) -> Path | None:
    for key in (disc.KEY_EVENT_A, disc.KEY_UTC_TIME):
        p = run.get(key)
        if p is not None:
            return Path(p).parent
    return None


def run_window_utc(run, utc: UtcTable | None):
    """(first, last UTC second, how) of this run, or (None, None, why not)."""
    if utc is not None and len(utc.epoch_s) >= 2:
        return float(utc.epoch_s[0]), float(utc.epoch_s[-1]), "the run's utcTime.txt span"
    # NOT the lasers' time span: tested 2026-11-19 on 34 runs, the lasers
    # start up to 10 s after the DataLogger, so the first pulses were cut
    # off and on 20261017.154208 every photo was paired one pulse late while
    # the camera start still read PASS. The run stamp + camera stop matched
    # the DataLogger's pulses on 33 of 34 runs; the 34th (20261101.093228,
    # the day the clocks went back) found no events and was refused.
    from . import cameralog
    lp = run.get(disc.KEY_CAMERA_LOG)
    if lp is None:
        return None, None, "utcTime.txt unusable and no camera log to find the run's span"
    try:
        log = cameralog.load(Path(lp))
    except OSError as exc:
        return None, None, f"utcTime.txt unusable and the camera log can't be read ({exc})"
    start = cameralog.start_for_run(log, run.run_id)
    if start is None:
        return None, None, "utcTime.txt unusable and the camera log has no start for this run"
    stops = log.stops_local[log.stops_local > start] if len(log.stops_local) else []
    if not len(stops):
        return None, None, "utcTime.txt unusable and the camera log has no stop for this run"
    # The DataLogger starts with the run stamp, the cameras 1-2 s later; start
    # at whichever is earlier, so no pulse the DataLogger would have had is lost
    stamp = cameralog.run_start_local(run.run_id)
    first = min(float(start), stamp) if stamp is not None else float(start)
    lo, hi = log.local_to_utc(first), log.local_to_utc(float(stops[0]))
    if lo is None or hi is None or hi <= lo:
        return None, None, ("utcTime.txt unusable and the camera log's PC clock can't be "
                            "tied to GPS time (no SBG log lines near it)")
    return lo, hi, "the run's start and the cameras' stop (camera log)"


def fallback_triggers(run, utc: UtcTable | None):
    """(FallbackTriggers, "") or (None, why none could be made)."""
    from .events import EventTable, utc_to_sbg_us
    why = []
    dl = _datalogger_dir(run)
    echo = dl / ECHO_FILENAME if dl is not None else None
    if utc is not None and echo is not None and echo.is_file():
        try:
            ts, _, dropped, repaired = parse_event_triggers(
                echo, drop_backwards=True, with_repaired=True)
            if len(ts):
                ts = np.round(utc.align_wraps(ts)).astype(np.int64)
                return FallbackTriggers(
                    ts, utc, False, SOURCE_ECHO,
                    f"{len(ts)} triggers from {SOURCE_ECHO} - the same pulses, "
                    "0.1-0.7 ms later than eventOutA", dropped, repaired), ""
        except ParseError as exc:
            why.append(f"eventInA.txt: {exc}")
    ep = run.get(disc.KEY_EVENTS_EXPORT)
    if ep is None:
        why.append("no Qinertia Events-output.txt for this session")
        return None, "; ".join(why)
    lo, hi, how = run_window_utc(run, utc)
    if lo is None:
        why.append(how)
        return None, "; ".join(why)
    try:
        ev = EventTable.parse(Path(ep), datetime.fromtimestamp((lo + hi) / 2, tz=timezone.utc))
    except ParseError as exc:
        why.append(f"Events-output.txt: {exc}")
        return None, "; ".join(why)
    sel = np.where((ev.epoch_s >= lo) & (ev.epoch_s <= hi))[0]
    if len(sel) < 1:
        why.append(f"Events-output.txt has no event inside {how}"
                   + (" - the PC clock may be off (it was an hour off on 20261101, "
                      "the day the clocks went back)" if "camera log" in how else ""))
        return None, "; ".join(why)
    times = ev.epoch_s[sel]
    keep = np.concatenate(([True], np.diff(times) > 0))   # never two at one time
    times, sel = times[keep], sel[keep]
    made_up = utc is None
    clock = synthetic_utc(lo, hi) if made_up else utc
    sbg = np.round(utc_to_sbg_us(clock, times)).astype(np.int64)
    note = (f"{len(times)} triggers from {SOURCE_EVENTS} (events "
            f"{int(ev.number[sel[0]])}..{int(ev.number[sel[-1]])}), cut to {how}"
            + ("; their UTC times are used directly, as utcTime.txt is unusable"
               if made_up else ""))
    return FallbackTriggers(sbg, clock, made_up, SOURCE_EVENTS, note), ""
