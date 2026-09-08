"""Shared fixtures.

Synthetic fixtures simulate a tiny but physically consistent run:
a cart that accelerates, cruises and stops, with triggers every 0.75 m,
a Gocator on the PTP/TAI clock (+37 s), and a 50 Hz nav table.

Integration tests use real sample data; set LIA_SAMPLE_ROOT / LIA_CAL_DIR to
override the default F: locations. They auto-skip when the data is absent.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

TAI_UTC = 37.0
T0_UTC = datetime(2026, 8, 16, 15, 0, 0, tzinfo=timezone.utc).timestamp()
SBG0_US = 500_000_000  # SBG internal clock at T0_UTC


def synth_motion(duration_s=120.0, dt=0.02):
    """Speed profile: 5 s standstill, ramp to 1.5 m/s, cruise, ramp down, stop."""
    t = np.arange(0.0, duration_s, dt)
    v = np.piecewise(
        t,
        [t < 5, (t >= 5) & (t < 15), (t >= 15) & (t < 100), (t >= 100) & (t < 110), t >= 110],
        [0.0, lambda x: 1.5 * (x - 5) / 10, 1.5, lambda x: 1.5 * (1 - (x - 100) / 10), 0.0],
    )
    d = np.cumsum(v) * dt
    return t, v, d


@pytest.fixture(scope="session")
def synth():
    t, v, d = synth_motion()
    utc = T0_UTC + t
    # triggers every 0.75 m of travel
    trig_d = np.arange(0.75, d[-1], 0.75)
    trig_utc = np.interp(trig_d, d, utc)
    trig_sbg_us = ((trig_utc - T0_UTC) * 1e6 + SBG0_US).astype(np.int64)
    # gocator: profile every 0.05 m while moving, ptp = utc + 37
    goc_d = np.arange(0.02, d[-1], 0.05)
    goc_utc = np.interp(goc_d, d, utc)
    goc_ptp_us = ((goc_utc + TAI_UTC) * 1e6).astype(np.int64)
    goc_enc = (goc_d * 4223.6).astype(np.int64)
    return dict(t=t, v=v, d=d, utc=utc, trig_d=trig_d, trig_utc=trig_utc,
                trig_sbg_us=trig_sbg_us, goc_d=goc_d, goc_utc=goc_utc,
                goc_ptp_us=goc_ptp_us, goc_enc=goc_enc)


def write_sbg_table(path: Path, names, units, rows):
    lines = ["\t".join(names), "\t".join(units)]
    for r in rows:
        lines.append("\t".join(r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def synth_run_root(tmp_path, synth):
    """A complete synthetic run root with one run + 1 pre-collection image."""
    run = "20260816.150000"
    date, tm = run.split(".")
    root = tmp_path / "root"
    rear = root / "Images" / run / "Rear"
    goc = root / "GoCatorData" / run
    logger = root / "SBGData" / "260816" / f"{run} DataLogger"
    exp = root / "SBGData" / "20260816.145000_0001" / "export"
    for p in (rear, goc, logger, exp):
        p.mkdir(parents=True)

    s = synth
    n_trig = len(s["trig_sbg_us"])
    # images: one extra at the start (pre-collection trigger) -> n_trig + 1.
    # Real (tiny) JPEGs, so EXIF geotagging can be exercised against them.
    try:
        import cv2
        import numpy as _np

        blob = cv2.imencode(".jpg", _np.full((32, 32, 3), 128, dtype=_np.uint8))[1].tobytes()
    except Exception:  # pragma: no cover - cv2 always present in this project
        blob = b"\xff\xd8\xff\xe0synthetic"
    row = root / "Images" / run / "ROW"   # second camera, same triggers
    row.mkdir(parents=True, exist_ok=True)
    for j in range(n_trig + 1):
        (rear / f"{(j + 1) * 750:012d}.jpg").write_bytes(blob)
        (row / f"{(j + 1) * 750:012d}.jpg").write_bytes(blob)

    # eventOutA
    write_sbg_table(
        logger / "eventOutA.txt",
        ["timestamp", "status", "timeOffset0", "timeOffset1", "timeOffset2", "timeOffset3"],
        ["(us)", "(na)", "(us)", "(us)", "(us)", "(us)"],
        [[str(int(ts)), "0x0000", "0", "0", "0", "0"] for ts in s["trig_sbg_us"]],
    )
    # utcTime: 1 Hz
    rows = []
    for k in range(0, 125):
        utc = T0_UTC + k
        dt = datetime.fromtimestamp(utc, tz=timezone.utc)
        rows.append([
            str(SBG0_US + k * 1_000_000), "0x00a7", str(239292000 + k * 1000),
            str(dt.year), str(dt.month), str(dt.day), str(dt.hour), str(dt.minute),
            str(dt.second), "0", "0.08", "10.2", "-0.08",
        ])
    write_sbg_table(
        logger / "utcTime.txt",
        ["timestamp", "status", "gpsTimeOfWeek", "year", "month", "day", "hour",
         "minute", "second", "nanosecond", "clkBiasStd", "clkSfErrorStd", "clkResidualError"],
        ["(us)"] * 13, rows,
    )
    # DmiStationEx
    with open(logger / f"DmiStationEx {run}.csv", "w", encoding="utf-8") as fh:
        fh.write("Dmi (m), Speed (m/s), UTC TS (s), TimeOfWeek (s)\n")
        for t, v, d in zip(s["t"], s["v"], s["d"]):
            fh.write(f"{d:.3f}, {v:.3f}, {T0_UTC + t:.3f}, 0.0\n")
    # gocator L/R csv: 4 points per profile
    for side in "LR":
        with open(goc / f"{date}T{tm}{side}.csv", "w", encoding="utf-8") as fh:
            fh.write("frameIndex,timestamp,ptpTimestamp,encoder,numberOfProfilePoints,"
                     "numberOfValidPoints,bridgedValue,status,x0,z0,x1,z1,x2,z2,x3,z3\n")
            for i, (ptp, enc) in enumerate(zip(s["goc_ptp_us"], s["goc_enc"]), start=1):
                fh.write(f"{i},{i * 1000},{ptp},{enc},4,3,-1.0,0,-10.0,-1.0,,,0.0,-1.2,10.0,-1.1\n")
    # nav export at 50 Hz, UTC time-of-day + GPS TOW (+18 s), around a fixed lat/lon
    with open(exp / "ascii-output.txt", "w", encoding="utf-8") as fh:
        fh.write("5.1.2457-stable\nsynthetic\nColumn #,\tUnit,\tDescription:\n\n")
        fh.write("GPS Time\t    UTC Time\tRoll\tPitch\tYaw\tLatitude\tLongitude\tAltitude MSL\n")
        fh.write("(S)\t(HH:MM:SS.SS\t(°)\t(°)\t(°)\t(°)\t(°)\t(m)\n")
        for t, d in zip(s["t"][::1], s["d"][::1]):
            utc = T0_UTC + t
            dt = datetime.fromtimestamp(utc, tz=timezone.utc)
            tod = dt.strftime("%H:%M:%S") + f".{int(dt.microsecond / 1000):03d}"
            # lat advances so along-track distance == d exactly; lon constant
            lat = 43.4363 + d / 111320.0
            lon = -80.3152
            tow = (utc % 86400) + 18.0
            fh.write(f"{tow:.3f}\t{tod}\t0.5\t0.0\t-16.0\t{lat:.9f}\t{lon:.9f}\t265.0\t"[:-1] + "\n")
    return root


@pytest.fixture()
def synth_cal_dir(tmp_path):
    cal = tmp_path / "cal" / "20260815_PaveIntrinsic"
    cal.mkdir(parents=True)
    (cal / "PAVE_TEST.yaml").write_text(
        "serial_number: 'TEST'\n"
        "calibration_datetime: '2026-08-11T20:23:57'\n"
        "rms_reprojection_error: 0.34\n"
        "image_count_used: 53\n"
        "verdict: Excellent\n"
        "camera_matrix:\n  rows: 3\n  cols: 3\n  data:\n"
        "  - 2757.591943\n  - 0.0\n  - 1417.737625\n"
        "  - 0.0\n  - 2758.043961\n  - 918.775062\n"
        "  - 0.0\n  - 0.0\n  - 1.0\n"
        "dist_coeffs:\n  rows: 1\n  cols: 5\n  data:\n"
        "  - -0.11547295\n  - 0.11742459\n  - -0.00099769\n  - -0.00087287\n  - -0.03682034\n"
        "image_resolution:\n  width: 2880\n  height: 1860\n"
        "board:\n  squares_x: 12\n  squares_y: 8\n  cell_width_mm: 76.2\n"
        "  cell_height_mm: 76.2\n  marker_size_mm: 60.0\n  aruco_dict: DICT_5X5_50\n",
        encoding="utf-8",
    )
    # the lever-arm file is a REQUIRED input; the numbers are the ones in
    # force when these tests were written (Rear 0.928, ROW 1.69 behind)
    from core.calibration import LEVER_ARMS_FILENAME
    (tmp_path / "cal" / LEVER_ARMS_FILENAME).write_text(
        "# lever arms\n\n## Rear camera\n\nX  -0.928\nY   0\nZ   0\n\n"
        "## ROW camera\n\nX  -1.69\nY   0\nZ   0\n\n"
        "## Gocator left\n\nX   0\nY  -0.3575\nZ   0\n\n"
        "## Gocator right\n\nX   0\nY  +0.3575\nZ   0\n\n"
        "## Rear lens height above the pavement\n\n1.667\n",
        encoding="utf-8")
    return tmp_path / "cal"


# --- real sample data (integration) -----------------------------------------

SAMPLE_ROOT = Path(os.environ.get(
    "LIA_SAMPLE_ROOT", r"F:\Sidewalk\099_CollectedData\20260816_Routed_IMages0.75_gocatordata_23.98"))
CAL_DIR = Path(os.environ.get(
    "LIA_CAL_DIR", r"F:\Sidewalk\002_App\SidewalkProfilier\100_AllCalibrations"))

sampledata = pytest.mark.skipif(
    not SAMPLE_ROOT.is_dir(), reason=f"sample data not available at {SAMPLE_ROOT}")


@pytest.fixture(scope="session")
def sample_root():
    return SAMPLE_ROOT


@pytest.fixture(scope="session")
def sample_cal_dir():
    return CAL_DIR
