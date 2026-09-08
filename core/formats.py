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
# The scale is CONFIGURED, not fitted (Derek's Gocator settings, 2026-08-20):
#   0.235116 mm per encoder tic  ->  4253.1 counts/m
#   scan trigger every 23.98 mm  ->  23.98 / 0.235116 = 102.0 tics
# Verified in the data: median profile spacing in 20260817.175605L is exactly
# 102 counts. The 4223.6 figure previously in the spec is 0.7% low.
#
# NOTE: the raw counts are never logged by the SBG — DmiStationEx carries the
# SBG's *estimated* distance, and post-processing outputs only velocity. The
# Gocator's `encoder` column is the ONLY physical-wheel measurement recorded
# anywhere in a collection.
MM_PER_ENCODER_TIC = 0.235116
GOCATOR_COUNTS_PER_M = 1000.0 / MM_PER_ENCODER_TIC     # 4253.1
GOCATOR_SCAN_TRIGGER_MM = 23.98                        # = 102 tics


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


def parse_event_triggers(path: Path) -> np.ndarray:
    """eventOutA.txt -> strictly increasing SBG timestamps in microseconds (int64)."""
    names, data = _read_sbg_table(path)
    ts = data[:, 0].astype(np.int64)
    if len(ts) >= 2 and not np.all(np.diff(ts) > 0):
        bad = int(np.argmin(np.diff(ts) > 0)) + 1
        raise ParseError(path, f"trigger timestamps not strictly increasing near data row {bad}")
    return ts


@dataclass
class UtcTable:
    """SBG internal clock (us) <-> UTC epoch seconds, from utcTime.txt."""

    sbg_us: np.ndarray  # int64, strictly increasing
    epoch_s: np.ndarray  # float64 UTC unix seconds
    path: str = ""

    @classmethod
    def parse(cls, path: Path) -> "UtcTable":
        names, data = _read_sbg_table(path)
        need = ["timestamp", "year", "month", "day", "hour", "minute", "second", "nanosecond"]
        idx = {}
        for col in need:
            if col not in names:
                raise ParseError(path, f"missing column '{col}'")
            idx[col] = names.index(col)
        sbg = data[:, idx["timestamp"]].astype(np.int64)
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
        return cls(sbg_us=sbg, epoch_s=epochs, path=str(path))

    def sbg_to_utc(self, sbg_us) -> np.ndarray:
        """Linear interpolation; extrapolates linearly at the edges (SBG clock is stable)."""
        sbg_us = np.asarray(sbg_us, dtype=np.float64)
        t0, t1 = float(self.sbg_us[0]), float(self.sbg_us[-1])
        e0, e1 = float(self.epoch_s[0]), float(self.epoch_s[-1])
        out = np.interp(sbg_us, self.sbg_us.astype(np.float64), self.epoch_s)
        rate = (e1 - e0) / (t1 - t0) if t1 > t0 else 1e-6
        below, above = sbg_us < t0, sbg_us > t1
        out = np.where(below, e0 + (sbg_us - t0) * rate, out)
        out = np.where(above, e1 + (sbg_us - t1) * rate, out)
        return out

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

    @classmethod
    def build(cls, path: Path) -> "GocatorIndex":
        path = Path(path)
        frames, ptps, encs, npts, nval, offs = [], [], [], [], [], []
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
        )
        if len(idx.ptp_us) >= 2 and not np.all(np.diff(idx.ptp_us) > 0):
            raise ParseError(path, "ptpTimestamp not strictly increasing")
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
