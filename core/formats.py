"""Parsers for every input format.

All formats are specified in Spec/LaserImageAlignment_Instructions.md
("Data formats" section) and were verified against real collected data.
Every parser raises ParseError with file + line context on malformed input.
All times are handled as Unix-epoch UTC seconds (float) unless suffixed _us.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

JPEG_SOI = b"\xff\xd8"
IMAGE_STEP_MM = 750
TAI_UTC_NOMINAL_S = 37.0  # nominal TAI-UTC offset; solved per run, never trusted blindly

# The Qinertia export's final rows are written as the solution shuts down and
# are not survey data — on 20260824 the last one is (0,0) with a NaN altitude,
# 8,095 km from site. They fall after the last run ends, so nothing is ever
# placed from them and there is nothing to report: they are simply not read.
TRAILING_ROWS_IGNORED = 2

# Images whose footprint falls before the section start are filed here by
# core.rename after a batch. They are still the run's images — the folder is
# housekeeping, not a change of ownership — so anything counting a run has to
# count them (see ImageSet.n_set_aside).
BEFORE_DIR = "BeforeCollection"

# ONE physical wheel encoder feeds BOTH the SBG (Kalman aiding, and firing the
# 0.75 m camera triggers logged in eventOutA) and the Gocator (which triggers
# itself on the same encoder and stamps every profile with the count).
# Confirmed with Derek 2026-08-20; ~3170 counts per camera trigger, stable to
# ~1% across runs with no drift.
#
# The scale is per VEHICLE and is read from ACS's own Settings file for the
# day - see core.acs_settings. Until 2026-10-08 the old cart's 0.235116
# mm/tick was built in here, which was wrong for every other van.
#
# NOTE: the raw counts are never logged by the SBG - DmiStationEx carries the
# SBG's *estimated* distance, and post-processing outputs only velocity. The
# Gocator's `encoder` column is the ONLY physical-wheel measurement recorded
# anywhere in a collection.


class ParseError(Exception):
    """Malformed input file. Message always includes file (and line when known)."""

    def __init__(self, path: Path | str, message: str, line_no: int | None = None):
        self.path = str(path)
        self.line_no = line_no
        where = f"{self.path}:{line_no}" if line_no is not None else self.path
        super().__init__(f"{where}: {message}")


# ---------------------------------------------------------------------------
# SBG DataLogger text files (tab separated, 2 header lines: names, units)
# ---------------------------------------------------------------------------

def _read_sbg_table(path: Path, expected_first_col: str = "timestamp") -> tuple[list[str], np.ndarray]:
    """Read an SBG DataLogger .txt table. Returns (column_names, float ndarray).

    Non-numeric cells raise ParseError. Hex status fields are converted via int(x, 16).
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except OSError as exc:
        raise ParseError(path, f"cannot read file: {exc}")
    lines = raw.splitlines()
    if len(lines) < 3:
        raise ParseError(path, "file too short: expected 2 header lines + data")
    names = lines[0].split("\t")
    units = lines[1].split("\t")
    if not names or names[0].strip() != expected_first_col:
        raise ParseError(path, f"expected first column '{expected_first_col}', got '{names[0] if names else ''}'", 1)
    if len(units) != len(names):
        raise ParseError(path, f"units line has {len(units)} fields, names line has {len(names)}", 2)
    n_cols = len(names)
    rows: list[list[float]] = []
    for i, line in enumerate(lines[2:], start=3):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != n_cols:
            raise ParseError(path, f"expected {n_cols} fields, got {len(parts)}", i)
        row = []
        for j, cell in enumerate(parts):
            cell = cell.strip()
            try:
                row.append(float(int(cell, 16)) if cell.startswith("0x") else float(cell))
            except ValueError:
                raise ParseError(path, f"non-numeric value '{cell}' in column '{names[j]}'", i)
        rows.append(row)
    if not rows:
        raise ParseError(path, "no data rows")
    return [n.strip() for n in names], np.asarray(rows, dtype=np.float64)


# The SBG's internal clock (the first column of every DataLogger .txt) is a
# 32-bit microsecond counter from power-on: it rolls over to 0 every 2**32 us
# (71.58 min). A run that spans that moment logs a ~4295 s backward jump. That
# is normal, not a fault - it is undone here so the clock keeps counting up.
SBG_CLOCK_WRAP_US = 2 ** 32


