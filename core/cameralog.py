"""Which trigger took each photo - read from the ACS camera log.

WHY (Derek, 2026-10-20). "photo n = trigger n-1" (the start-up-frame rule,
proven with the yardstick on 2026-10-10) only holds when the cameras are
ready before the first trigger. Run 20261014.094844 started at 3.1 m/s,
logging began before the cameras were ready, and the camera log showed
ROW photos 100/200/300 came from triggers 102/202/302 (0 ms error) and Rear
from 104/204/304 (1 ms). The n-1 rule would have put every Rear photo
3.75 m off.

The camera log (<collection>/<day>/Logs/<PC>/*VideoLogCXP.log) writes a line
for every 100th photo of each camera:

    2026-10-14 09:50:02,375 0x... root - ProcessingFunction, FrameID: 100,
    EventTimeStamp: 594.2888, MilErr: 0, File: ...\\Images\\<run>\\Rear\\000000075000.jpg

EventTimeStamp is the CAMERA's clock at the picture. The gap between two such
times must equal the gap between the two triggers that took them - so trying
every offset k (photo n <- trigger n+k) and keeping the one that reproduces
the gaps identifies k. It needs two of those lines per camera: 200+ photos.

For shorter runs the log still says when acquisition started
("VC2_StartAcquisition out", PC clock). The SBG log ("CallbackUtc, TS HWUnix:
..") ties the PC clock to GPS time. If the cameras were ready well before the
first trigger fired, the start-up-frame rule holds; if triggers fired before
they were ready, those are counted - an estimate, reported as one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

CXP_GLOB = "*VideoLogCXP*.log*"
SBG_GLOB = "*-Server-SBG.log*"

# a camera-log fit is accepted when the best offset reproduces every gap to
# within FIT_MAX_ERR_S and the runner-up is clearly worse
FIT_MAX_ERR_S = 0.015
FIT_MIN_MARGIN_S = 0.010
FIT_K_RANGE = range(-3, 61)
# start-time decisions need this much room: the PC clock is tied to GPS time
# only to about 0.1 s through the SBG log, and the log line is written a
# little after the camera call returns
START_MARGIN_S = 1.0

_TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3})"
_FRAME_RE = re.compile(
    _TS + r".*ProcessingFunction, FrameID: (\d+), EventTimeStamp: ([\d.]+).*?"
    r"File: .*[\\/]Images[\\/](\d{8}\.\d{6})[\\/]([^\\/]+)[\\/](-?\d+)\.jpg")
_START_RE = re.compile(_TS + r".*VC2_StartAcquisition out")
_STOP_RE = re.compile(_TS + r".*VC2_StopAcquisition in")
_SBG_RE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}).*CallbackUtc, TS HWUnix: (\d+)")


def _local_epoch(s: str) -> float:
    """PC wall-clock text as seconds, read as if it were UTC. Only differences
    and the SBG-derived offset are ever used, so the time zone cancels."""
    return datetime.strptime(s.replace(",", "."), "%Y-%m-%d %H:%M:%S.%f") \
        .replace(tzinfo=timezone.utc).timestamp()


@dataclass
class CameraLog:
    """Everything the camera log (and its SBG log, for the clock) says."""

    paths: list = field(default_factory=list)
    # (run, camera) -> [(photo ordinal, camera-clock seconds)]
    frames: dict = field(default_factory=dict)
    starts_local: np.ndarray = field(default_factory=lambda: np.empty(0))
    stops_local: np.ndarray = field(default_factory=lambda: np.empty(0))
    # PC wall clock -> UTC: utc = local + offset, sampled through the day
    clock_local: np.ndarray = field(default_factory=lambda: np.empty(0))
    clock_offset: np.ndarray = field(default_factory=lambda: np.empty(0))

    def local_to_utc(self, t_local: float) -> float | None:
        if not len(self.clock_local):
            return None
        i = int(np.argmin(np.abs(self.clock_local - t_local)))
        # no clock sample within an hour: the offset may have stepped
        if abs(self.clock_local[i] - t_local) > 3600:
            return None
        lo, hi = max(0, i - 25), min(len(self.clock_local), i + 25)
        return float(t_local + np.median(self.clock_offset[lo:hi]))


_CACHE: dict = {}


def load(cxp_path: Path) -> CameraLog:
    """Parse the camera log at `cxp_path`, every rotated piece of it beside it,
    and the SBG log in the same folder. Cached on (folder, newest mtime)."""
    folder = Path(cxp_path).parent
    cxp = sorted(folder.glob(CXP_GLOB))
    sbg = sorted(folder.glob(SBG_GLOB))
    key = (str(folder), max((p.stat().st_mtime for p in cxp + sbg), default=0))
    if key in _CACHE:
        return _CACHE[key]
    log = CameraLog(paths=[str(p) for p in cxp + sbg])
    starts, stops = [], []
    for p in cxp:
        for line in open(p, encoding="utf-8", errors="ignore"):
            if "FrameID" in line:
                m = _FRAME_RE.search(line)
                if m:
                    run, cam, name = m.group(4), m.group(5), m.group(6)
                    ordinal = int(round(abs(int(name)) / 750.0))
                    log.frames.setdefault((run, cam), []).append(
                        (ordinal, float(m.group(3))))
            elif "VC2_StartAcquisition out" in line:
                m = _START_RE.search(line)
                if m:
                    starts.append(_local_epoch(m.group(1)))
            elif "VC2_StopAcquisition in" in line:
                m = _STOP_RE.search(line)
                if m:
                    stops.append(_local_epoch(m.group(1)))
    cl, co = [], []
    for p in sbg:
        for line in open(p, encoding="utf-8", errors="ignore"):
            if "CallbackUtc" in line:
                m = _SBG_RE.search(line)
                if m:
                    t = _local_epoch(m.group(1))
                    cl.append(t)
                    co.append(float(m.group(2)) - t)
    for k in log.frames:
        log.frames[k] = sorted(set(log.frames[k]))
    log.starts_local = np.array(sorted(starts))
    log.stops_local = np.array(sorted(stops))
    o = np.argsort(cl)
    log.clock_local = np.asarray(cl)[o] if cl else np.empty(0)
    log.clock_offset = np.asarray(co)[o] if co else np.empty(0)
    _CACHE[key] = log
    return log


def mentions_run(cxp_path: Path, run_id: str) -> bool:
    """Does this camera log hold photo lines for `run_id`?"""
    log = load(cxp_path)
    return any(r == run_id for r, _ in log.frames)


def run_start_local(run_id: str) -> float | None:
    """The run stamp (PC clock, e.g. 20261014.094844) on the same scale as
    the log times."""
    try:
        return datetime.strptime(run_id, "%Y%m%d.%H%M%S") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def start_for_run(log: CameraLog, run_id: str) -> float | None:
    """PC-clock time the cameras started for this run, or None. ACS names the
    run folder, then starts the cameras about 1-2 s later (094844: stamp
    09:48:44, cameras ready 09:48:45.5). Runs can follow each other within a
    minute, so take the first start just after the stamp."""
    t0 = run_start_local(run_id)
    if t0 is None or not len(log.starts_local):
        return None
    cand = log.starts_local[(log.starts_local >= t0 - 5) & (log.starts_local <= t0 + 20)]
    return float(cand[0]) if len(cand) else None


def find_logs(root: Path) -> list[Path]:
    """One camera log per Logs folder under a collection (the un-numbered
    file; load() reads the rotated pieces beside it)."""
    import fnmatch
    import os
    # the data folders hold tens of thousands of files and never a log
    skip = {"images", "gocatordata", "sbgdata", "beforecollection"}
    by_folder: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip]
        here = Path(dirpath)
        if "logs" not in [s.lower() for s in here.parts]:
            continue
        for name in fnmatch.filter(filenames, CXP_GLOB):
            cur = by_folder.get(here)
            if cur is None or len(name) < len(cur.name):
                by_folder[here] = here / name
    return sorted(by_folder.values())


def pick_log(paths: list[Path], run_id: str) -> Path | None:
    """The log that has this run's photo lines, else one that has its camera
    start, else None (so the user can Locate one)."""
    loaded = []
    for p in paths:
        try:
            loaded.append((p, load(p)))
        except OSError:
            continue
    for p, log in loaded:
        if any(r == run_id for r, _ in log.frames):
            return p
    for p, log in loaded:
        if start_for_run(log, run_id) is not None:
            return p
    return None


@dataclass
class OffsetResult:
    """photo n <- trigger n + k (1-based), and how that was decided."""

    k: int | None
    method: str          # "camera log", "camera start", "assumed"
    confident: bool
    message: str


def fit_frames(frames: list, trig_utc: np.ndarray) -> OffsetResult | None:
    """Offset from two or more camera-clock frame times, or None when the log
    has fewer than two for this camera or no offset stands out."""
    if len(frames) < 2:
        return None
    ords = np.array([f[0] for f in frames], dtype=np.int64)
    cam_t = np.array([f[1] for f in frames], dtype=np.float64)
    gaps = np.diff(cam_t)
    n = len(trig_utc)
    scores = []
    for k in FIT_K_RANGE:
        idx = ords + k - 1                      # 0-based trigger of each photo
        if idx.min() < 0 or idx.max() >= n:
            continue
        err = np.abs(np.diff(trig_utc[idx]) - gaps).max()
        scores.append((float(err), k))
    if len(scores) < 2:
        return None
    scores.sort()
    (best, k), (second, _) = scores[0], scores[1]
    shown = "/".join(str(int(o)) for o in ords)
    if best <= FIT_MAX_ERR_S and second - best >= FIT_MIN_MARGIN_S:
        return OffsetResult(
            k, "camera log", True,
            f"camera log: photos {shown} came from triggers "
            f"{'/'.join(str(int(o) + k) for o in ords)} (worst {best * 1000:.0f} ms, "
            f"next offset {second * 1000:.0f} ms) - photo n = trigger n{k:+d}")
    return OffsetResult(
        None, "camera log", False,
        f"camera log has photos {shown} but no trigger offset fits clearly "
        f"(best {best * 1000:.0f} ms, next {second * 1000:.0f} ms)")


def from_start(log: CameraLog, trig_utc: np.ndarray, delay_s: float,
               run_start_local: float) -> OffsetResult | None:
    """Offset from when acquisition started. None when the log has no start
    for this run or the PC clock cannot be tied to GPS time."""
    if not len(log.starts_local) or not len(trig_utc) or run_start_local is None:
        return None
    # the start belonging to this run - see start_for_run
    cand = log.starts_local[(log.starts_local >= run_start_local - 5)
                            & (log.starts_local <= run_start_local + 20)]
    if not len(cand):
        return None
    ready = log.local_to_utc(float(cand[0]))
    if ready is None:
        return None
    first_exposure = float(trig_utc[0]) + float(delay_s)
    lead = first_exposure - ready
    if lead >= START_MARGIN_S:
        return OffsetResult(
            -1, "camera start", True,
            f"cameras were ready {lead:.1f} s before the first trigger's picture - "
            "photo 1 is the start-up frame, photo n = trigger n-1")
    missed = int(np.sum(trig_utc + delay_s < ready))
    near = float(np.min(np.abs(trig_utc + delay_s - ready)))
    if lead <= -START_MARGIN_S and near >= 0.15:
        k = missed - 1
        return OffsetResult(
            k, "camera start", False,
            f"ESTIMATED from the camera start: {missed} trigger(s) fired before the "
            f"cameras were ready ({-lead:.1f} s late) - photo n = trigger n{k:+d}. "
            "Spot-check this run (moving start)")
    return None


def decide(log_path: Path | None, run_id: str, cam: str, trig_utc: np.ndarray,
           delay_s: float, n_photos: int) -> OffsetResult:
    """Which trigger took photo n, in Derek's order (2026-10-20):

    1. the camera log's frame times (200+ photos) - PASS
    2. the camera start vs the triggers (short runs) - PASS when the cameras
       were ready well before, an ESTIMATE (WARN) when triggers fired first
    3. nothing usable - photo n = trigger n-1 assumed (WARN), with the count
       check spelled out

    n_photos counts every photo the camera made in this run, including any
    already filed in BeforeCollection.
    """
    trig_utc = np.asarray(trig_utc, dtype=np.float64)
    notes = []
    log = None
    if log_path is not None:
        try:
            log = load(Path(log_path))
        except OSError as e:
            notes.append(f"camera log unreadable ({e})")
    if log is not None:
        r = fit_frames(log.frames.get((run_id, cam), []), trig_utc)
        if r is not None and r.confident:
            return r
        if r is not None:
            notes.append(r.message)
        s = from_start(log, trig_utc, delay_s, run_start_local(run_id))
        if s is not None:
            if notes:
                s.message += " (" + "; ".join(notes) + ")"
            return s
        if start_for_run(log, run_id) is None:
            notes.append("camera log has no start for this run")
        else:
            notes.append("camera start found but the PC clock could not be tied "
                         "to GPS time (no SBG log lines near it) or it was too "
                         "close to a trigger to call")
    else:
        notes.append("no camera log")
    # Expected with photo n = trigger n-1: one more photo than triggers (the
    # start-up frame). Fewer photos is normal when the cameras stop while
    # still moving; more is not.
    extra = n_photos - len(trig_utc)
    fit = ("photo count fits (one start-up frame)" if extra == 1 else
           f"{-extra + 1} trigger(s) at the end got no photo" if extra < 1 else
           f"COUNT DOES NOT FIT: {extra} more photos than triggers")
    return OffsetResult(
        -1, "assumed", False,
        "pairing NOT confirmed - photo n = trigger n-1 assumed ("
        + "; ".join(notes) + f"). {n_photos} photos, {len(trig_utc)} triggers: {fit}")
