"""Write corrected positions INTO the data files.

This is the app's product: ACS assigns positions its own (wrong) way; here we
stamp the positions recomputed from PTP time + the post-processed Qinertia
export onto the deliverables the client receives.

Two writers, both IN PLACE and both safe to re-run:
  - geotag_image()      : GPS EXIF into a JPEG (metadata only, pixels untouched)
  - inject_gocator_gps(): lat/lon/elev columns into a Gocator profile CSV

Safety model for in-place writes:
  * write a temp file in the same directory, fsync, then os.replace() — an
    interrupted run can never leave a half-written source file
  * JPEG: EXIF segment is inserted, image data is NOT re-encoded (no quality
    loss), and existing tags (CameraID, DateTime, ...) are preserved
  * idempotent: re-processing a run overwrites the previous values rather than
    duplicating columns/tags
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

GPS_COLUMNS = ("latitude", "longitude", "elevation")


def ensure_piexif() -> tuple[bool, str]:
    """Make the EXIF writer available, installing it from the bundled offline
    wheels if the venv predates it.

    Writing GPS needs piexif; a venv created before it was added to
    requirements.txt will not have it, and the user should not have to
    hand-install a dependency the app ships with.
    """
    try:
        import piexif  # noqa: F401
        return True, "piexif available"
    except ImportError:
        pass
    wheels = Path(__file__).resolve().parent.parent / "wheels"
    if not wheels.is_dir():
        return False, ("piexif is not installed and no bundled wheels/ folder was "
                       "found — run: pip install piexif")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-index",
             "--find-links", str(wheels), "piexif"],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:                      # pragma: no cover - env specific
        return False, f"piexif missing; auto-install could not run: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, ("piexif missing; auto-install from bundled wheels failed: "
                       + (tail[-1] if tail else f"pip exit {proc.returncode}"))
    importlib.invalidate_caches()
    try:
        import piexif  # noqa: F401
        return True, "piexif was missing and was installed from the bundled wheels"
    except ImportError:                            # pragma: no cover
        return False, "piexif installed but still not importable — restart the app"


_DEG_DEN = 1
_MIN_DEN = 1
_SEC_DEN = 1000000  # ~0.03 mm resolution


def _deg_to_dms_rational(value: float) -> tuple:
    value = abs(float(value))
    deg = int(value)
    rem = (value - deg) * 60.0
    minute = int(rem)
    sec = (rem - minute) * 60.0
    return ((deg, _DEG_DEN), (minute, _MIN_DEN), (int(round(sec * _SEC_DEN)), _SEC_DEN))


def _rational(value: float, den: int = 1000) -> tuple[int, int]:
    return (int(round(float(value) * den)), den)


def geotag_image(path: Path, lat_deg: float, lon_deg: float, alt_m: float,
                 utc_epoch_s: float, heading_deg: float | None = None,
                 speed_ms: float | None = None) -> None:
    """Insert/replace the GPS EXIF block in a JPEG, in place.

    Schema matches the Fugro ARAN PhotoDeveloper output (GPSLatitude/Ref,
    GPSLongitude/Ref, GPSAltitude/Ref, GPSTimeStamp, GPSDateStamp, GPSSpeed/Ref)
    plus GPSImgDirection when a heading is available. Existing EXIF is kept.
    """
    import piexif

    path = Path(path)
    if not (np.isfinite(lat_deg) and np.isfinite(lon_deg)):
        raise ValueError("cannot geotag without a finite lat/lon")
    try:
        exif = piexif.load(str(path))
    except Exception:  # no/blank EXIF — start a fresh structure
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif.setdefault("GPS", {})

    dt = datetime.fromtimestamp(float(utc_epoch_s), tz=timezone.utc)
    gps = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat_deg >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(lat_deg),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon_deg >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(lon_deg),
        piexif.GPSIFD.GPSTimeStamp: ((dt.hour, 1), (dt.minute, 1),
                                     (int(round((dt.second + dt.microsecond / 1e6) * 1000)), 1000)),
        piexif.GPSIFD.GPSDateStamp: dt.strftime("%Y:%m:%d"),
    }
    if np.isfinite(alt_m):
        gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt_m >= 0 else 1
        gps[piexif.GPSIFD.GPSAltitude] = _rational(abs(alt_m))
    if heading_deg is not None and np.isfinite(heading_deg):
        gps[piexif.GPSIFD.GPSImgDirectionRef] = "T"  # true north
        gps[piexif.GPSIFD.GPSImgDirection] = _rational(float(heading_deg) % 360.0)
    if speed_ms is not None and np.isfinite(speed_ms):
        gps[piexif.GPSIFD.GPSSpeedRef] = "K"  # km/h, as in the ARAN sample
        gps[piexif.GPSIFD.GPSSpeed] = _rational(float(speed_ms) * 3.6)

    exif["GPS"] = gps  # replace wholesale -> idempotent re-runs
    exif_bytes = piexif.dump(exif)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        # insert() rewrites only the metadata segment; image data is untouched
        piexif.insert(exif_bytes, str(path), str(tmp))
        with open(tmp, "rb+") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_image_gps(path: Path) -> dict | None:
    """Read back the GPS EXIF (for verification/tests). None if absent."""
    import piexif

    try:
        exif = piexif.load(str(path))
    except Exception:
        return None
    gps = exif.get("GPS") or {}
    if piexif.GPSIFD.GPSLatitude not in gps:
        return None

    def dms(vals):
        d, m, s = ((v[0] / v[1]) for v in vals)
        return d + m / 60.0 + s / 3600.0

    lat = dms(gps[piexif.GPSIFD.GPSLatitude])
    lon = dms(gps[piexif.GPSIFD.GPSLongitude])
    if gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N") in (b"S", "S"):
        lat = -lat
    if gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E") in (b"W", "W"):
        lon = -lon
    out = {"lat_deg": lat, "lon_deg": lon}
    if piexif.GPSIFD.GPSAltitude in gps:
        a = gps[piexif.GPSIFD.GPSAltitude]
        alt = a[0] / a[1]
        if gps.get(piexif.GPSIFD.GPSAltitudeRef, 0) in (1, b"\x01"):
            alt = -alt
        out["alt_m"] = alt
    if piexif.GPSIFD.GPSImgDirection in gps:
        d = gps[piexif.GPSIFD.GPSImgDirection]
        out["heading_deg"] = d[0] / d[1]
    return out


def inject_gocator_gps(path: Path, lat: np.ndarray, lon: np.ndarray,
                       elev: np.ndarray, progress=None) -> int:
    """Insert (or refresh) latitude/longitude/elevation columns in a Gocator
    profile CSV, in place, streaming — safe for 130 MB files.

    Columns are inserted directly after `status`, i.e. immediately before the
    x0/z0 point pairs, and the header is updated to match. Re-running replaces
    the values instead of adding duplicate columns.

    Returns the number of data rows written.
    """
    path = Path(path)
    n = len(lat)
    tmp = path.with_suffix(path.suffix + ".tmp")
    written = 0
    try:
        written = _write_gocator_with_gps(path, tmp, lat, lon, elev, n, progress)
    except Exception:
        tmp.unlink(missing_ok=True)   # original left untouched
        raise
    if written != n:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"{path}: wrote {written} rows but had {n} positions")
    os.replace(tmp, path)
    return written


def _fmt(value, decimals: int) -> str:
    """CSV cell for one position component; blank when there is no position."""
    return "" if not np.isfinite(value) else f"{value:.{decimals}f}"


def _write_gocator_with_gps(path: Path, tmp: Path, lat, lon, elev, n, progress) -> int:
    written = 0
    with open(path, "r", encoding="utf-8", newline="") as src, \
            open(tmp, "w", encoding="utf-8", newline="") as dst:
        header = src.readline().rstrip("\r\n")
        cols = header.split(",")
        try:
            x0 = cols.index("x0")
        except ValueError:
            raise ValueError(f"{path}: no 'x0' column in header — not a Gocator profile CSV")
        already = [c for c in GPS_COLUMNS if c in cols]
        if already and already != list(GPS_COLUMNS):
            raise ValueError(f"{path}: partial GPS columns present {already}; refusing to guess")
        meta_end = cols.index(GPS_COLUMNS[0]) if already else x0
        new_header = cols[:meta_end] + list(GPS_COLUMNS) + cols[x0:]
        dst.write(",".join(new_header) + "\n")

        # `written` is the DATA-ROW counter and indexes the position arrays;
        # `line_no` is only for messages. They differ whenever the file contains
        # a blank line, which the index builder skips too — using the raw line
        # number here made one stray blank line abort the whole injection.
        for line_no, line in enumerate(src, start=2):
            line = line.rstrip("\r\n")
            if not line:
                continue
            if written >= n:
                raise ValueError(f"{path}: more data rows than supplied positions ({n})")
            parts = line.split(",")
            if len(parts) < x0:
                raise ValueError(f"{path}: row {line_no} has too few columns")
            row = (parts[:meta_end]
                   + [_fmt(lat[written], 9), _fmt(lon[written], 9), _fmt(elev[written], 3)]
                   + parts[x0:])
            dst.write(",".join(row) + "\n")
            written += 1
            if progress is not None and written % 2000 == 0:
                progress(written, n)
        dst.flush()
        os.fsync(dst.fileno())
    return written