def unwrap_sbg_us(ts) -> tuple[np.ndarray, int]:
    """Undo the SBG clock roll-over in file order. Returns (int64 ts, n_wraps).

    Only a drop of more than half the wrap counts as a roll-over; any smaller
    backward step is left alone so the callers still reject it as bad data.
    """
    ts = np.asarray(ts, dtype=np.int64)
    if len(ts) < 2:
        return ts, 0
    wraps = np.concatenate(([0], np.cumsum(np.diff(ts) < -SBG_CLOCK_WRAP_US // 2)))
    return ts + wraps.astype(np.int64) * SBG_CLOCK_WRAP_US, int(wraps[-1])


def parse_event_triggers(path: Path, with_wraps: bool = False):
    """eventOutA.txt -> strictly increasing SBG timestamps in microseconds (int64).

    The clock roll-over is undone (see SBG_CLOCK_WRAP_US). with_wraps=True
    returns (ts, n_wraps) so a report can say a roll-over was corrected.
    """
    names, data = _read_sbg_table(path)
    ts, n_wraps = unwrap_sbg_us(data[:, 0].astype(np.int64))
    if len(ts) >= 2 and not np.all(np.diff(ts) > 0):
        bad = int(np.argmin(np.diff(ts) > 0)) + 1
        raise ParseError(path, f"trigger timestamps not strictly increasing near data row {bad}")
    return (ts, n_wraps) if with_wraps else ts


@dataclass
class UtcTable:
    """SBG internal clock (us) <-> UTC epoch seconds, from utcTime.txt."""

    sbg_us: np.ndarray  # int64, strictly increasing (clock roll-over undone)
    epoch_s: np.ndarray  # float64 UTC unix seconds
    path: str = ""
    n_wraps: int = 0     # SBG clock roll-overs undone while reading the file

    @classmethod
    def parse(cls, path: Path) -> "UtcTable":
        names, data = _read_sbg_table(path)
        need = ["timestamp", "year", "month", "day", "hour", "minute", "second", "nanosecond"]
        idx = {}
        for col in need:
            if col not in names:
                raise ParseError(path, f"missing column '{col}'")
            idx[col] = names.index(col)
        # unwrap in FILE order, before the sort below - sorting first would
        # silently pair post-roll-over clock values with the wrong UTC rows
        sbg, n_wraps = unwrap_sbg_us(data[:, idx["timestamp"]].astype(np.int64))
        epochs = np.empty(len(data), dtype=np.float64)
        for i, row in enumerate(data):
            try:
                dt = datetime(
                    int(row[idx["year"]]), int(row[idx["month"]]), int(row[idx["day"]]),
                    int(row[idx["hour"]]), int(row[idx["minute"]]), int(row[idx["second"]]),
                    tzinfo=timezone.utc,
                )
            except ValueError as exc:
                raise ParseError(path, f"invalid date/time: {exc}", i + 3)
            epochs[i] = dt.timestamp() + row[idx["nanosecond"]] * 1e-9
        order = np.argsort(sbg)
        sbg, epochs = sbg[order], epochs[order]
        if len(sbg) >= 2 and not np.all(np.diff(sbg) > 0):
            raise ParseError(path, "duplicate SBG timestamps in utcTime table")
        return cls(sbg_us=sbg, epoch_s=epochs, path=str(path), n_wraps=n_wraps)

    def sbg_to_utc(self, sbg_us) -> np.ndarray:
        """Linear interpolation; extrapolates linearly at the edges (SBG clock is stable).

        Each file is unwrapped from its own first row, so a trigger file that
        starts after a roll-over the utcTime file started before sits whole
        wraps low. It is moved by the whole number of wraps that brings it to
        the table - never by anything else.
        """
        sbg_us = self.align_wraps(sbg_us)
        t0, t1 = float(self.sbg_us[0]), float(self.sbg_us[-1])
        e0, e1 = float(self.epoch_s[0]), float(self.epoch_s[-1])
        out = np.interp(sbg_us, self.sbg_us.astype(np.float64), self.epoch_s)
        rate = (e1 - e0) / (t1 - t0) if t1 > t0 else 1e-6
        below, above = sbg_us < t0, sbg_us > t1
        out = np.where(below, e0 + (sbg_us - t0) * rate, out)
        out = np.where(above, e1 + (sbg_us - t1) * rate, out)
        return out

    def align_wraps(self, sbg_us) -> np.ndarray:
        """Shift clock values by whole SBG roll-overs onto this table's span."""
        x = np.asarray(sbg_us, dtype=np.float64)
        if x.size == 0:
            return x
        mid_table = 0.5 * (float(self.sbg_us[0]) + float(self.sbg_us[-1]))
        k = np.round((mid_table - float(np.median(x))) / SBG_CLOCK_WRAP_US)
        return x + k * SBG_CLOCK_WRAP_US

    @property
    def max_gap_s(self) -> float:
        return float(np.max(np.diff(self.epoch_s))) if len(self.epoch_s) > 1 else 0.0

    @property
    def utc_date(self) -> datetime:
        """Median date of the table (UTC) — used to anchor time-of-day sources."""
        mid = float(np.median(self.epoch_s))
        return datetime.fromtimestamp(mid, tz=timezone.utc)


@dataclass
class DmiTable:
    """Travelled distance & speed vs UTC epoch time, from 'DmiStationEx *.csv'.

    NOTE: the file's TimeOfWeek column is unreliable (verified) — never used.
    """

    epoch_s: np.ndarray
    dist_m: np.ndarray
    speed_ms: np.ndarray
    path: str = ""

    @classmethod
    def parse(cls, path: Path) -> "DmiTable":
        path = Path(path)
        t, d, v = [], [], []
        try:
            fh = open(path, encoding="utf-8", newline="")
        except OSError as exc:
            raise ParseError(path, f"cannot read file: {exc}")
        with fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise ParseError(path, "empty file")
            cols = [c.strip() for c in header]
            if len(cols) < 3 or not cols[0].startswith("Dmi") or not cols[2].startswith("UTC TS"):
                raise ParseError(path, f"unexpected header {cols!r}: expected 'Dmi (m), Speed (m/s), UTC TS (s), ...'", 1)
            for i, row in enumerate(reader, start=2):
                if not row or not "".join(row).strip():
                    continue
                if len(row) < 3:
                    raise ParseError(path, f"expected >=3 fields, got {len(row)}", i)
                try:
                    d.append(float(row[0]))
                    v.append(float(row[1]))
                    t.append(float(row[2]))
                except ValueError as exc:
                    raise ParseError(path, f"non-numeric value: {exc}", i)
        if not t:
            raise ParseError(path, "no data rows")
        t_arr, d_arr, v_arr = (np.asarray(x, dtype=np.float64) for x in (t, d, v))
        order = np.argsort(t_arr)
        t_arr, d_arr, v_arr = t_arr[order], d_arr[order], v_arr[order]
        keep = np.concatenate(([True], np.diff(t_arr) > 0))  # drop duplicate timestamps
        t_arr, d_arr, v_arr = t_arr[keep], d_arr[keep], v_arr[keep]
        # mm-level backward jitter at standstill is normal (cart rocking);
        # verified on real data: run 20260816.110840 has 54 steps of <= 2 mm.
        # Only a genuine reversal (> 5 cm in one step) is treated as corrupt.
        steps = np.diff(d_arr)
        if np.any(steps < -0.05):
            bad = int(np.argmax(steps < -0.05)) + 1
            raise ParseError(path, f"DMI distance reverses by {-steps.min():.3f} m near data row {bad}")
        return cls(epoch_s=t_arr, dist_m=d_arr, speed_ms=v_arr, path=str(path))

    def dist_at(self, epoch_s) -> np.ndarray:
        return np.interp(np.asarray(epoch_s, dtype=np.float64), self.epoch_s, self.dist_m)

    def speed_at(self, epoch_s) -> np.ndarray:
        return np.interp(np.asarray(epoch_s, dtype=np.float64), self.epoch_s, self.speed_ms)

    @property
    def total_dist_m(self) -> float:
        return float(self.dist_m[-1])


# ---------------------------------------------------------------------------
# Qinertia post-processed ASCII export (ascii-output.txt)
# ---------------------------------------------------------------------------

EARTH_M_PER_DEG_LAT = 111320.0


@dataclass
class NavTable:
    """Post-processed navigation: UTC epoch -> lat/lon/alt (and yaw), ~50 Hz.

    THE RULER: this table is the app's only source of position AND of
    along-track distance. The raw DMI wheel is never used for placement —
    it is uncorrected realtime data; the export is the corrected product.

    Location lookups NEVER extrapolate: out-of-window queries return NaN.
    """

    epoch_s: np.ndarray
    lat_deg: np.ndarray
    lon_deg: np.ndarray
    alt_m: np.ndarray
    yaw_deg: np.ndarray
    gps_tow_s: np.ndarray
    path: str = ""
    vel_h_ms: np.ndarray | None = None   # horizontal speed from N/E velocity columns
    n_dropped_nofix: int = 0             # (0,0) placeholder rows removed at parse
    # along-track ruler, filled in __post_init__
    along_m: np.ndarray | None = None
    speed_ms: np.ndarray | None = None

    def __post_init__(self):
        # cumulative horizontal distance along the trajectory (equirectangular
        # locally — exact to sub-mm at run scale)
        lat0 = float(np.median(self.lat_deg))
        m_per_deg_lon = EARTH_M_PER_DEG_LAT * np.cos(np.radians(lat0))
        dx = np.diff(self.lon_deg) * m_per_deg_lon
        dy = np.diff(self.lat_deg) * EARTH_M_PER_DEG_LAT
        step = np.hypot(dx, dy)
        self.along_m = np.concatenate(([0.0], np.cumsum(step)))
        # speed: prefer the export's own velocity columns (clean, Qinertia-
        # smoothed); fall back to position gradient when absent
        if self.vel_h_ms is not None and np.any(np.isfinite(self.vel_h_ms)):
            self.speed_ms = np.where(np.isfinite(self.vel_h_ms), self.vel_h_ms, 0.0)
        else:
            dt = np.gradient(self.epoch_s)
            self.speed_ms = np.gradient(self.along_m) / np.where(dt > 0, dt, 1.0)

    # --- ruler interface (same shape as the old DMI ruler) ---
    def dist_at(self, epoch_s) -> np.ndarray:
        return np.interp(np.asarray(epoch_s, dtype=np.float64), self.epoch_s, self.along_m)

    def speed_at(self, epoch_s) -> np.ndarray:
        return np.interp(np.asarray(epoch_s, dtype=np.float64), self.epoch_s, self.speed_ms)

    def heading_at(self, epoch_s) -> np.ndarray:
        """True heading in degrees (0-360) at the given UTC times.

        Yaw degrees must NOT be interpolated directly: across the wrap, 359 deg
        and 1 deg average to 180 deg — a heading pointing the opposite way. So
        interpolate the unit vector and take atan2. NaN in, NaN out.
        """
        e = np.asarray(epoch_s, dtype=np.float64)
        yaw = np.radians(self.yaw_deg)
        sin_i = np.interp(e, self.epoch_s, np.sin(yaw))
        cos_i = np.interp(e, self.epoch_s, np.cos(yaw))
        return np.degrees(np.arctan2(sin_i, cos_i)) % 360.0

    @property
    def total_dist_m(self) -> float:
        return float(self.along_m[-1])

    @classmethod
    def parse(cls, path: Path, anchor_utc_date: datetime) -> "NavTable":
        """anchor_utc_date: any UTC datetime on the run's UTC calendar day
        (take it from the run's UtcTable.utc_date). The export's UTC column is
        time-of-day only; midnight rollovers inside the file are handled."""
        path = Path(path)
        try:
            fh = open(path, encoding="utf-8", errors="strict")
        except OSError as exc:
            raise ParseError(path, f"cannot read file: {exc}")
        with fh:
            lines = fh.readlines()
        # locate the tab-separated header line starting with 'GPS Time'
        hdr_i = None
        for i, line in enumerate(lines):
            if line.startswith("GPS Time"):
                hdr_i = i
                break
        if hdr_i is None or hdr_i + 2 >= len(lines):
            raise ParseError(path, "could not locate the 'GPS Time' column header line")
        names = [c.strip() for c in lines[hdr_i].split("\t")]

        def col(prefix: str) -> int:
            for j, n in enumerate(names):
                if n.startswith(prefix):
                    return j
            raise ParseError(path, f"missing column starting with '{prefix}'", hdr_i + 1)

        c_gps, c_utc = col("GPS Time"), col("UTC Time")
        c_lat, c_lon, c_alt, c_yaw = col("Latitude"), col("Longitude"), col("Altitude"), col("Yaw")
        try:
            c_vn, c_ve = col("North Velocity"), col("East Velocity")
        except ParseError:
            c_vn = c_ve = None
        base_midnight = datetime(
            anchor_utc_date.year, anchor_utc_date.month, anchor_utc_date.day, tzinfo=timezone.utc
        ).timestamp()

        def numval(cell: str) -> float:
            cell = cell.strip()
            return float("nan") if cell == "N/A" else float(cell)

        gps, tod, lat, lon, alt, yaw, velh = [], [], [], [], [], [], []
        for i, line in enumerate(lines[hdr_i + 2:], start=hdr_i + 3):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != len(names):
                raise ParseError(path, f"expected {len(names)} fields, got {len(parts)}", i)
            try:
                m = re.match(r"^(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)$", parts[c_utc].strip())
                if not m:
                    raise ValueError(f"bad UTC Time '{parts[c_utc].strip()}'")
                tod.append(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
                gps.append(float(parts[c_gps]))
                lat.append(float(parts[c_lat]))
                lon.append(float(parts[c_lon]))
                alt.append(float(parts[c_alt]))
                yaw.append(numval(parts[c_yaw]))
                if c_vn is not None:
                    velh.append(float(np.hypot(numval(parts[c_vn]), numval(parts[c_ve]))))
                else:
                    velh.append(float("nan"))
            except ValueError as exc:
                raise ParseError(path, str(exc), i)
        if not gps:
            raise ParseError(path, "no data rows")
        tod_arr = np.asarray(tod, dtype=np.float64)
        rollover = np.concatenate(([0.0], np.cumsum(np.diff(tod_arr) < -43200.0))) * 86400.0
        epoch = base_midnight + tod_arr + rollover

        # Drop no-fix / unconverged position rows — real exports carry a few at
        # session start/end ((0,0) placeholders and half-converged garbage like
        # (2.13, -3.94)); a single one poisons the along-track cumsum by
        # thousands of km (verified on the 20260817 session: 76 + 1 such rows).
        # Rule: keep only rows within ~1 degree (~110 km) of the session's
        # median position (median is robust to the outliers themselves).
        arr = {k: np.asarray(v) for k, v in
               dict(lat=lat, lon=lon, alt=alt, yaw=yaw, gps=gps, velh=velh).items()}
        n_all = len(arr["lat"])
        keep = np.ones(n_all, dtype=bool)
        # never trim a file so small that the trim is most of it — a couple
        # of rows is a test fixture or a broken export, not a survey
        n_trailing = TRAILING_ROWS_IGNORED if n_all > TRAILING_ROWS_IGNORED + 2 else 0
        if n_trailing:
            keep[-n_trailing:] = False
        lat_med, lon_med = float(np.median(arr["lat"][keep])), \
            float(np.median(arr["lon"][keep]))
        on_site = (np.abs(arr["lat"] - lat_med) < 1.0) & (np.abs(arr["lon"] - lon_med) < 1.0)
        good = keep & on_site
        n_dropped = int(np.sum(keep & ~on_site))   # no-fix rows we did read
        if not np.any(good):
            raise ParseError(path, "all rows are (0,0) no-fix placeholders")
        table = cls(
            epoch_s=epoch[good],
            lat_deg=arr["lat"][good], lon_deg=arr["lon"][good], alt_m=arr["alt"][good],
            yaw_deg=arr["yaw"][good], gps_tow_s=arr["gps"][good], path=str(path),
            vel_h_ms=arr["velh"][good], n_dropped_nofix=n_dropped,
        )
        if not np.all(np.diff(table.epoch_s) > 0):
            raise ParseError(path, "export UTC times are not strictly increasing after rollover handling")
        return table

    def covers(self, epoch_s) -> np.ndarray:
        e = np.asarray(epoch_s, dtype=np.float64)
        return (e >= self.epoch_s[0]) & (e <= self.epoch_s[-1])

    def locate(self, epoch_s) -> dict[str, np.ndarray]:
        """Interpolated lat/lon/alt at UTC epoch times. NaN outside the window."""
        e = np.asarray(epoch_s, dtype=np.float64)
        inside = self.covers(e)
        out = {
            "lat_deg": np.interp(e, self.epoch_s, self.lat_deg),
            "lon_deg": np.interp(e, self.epoch_s, self.lon_deg),
            "alt_m": np.interp(e, self.epoch_s, self.alt_m),
        }
        for k in out:
            out[k] = np.where(inside, out[k], np.nan)
        return out

    @property
    def max_gap_s(self) -> float:
        return float(np.max(np.diff(self.epoch_s))) if len(self.epoch_s) > 1 else 0.0

    @property
    def gps_minus_utc_s(self) -> float:
        """Median (GPS TOW - UTC time-of-week) — should equal the GPS-UTC leap offset (18 s)."""
        # compare modulo one day: GPS TOW mod 86400 vs UTC time-of-day
        diff = np.mod(self.gps_tow_s, 86400.0) - np.mod(self.epoch_s, 86400.0)
        diff = np.mod(diff + 43200.0, 86400.0) - 43200.0
        return float(np.median(diff))


# ---------------------------------------------------------------------------
# Gocator profile CSV
# ---------------------------------------------------------------------------

GOCATOR_META_COLS = 8
_GOCATOR_HEADER_PREFIX = (
    "frameIndex,timestamp,ptpTimestamp,encoder,numberOfProfilePoints,numberOfValidPoints,bridgedValue,status"
)


@dataclass
class GocatorIndex:
    """Streaming index of a Gocator CSV: per-row metadata + byte offsets.

    Profile point data is NOT held in memory; use read_profile(row) on demand.
    ptp_us is on the PTP/TAI epoch — see core.alignment for the UTC conversion.
    """

    path: str
    frame: np.ndarray          # int64
    ptp_us: np.ndarray         # int64 (TAI epoch, microseconds)
    encoder: np.ndarray        # int64
    n_points: np.ndarray       # int32
    n_valid: np.ndarray        # int32
    byte_offset: np.ndarray    # int64 offset of each data row in the file
    meta_cols: int = GOCATOR_META_COLS  # columns before x0 (grows once GPS is injected)
    # set when PTP clock steps were corrected on load - see _align_ptp_to_sensor
    # and _repair_ptp_step
    ptp_step: dict | None = None
    # set when PTP steps were SEEN but could not be corrected safely (the
    # file still reads; QC warns) - see _align_ptp_to_sensor
    ptp_suspect: dict | None = None
    # the sensor's own free-running clock (the `timestamp` column). Never
    # used to place anything by itself; it only shows where the PTP time
    # stepped (see _align_ptp_to_sensor). None when not usable.
    sensor_ts: np.ndarray | None = None

    @classmethod
    def build(cls, path: Path) -> "GocatorIndex":
        path = Path(path)
        frames, ptps, encs, npts, nval, offs = [], [], [], [], [], []
        sts: list | None = []
        try:
            fh = open(path, "rb")
        except OSError as exc:
            raise ParseError(path, f"cannot read file: {exc}")
        with fh:
            header = fh.readline().decode("utf-8", errors="replace").strip()
            if not header.startswith(_GOCATOR_HEADER_PREFIX):
                raise ParseError(path, f"unexpected Gocator header: {header[:120]!r}", 1)
            # point data starts at the x0 column; the metadata block widens
            # when this app injects latitude/longitude/elevation.
            header_cols = header.split(",")
            try:
                meta_cols = header_cols.index("x0")
            except ValueError:
                raise ParseError(path, "Gocator header has no 'x0' column", 1)
            line_no = 1
            while True:
                off = fh.tell()
                line = fh.readline()
                if not line:
                    break
                line_no += 1
                if not line.strip():
                    continue
                # metadata = first 8 comma-separated fields; avoid splitting the whole row
                parts = line.split(b",", GOCATOR_META_COLS)
                if len(parts) < GOCATOR_META_COLS + 1:
                    raise ParseError(path, f"row has fewer than {GOCATOR_META_COLS} metadata columns", line_no)
                try:
                    frames.append(int(parts[0]))
                    ptps.append(int(parts[2]))
                    encs.append(int(parts[3]))
                    npts.append(int(parts[4]))
                    nval.append(int(parts[5]))
                except ValueError as exc:
                    raise ParseError(path, f"non-numeric metadata: {exc}", line_no)
                if sts is not None:
                    try:
                        sts.append(int(parts[1]))
                    except ValueError:
                        sts = None          # not usable; nothing depends on it
                offs.append(off)
        if not frames:
            raise ParseError(path, "no profile rows")
        idx = cls(
            path=str(path),
            frame=np.asarray(frames, dtype=np.int64),
            ptp_us=np.asarray(ptps, dtype=np.int64),
            encoder=np.asarray(encs, dtype=np.int64),
            n_points=np.asarray(npts, dtype=np.int32),
            n_valid=np.asarray(nval, dtype=np.int32),
            byte_offset=np.asarray(offs, dtype=np.int64),
            meta_cols=meta_cols,
            sensor_ts=(np.asarray(sts, dtype=np.int64)
                       if sts is not None and len(sts) == len(frames) else None),
        )
        # Every file is checked against its own sensor clock; a file with no
        # PTP step comes out exactly as read.
        sensor_status = _align_ptp_to_sensor(idx)
        if len(idx.ptp_us) >= 2 and not np.all(np.diff(idx.ptp_us) > 0):
            # Whenever the sensor-clock repair did not fix it, the older
            # one-step repair runs exactly as before, so no file is ever
            # refused that loaded before this change (a 'suspect' file
            # still carries its warning).
            if sensor_status != "corrected" and idx.ptp_step is None:
                idx.ptp_step = _repair_ptp_step(idx)
            if idx.ptp_step is None or not np.all(np.diff(idx.ptp_us) > 0):
                d = np.diff(idx.ptp_us)
                back = np.where(d <= 0)[0]
                raise ParseError(
                    path,
                    f"ptpTimestamp not strictly increasing: {len(back)} backward "
                    f"step(s), first at frame {int(idx.frame[back[0] + 1])} "
                    f"({int(d[back[0]]) / 1000:+.1f} ms) - the PTP time could not "
                    "be put back on one clock")
        return idx

    def __len__(self) -> int:
        return len(self.frame)

    def read_profile(self, row: int) -> np.ndarray:
        """Return the (N, 2) array of valid (x_mm, z_mm) points for index row `row`."""
        if not 0 <= row < len(self):
            raise IndexError(f"profile row {row} out of range 0..{len(self) - 1}")
        with open(self.path, "rb") as fh:
            fh.seek(int(self.byte_offset[row]))
            line = fh.readline().decode("utf-8", errors="replace")
        cells = line.rstrip("\n\r").split(",")[self.meta_cols:]
        pts = []
        for i in range(0, len(cells) - 1, 2):
            a, b = cells[i].strip(), cells[i + 1].strip()
            if a and b:
                try:
                    pts.append((float(a), float(b)))
                except ValueError:
                    raise ParseError(self.path, f"non-numeric profile point at pair {i // 2}")
        return np.asarray(pts, dtype=np.float64).reshape(-1, 2)


# One PTP clock step is corrected, anything else still refuses the file.
#
# 20261007.120330 left laser: profiles every 9.2 ms, and at frame 150 (1.4%
# into the run) ptpTimestamp jumps back 13.85 ms - the profile is stamped
# 4.65 ms BEFORE the one ahead of it - then runs perfectly for the other
# 10,664 profiles. frameIndex keeps counting and the encoder keeps its steady
# 145 counts per frame straight through, so the laser never stopped; only its
# clock moved: a one-off PTP correction as it finished locking to the SBG.
# Refusing the file cost the whole laser (10,813 profiles, no GPS) over 14 mm.
# Derek, 2026-10-08: correct the step.
#
# Only when it is unmistakably that: exactly one backwards step, frames and
# encoder continuous across it, the step no bigger than PTP_STEP_MAX_US, and
# the file strictly increasing once corrected. The SHORTER side is moved onto
# the longer side's clock, so the bulk of the run keeps its own timestamps.
PTP_STEP_MAX_US = 50_000


def _repair_ptp_step(idx: "GocatorIndex") -> dict | None:
    ptp = idx.ptp_us
    d = np.diff(ptp)
    bad = np.where(d <= 0)[0]
    if len(bad) != 1 or len(d) < 10:
        return None
    i = int(bad[0]) + 1                       # first row on the new clock
    # the spacing either side of the step, not the whole file's: the scan
    # interval follows speed, so a run-wide median misjudges the jump
    near = d[max(i - 11, 0):i + 10]
    near = near[near > 0]
    expected = int(np.median(near if len(near) else d[d > 0]))
    jump = int(ptp[i] - ptp[i - 1]) - expected   # negative: the clock went back
    if abs(jump) > PTP_STEP_MAX_US:
        return None
    if int(idx.frame[i] - idx.frame[i - 1]) != 1:
        return None
    de = np.diff(idx.encoder)
    notch = float(np.median(de[de > 0])) if np.any(de > 0) else 0.0
    step_enc = float(idx.encoder[i] - idx.encoder[i - 1])
    if notch <= 0 or not (0.5 * notch <= step_enc <= 1.5 * notch):
        return None
    fixed = ptp.copy()
    if i <= len(ptp) - i:                     # the part before the step is shorter
        fixed[:i] += jump
        moved, side = i, "before"
    else:
        fixed[i:] -= jump
        moved, side = len(ptp) - i, "after"
    if not np.all(np.diff(fixed) > 0):
        return None
    idx.ptp_us = fixed
    return {"frame": int(idx.frame[i]), "row": i, "jump_us": jump,
            "moved_rows": moved, "side": side, "n_rows": len(ptp)}


# The sensor-clock repair (Derek, 2026-10-24).
#
# Each Gocator row carries two clocks: ptpTimestamp (PTP, what places the
# profile) and `timestamp`, the sensor's own free-running clock, which never
# sees PTP. Lined up, the two differ by a slow drift and nothing else - the
# clean left laser of 20261016.124201 holds within 0.1 ms over 14,054 rows.
# While the laser locks onto PTP at the start of a run its PTP time can step,
# and on every file seen it stepped in a +X, +2X, -X pattern:
#
#   20261016.124201 R  +62 ms row 7, +124 ms row 13, -62 ms row 33
#   20261014.173643 R  +20 ms row 21, +40 ms row 45, -20 ms row 94
#   20261007.120330 L  +14 ms row 89, +28 ms row 91, -14 ms row 149
#
# The older repair (_repair_ptp_step) sees only the one BACKWARD step and
# moved every row before it by that one amount, so the rows before the
# forward steps stayed 2-3 steps off (124201 R rows 0-12: 124-187 ms). And a
# file whose clock only stepped FORWARD was never caught at all.
#
# So every file is lined up against its own sensor clock. Where the PTP time
# sits off the steady part by more than PTP_SENSOR_JUMP_US, that stretch is
# moved back onto it. Only when it is unmistakably the PTP clock: the sensor
# clock and frameIndex run on unbroken across every step, the encoder takes
# an ordinary step there, the moved stretches lie at the START or END of the
# run and together hold at most PTP_SENSOR_MAX_MOVED_FRAC of it, and no step
# is bigger than PTP_SENSOR_MAX_JUMP_US. Otherwise nothing is moved: a file
# whose PTP time still runs forward is kept with a warning (ptp_suspect), one
# that does not is refused as before.
PTP_SENSOR_JUMP_US = 5_000          # the clean files wander < 2 ms end to end
PTP_SENSOR_MAX_JUMP_US = 250_000
# the unsteady start is a few seconds, so on a short run it is a bigger
# share: 11-16% on eight short runs of 20261005-09 (499-4507 profiles)
PTP_SENSOR_MAX_MOVED_FRAC = 0.25


def _sensor_residual(ptp: np.ndarray, ts: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """PTP minus the sensor clock mapped onto PTP by a straight line fitted
    on `rows`, in microseconds."""
    a, b = np.polyfit(ts[rows].astype(np.float64), ptp[rows].astype(np.float64), 1)
    return ptp.astype(np.float64) - (a * ts.astype(np.float64) + b)


def _segments(res: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) runs of rows between residual jumps."""
    cuts = (np.where(np.abs(np.diff(res)) > PTP_SENSOR_JUMP_US)[0] + 1).tolist()
    bounds = [0] + cuts + [len(res)]
    return [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)]


def _align_ptp_to_sensor(idx: "GocatorIndex") -> str:
    """'unusable' (no sensor clock to judge by), 'clean' (no PTP step),
    'corrected' (steps moved back; idx.ptp_step says what) or 'suspect'
    (steps seen but not safe to move; idx.ptp_suspect says why)."""
    ts, ptp = idx.sensor_ts, idx.ptp_us
    n = len(ptp)
    if ts is None or n < 50 or len(ts) != n or not np.all(np.diff(ts) > 0):
        return "unusable"
    # pass 1 on a rate from every interval, pass 2 refitted on the main run
    dptp, dts = np.diff(ptp).astype(np.float64), np.diff(ts).astype(np.float64)
    ok = dptp > 0
    if int(ok.sum()) < 10:
        return "unusable"
    rate = float(np.median(dptp[ok] / dts[ok]))
    res = ptp - ts.astype(np.float64) * rate
    res = res - np.median(res)
    segs = _segments(res)
    main = max(segs, key=lambda s: s[1] - s[0])
    res = _sensor_residual(ptp, ts, np.arange(main[0], main[1]))
    segs = _segments(res)
    if len(segs) == 1:
        return "clean"
    main = max(segs, key=lambda s: s[1] - s[0])

    def suspect(why: str) -> str:
        idx.ptp_suspect = {"why": why, "steps": len(segs) - 1,
                           "first_frame": int(idx.frame[segs[1][0]]),
                           "n_rows": n}
        return "suspect"

    others = [s for s in segs if s != main]
    moved = sum(e - s for s, e in others)
    if moved > PTP_SENSOR_MAX_MOVED_FRAC * n:
        return suspect(f"PTP steps cover {moved} of {n} profiles - too many to move")
    # frames, sensor clock and encoder must run on unbroken at every step
    de = np.diff(idx.encoder)
    notch = float(np.median(de[de > 0])) if np.any(de > 0) else 0.0
    jumps = []
    for s, e in segs[1:]:
        k = s                                  # first row after the step
        if int(idx.frame[k] - idx.frame[k - 1]) != 1:
            return suspect(f"frames missing at the PTP step at frame {int(idx.frame[k])}")
        # the spacing AROUND the step: it follows speed, and the cart is
        # slow at the start, so a run-wide median misjudges it
        near = np.delete(dts[max(k - 11, 0):k + 10], min(k - 1, 10))
        sdt = float(np.median(near)) if len(near) else float(np.median(dts))
        if not (0.5 * sdt <= float(ts[k] - ts[k - 1]) <= 1.5 * sdt):
            return suspect(f"the sensor clock also jumped at frame {int(idx.frame[k])}")
        step_enc = float(idx.encoder[k] - idx.encoder[k - 1])
        if notch <= 0 or not (0.5 * notch <= step_enc <= 1.5 * notch):
            return suspect(f"the encoder jumped at the PTP step at frame {int(idx.frame[k])}")
        jumps.append((int(idx.frame[k]), k, float(res[k] - res[k - 1])))
    offsets = [float(np.median(res[s:e])) - float(np.median(res[main[0]:main[1]]))
               for s, e in others]
    if max(abs(o) for o in offsets) > PTP_SENSOR_MAX_JUMP_US:
        return suspect("a PTP step is bigger than "
                       f"{PTP_SENSOR_MAX_JUMP_US / 1000:.0f} ms")
    fixed = ptp.copy()
    for (s, e), off in zip(others, offsets):
        fixed[s:e] = ptp[s:e] - int(round(off))
    if not np.all(np.diff(fixed) > 0):
        return suspect("the PTP time still does not run forward once moved")
    idx.ptp_us = fixed
    idx.ptp_step = {
        "method": "sensor clock", "n_rows": n, "moved_rows": moved,
        "frame": jumps[0][0], "row": jumps[0][1],
        "steps_ms": [round(j / 1000.0, 1) for _, _, j in jumps],
        "step_frames": [f for f, _, _ in jumps],
        "max_shift_ms": round(max(abs(o) for o in offsets) / 1000.0, 1),
        "side": "before" if others[0][0] == 0 else "after",
    }
    return "corrected"


# ---------------------------------------------------------------------------
# Rear image folder
# ---------------------------------------------------------------------------

@dataclass
class ImageSet:
    """Rear-camera JPEGs; filename stem is a distance counter in millimetres."""

    dir: str
    files: list[str]            # sorted filenames
    counter_mm: np.ndarray      # int64, parallel to files
    # images filed away in BEFORE_DIR: not listed above (nothing places them
    # any more) but still part of this run, so the trigger count can be
    # reconciled against the run as collected rather than as it now looks
    n_set_aside: int = 0

    @classmethod
    def scan(cls, images_dir: Path) -> "ImageSet":
        images_dir = Path(images_dir)
        if not images_dir.is_dir():
            raise ParseError(images_dir, "images directory does not exist")
        # A folder renamed by core.rename carries a rename_manifest.csv
        # mapping each new name back to the original odometer name. This is
        # NOT optional: a renamed name is the corrected distance into the
        # section, in the very same 12-digit-millimetre form as the odometer
        # name it replaced, so the two cannot be told apart by looking. And
        # matching NEEDS the odometer counter — it is what pairs images with
        # triggers. So the counter comes from the manifest whenever one is
        # there, and the on-disk name is kept only for display and writing.
        # Delete the manifest from a renamed folder and this scan will
        # silently read distances as counters.
        counter_for: dict[str, int] = {}
        manifest = images_dir / "rename_manifest.csv"
        if manifest.is_file():
            import csv as _csv
            with open(manifest, newline="", encoding="utf-8") as fh:
                for row in _csv.DictReader(fh):
                    new_name = (row.get("new_name") or "").strip()
                    orig = Path(row.get("original_name") or "").stem
                    if new_name and orig.isdigit():
                        counter_for[new_name] = int(orig)
        entries = []
        for p in sorted(images_dir.iterdir()):
            if p.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            if p.name in counter_for:
                entries.append((counter_for[p.name], p.name))
            elif p.stem.isdigit():
                entries.append((int(p.stem), p.name))
        if not entries:
            raise ParseError(images_dir, "no numbered .jpg files found")
        entries.sort()
        counters = np.asarray([e[0] for e in entries], dtype=np.int64)
        aside = images_dir / BEFORE_DIR
        n_aside = len(list(aside.glob("*.jpg"))) if aside.is_dir() else 0
        return cls(dir=str(images_dir), files=[e[1] for e in entries],
                   counter_mm=counters, n_set_aside=n_aside)

    def __len__(self) -> int:
        return len(self.files)

    @property
    def dist_m(self) -> np.ndarray:
        return self.counter_mm.astype(np.float64) / 1000.0

    def sequence_gaps(self, step_mm: int = IMAGE_STEP_MM) -> list[int]:
        """Missing counter values implied by an arithmetic sequence with `step_mm`."""
        gaps: list[int] = []
        c = self.counter_mm
        for i in range(1, len(c)):
            delta = int(c[i] - c[i - 1])
            if delta != step_mm:
                gaps.extend(range(int(c[i - 1]) + step_mm, int(c[i]), step_mm))
        return gaps

    def path(self, i: int) -> Path:
        return Path(self.dir) / self.files[i]

    def is_readable_jpeg(self, i: int) -> bool:
        try:
            with open(self.path(i), "rb") as fh:
                return fh.read(2) == JPEG_SOI
        except OSError:
            return False
