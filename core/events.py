"""Qinertia's event export: every camera trigger of the whole session.

The SBG fires the camera every 0.750 m whether ACS is collecting or not, but
the per-run DataLogger folder only exists while ACS runs. So each run's
eventOutA.txt is missing the trigger(s) that fired before ACS started — and
that is exactly one image per run: 44 images, 43 logged triggers, every time.

Qinertia's event-mode export (Events-output.txt) has no such gap. It is one
row per trigger for the entire session, straight out of the postprocessed
solution, carrying position AND attitude at the event instant:

    GPS Time, UTC Time, Roll, Pitch, Yaw, ..., Latitude, Longitude,
    Altitude MSL, Timestamp, Identification (Event_A_<n>), Number, ...

Two things to know before trusting it:

  * The `Timestamp` column does NOT share the DataLogger's clock. The two
    zero points sit ~123 s apart on 20260824. Join on UTC, never on that
    column.
  * Rows are only meaningful where `Identification` is set; a time-mode
    export of the same template leaves that column N/A on every row, and
    such a file carries no events at all.

Measured against the app's existing route (eventOutA -> utcTime -> interpolate
the 50 Hz nav) over 530 triggers on 20260824: position agrees to 0.16 mm mean
/ 0.84 mm max, yaw to 0.0013 deg. So this file does not move the images we
could already place. What it adds is the ones we could not place at all, and
roll/pitch at the trigger, which exist nowhere else per-image.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .formats import ParseError

EVENTS_FILENAME = "Events-output.txt"
# The UTC column is quoted to the millisecond, so a matching event should land
# within half of that. 5 ms leaves room without ever reaching the 0.66 s
# trigger spacing, where a mismatch could pick the neighbouring image.
MATCH_TOL_S = 0.005


@dataclass
class EventTable:
    """One row per camera trigger, for the whole session."""

    epoch_s: np.ndarray       # UTC unix seconds
    lat_deg: np.ndarray
    lon_deg: np.ndarray
    alt_m: np.ndarray
    roll_deg: np.ndarray
    pitch_deg: np.ndarray
    yaw_deg: np.ndarray
    number: np.ndarray        # int64, Qinertia's own event counter
    path: str = ""

    def __len__(self) -> int:
        return len(self.epoch_s)

    @classmethod
    def parse(cls, path: Path, anchor_utc_date: datetime) -> "EventTable":
        """anchor_utc_date: any UTC datetime on the session's calendar day —
        the UTC column is time-of-day only, as in the nav export."""
        path = Path(path)
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                lines = fh.readlines()
        except OSError as exc:
            raise ParseError(path, f"cannot read file: {exc}")
        hdr_i = next((i for i, ln in enumerate(lines)
                      if ln.startswith("GPS Time")), None)
        if hdr_i is None:
            raise ParseError(path, "could not locate the 'GPS Time' column header line")
        names = [c.strip() for c in lines[hdr_i].split("\t")]

        def col(prefix: str) -> int:
            for j, n in enumerate(names):
                if n.startswith(prefix):
                    return j
            raise ParseError(path, f"missing column starting with '{prefix}'", hdr_i + 1)

        c_utc, c_id, c_num = col("UTC Time"), col("Identification"), col("Number")
        c_lat, c_lon, c_alt = col("Latitude"), col("Longitude"), col("Altitude")
        c_roll, c_pitch, c_yaw = col("Roll"), col("Pitch"), col("Yaw")
        base_midnight = datetime(anchor_utc_date.year, anchor_utc_date.month,
                                 anchor_utc_date.day, tzinfo=timezone.utc).timestamp()

        tod, vals, nums = [], [], []
        for i, line in enumerate(lines[hdr_i + 1:], start=hdr_i + 2):
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= max(c_id, c_num, c_yaw):
                continue
            ident = parts[c_id].strip()
            if not ident or ident in ("N/A", "(string)"):
                continue        # units row, or a time-mode export with no events
            try:
                hh, mm, ss = parts[c_utc].strip().split(":")
                tod.append(int(hh) * 3600 + int(mm) * 60 + float(ss))
                vals.append([float(parts[c]) for c in
                             (c_lat, c_lon, c_alt, c_roll, c_pitch, c_yaw)])
                nums.append(int(float(parts[c_num])))
            except ValueError as exc:
                raise ParseError(path, f"bad event row: {exc}", i)
        if not tod:
            raise ParseError(
                path, "no event rows — every 'Identification' cell is N/A. This is "
                      "a time-mode export; re-export from Qinertia with the output "
                      "triggered on the event")
        tod_arr = np.asarray(tod, dtype=np.float64)
        # same midnight handling as the nav export: time-of-day can wrap
        rollover = np.concatenate(([0.0], np.cumsum(np.diff(tod_arr) < -43200.0))) * 86400.0
        v = np.asarray(vals, dtype=np.float64)
        return cls(
            epoch_s=base_midnight + tod_arr + rollover,
            lat_deg=v[:, 0], lon_deg=v[:, 1], alt_m=v[:, 2],
            roll_deg=v[:, 3], pitch_deg=v[:, 4], yaw_deg=v[:, 5],
            number=np.asarray(nums, dtype=np.int64), path=str(path),
        )

    def nearest(self, epoch_s, tol_s: float = MATCH_TOL_S) -> np.ndarray:
        """Index of the event at each time, or -1 when none is that close."""
        e = np.atleast_1d(np.asarray(epoch_s, dtype=np.float64))
        out = np.full(len(e), -1, dtype=np.int64)
        if len(self.epoch_s) == 0:
            return out
        j = np.clip(np.searchsorted(self.epoch_s, e), 1, len(self.epoch_s) - 1)
        for cand in (j - 1, j):
            closer = np.abs(self.epoch_s[cand] - e) <= tol_s
            out = np.where(closer & (out < 0), cand, out)
        return out


def leading_trigger_utc_s(events: EventTable, trigger_utc_s: np.ndarray,
                          n_missing: int, tol_s: float = MATCH_TOL_S):
    """UTC times of the `n_missing` triggers that fired before logging began.

    Returns ``(times, note)``. `times` is empty whenever the events file
    cannot be trusted for this run, and `note` always says why — this is a
    correction to production positions, so it must never happen quietly.

    The trust test is strict on purpose: EVERY logged trigger of the run has
    to find its own event within `tol_s`, and they have to be consecutive in
    the file. That is what proves the two records are the same trigger train,
    and therefore that the rows sitting just before the run's first trigger
    really are the images ACS did not see the triggers for.
    """
    if n_missing <= 0:
        return np.empty(0), "no missing triggers"
    if len(trigger_utc_s) == 0:
        return np.empty(0), "run has no logged triggers to anchor on"
    idx = events.nearest(trigger_utc_s, tol_s)
    if np.any(idx < 0):
        n_bad = int(np.sum(idx < 0))
        return np.empty(0), (f"{n_bad} of {len(idx)} logged triggers have no event "
                             f"within {tol_s * 1000:.0f} ms — events file does not "
                             f"cover this run, triggers left as logged")
    if not np.all(np.diff(idx) == 1):
        return np.empty(0), ("logged triggers do not map to consecutive events — "
                             "events file does not line up, triggers left as logged")
    first = int(idx[0])
    if first < n_missing:
        return np.empty(0), (f"only {first} event(s) precede this run in the file, "
                             f"need {n_missing} — triggers left as logged")
    take = np.arange(first - n_missing, first)
    return events.epoch_s[take], (
        f"recovered {n_missing} pre-collection trigger(s) from {EVENTS_FILENAME} "
        f"(events {int(events.number[take[0]])}..{int(events.number[take[-1]])})")


def utc_to_sbg_us(utc, epoch_s) -> np.ndarray:
    """Invert UtcTable's clock map, so recovered triggers can join the SBG
    timeline the rest of the pipeline speaks in."""
    e = np.asarray(epoch_s, dtype=np.float64)
    t0, t1 = float(utc.sbg_us[0]), float(utc.sbg_us[-1])
    e0, e1 = float(utc.epoch_s[0]), float(utc.epoch_s[-1])
    rate = (t1 - t0) / (e1 - e0) if e1 > e0 else 1e6      # us per second
    inside = (e >= e0) & (e <= e1)
    interp = np.interp(e, utc.epoch_s, utc.sbg_us.astype(np.float64))
    return np.where(inside, interp, t0 + (e - e0) * rate)


def find_events_file(root: Path) -> Path | None:
    """Beside the nav export: SBGData/**/export/Events-output.txt."""
    sbg = Path(root) / "SBGData"
    if not sbg.is_dir():
        return None
    hits = sorted(sbg.rglob(f"export/{EVENTS_FILENAME}"))
    return hits[0] if hits else None
