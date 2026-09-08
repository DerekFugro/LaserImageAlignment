"""Run discovery and per-run path overrides.

A "run root" is a collection-day folder (e.g. 20260818_RoutedRuns_075Images)
containing Images/, GoCatorData/ and SBGData/ trees. Runs are matched across
the trees by their shared YYYYMMDD.HHMMSS stamp; the SBG date folder may use a
different date style (e.g. 260818), so we match on the run stamp only.

User-chosen locations for missing inputs (e.g. the Qinertia ascii-output.txt
living in another dataset) are stored in a JSON sidecar in the APP's own data
directory — never inside the collected-data folders (they are read-only).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RUN_STAMP_RE = re.compile(r"^(\d{8})\.(\d{6})$")

# The SBG logger and the collection system open their per-run folders
# independently, so the same physical run can be named a second or two apart:
# on 2026-08-24 the images and Gocator CSVs said 20260824.100258 while the log
# folder said "20260824.100259 DataLogger". Matched on the exact string, that
# ONE run became TWO half-runs — images with no triggers, triggers with no
# images — and both were skipped, so 86 images and 2 Gocator files silently got
# no GPS at all. Allow a small skew instead.
#
# 3 s is deliberately far below the gap it could confuse: consecutive runs start
# about a minute apart in practice (60 s was the closest seen), so this is ~20x
# inside the margin. Exact matches always win, and an ambiguous pairing is left
# unmatched rather than guessed.
MAX_SBG_STAMP_SKEW_S = 3
SBG_LOGGER_SUFFIX = " DataLogger"
# LAST RESORT ONLY. `lia.ini` beside app.py is what an installation should
# set (see core.config); this is what an installation with no INI falls back
# to, which keeps the original machine behaving exactly as it always has.
# Moved 2026-09-08 from F:\Sidewalk\002_App\AllCalibrations, which now holds
# only the raw intrinsics, the backups and ReverseRunProcessor's own files.
DEFAULT_CALIBRATIONS_DIR = Path(
    r"F:\Sidewalk\002_App\SidewalkProfilier\100_AllCalibrations")

# Required input keys (used by QC presence checks and the overrides store)
KEY_IMAGES = "images_dir"
KEY_GOCATOR_L = "gocator_l"
KEY_GOCATOR_R = "gocator_r"
KEY_EVENT_A = "event_a"
KEY_UTC_TIME = "utc_time"
KEY_DMI = "dmi"
KEY_NAV_EXPORT = "nav_export"
KEY_CALIBRATION = "calibration"
KEY_EVENTS_EXPORT = "events_export"
KEY_LEVER_ARMS = "lever_arms"

REQUIRED_KEYS = [
    KEY_IMAGES, KEY_GOCATOR_L, KEY_GOCATOR_R, KEY_EVENT_A,
    KEY_UTC_TIME, KEY_DMI, KEY_NAV_EXPORT, KEY_CALIBRATION, KEY_LEVER_ARMS,
]
# KEY_LEVER_ARMS is REQUIRED and not optional (Derek, 2026-09-02): every
# written position has an arm in it, so without the file there is nothing
# correct to write. A missing file used to fall back to built-in constants -
# silently, and a whole batch went out that way.
# Deliberately NOT in REQUIRED_KEYS: a collection without the events export is
# not incomplete, it is just the way every collection was before the file
# existed. Its absence is reported by QC only when it would actually have
# changed something — i.e. when a run has more images than logged triggers.

# Optional inputs warn instead of failing — neither is used to place anything.
#   DMI:         cross-check only; placement uses the corrected Qinertia export.
#   CALIBRATION: only the viewer needs it (undistort / measure / board check).
#                Writing GPS needs PTP + triggers + export, nothing else, so a
#                bad calibration must never block a production write.
#   EVENTS:      recovers the pre-collection trigger of each run. Without it
#                every run's first image simply stays unplaced, exactly as
#                before this file existed — nothing else changes.
OPTIONAL_KEYS = {KEY_DMI, KEY_CALIBRATION, KEY_EVENTS_EXPORT}

KEY_DESCRIPTIONS = {
    KEY_IMAGES: "Primary camera image folder (Images/<run>/Rear) — shown in the viewer; "
                "the batch geotags every camera folder it finds",
    KEY_GOCATOR_L: "Gocator LEFT profile CSV — laser profiles + PTP timestamps",
    KEY_GOCATOR_R: "Gocator RIGHT profile CSV — laser profiles + PTP timestamps",
    KEY_EVENT_A: "eventOutA.txt — the 0.75 m camera trigger times (SBG clock)",
    KEY_UTC_TIME: "utcTime.txt — SBG clock to UTC mapping",
    KEY_DMI: "DmiStationEx CSV — raw wheel distance (cross-check only, never used for placement)",
    KEY_NAV_EXPORT: "Qinertia ascii-output.txt — CORRECTED positions; sole source of location and along-track distance (often in a separate processed folder)",
    KEY_CALIBRATION: "Pave camera intrinsic calibration YAML (PAVE_*.yaml)",
    KEY_LEVER_ARMS: "LaserImageAlignmentLeverArms.md — the lever arms of every camera and "
                    "laser (AllCalibrations); REQUIRED, every written position has one in it",
    KEY_EVENTS_EXPORT: "Qinertia Events-output.txt — one row per camera trigger for the "
                       "WHOLE session, including the triggers that fired before ACS "
                       "started collecting (which is every run's first image)",
}


@dataclass
class RunPaths:
    """Resolved (or missing=None) input paths for one run."""

    run_id: str
    root: str
    paths: dict[str, Path | None] = field(default_factory=dict)
    # which SBG DataLogger folder this run's triggers/clock actually came from,
    # and how far its name is from run_id (0 = exact). Non-zero is reported by
    # QC: a fuzzy pairing must never be silent.
    sbg_stamp: str = ""
    sbg_skew_s: int = 0
    # every camera folder found under Images/<run>/ (name -> dir). The primary
    # camera (Rear/Pave) is also in paths[KEY_IMAGES] for the viewer; the batch
    # geotags ALL of them.
    cameras: dict[str, Path] = field(default_factory=dict)
    # Where this run's calibrations came from, so the lever-arm file is
    # read from the same place rather than from a module-level default.
    # A batch pointed at a different AllCalibrations must get that
    # folder's arms.
    calibrations_dir: str = ""

    def missing(self) -> list[str]:
        return [k for k in REQUIRED_KEYS if self.paths.get(k) is None]

    def get(self, key: str) -> Path | None:
        return self.paths.get(key)


def _stamp_epoch_s(stamp: str) -> float | None:
    """YYYYMMDD.HHMMSS -> seconds, so two stamps can be compared across a
    midnight or month boundary rather than by string distance."""
    if not RUN_STAMP_RE.match(stamp):
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%d.%H%M%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def pair_sbg_stamps(collection_stamps, sbg_stamps,
                    max_skew_s: int = MAX_SBG_STAMP_SKEW_S) -> dict:
    """Match each collection-system run stamp to its SBG DataLogger stamp,
    tolerating a small start/clock skew. Returns {run stamp: (sbg stamp, skew)}.

    Nearest-first greedy assignment, so an exact match always claims its own
    logger before any fuzzy candidate can take it, and no logger is handed to
    two runs. Anything left over stays unmatched — a run with no logger is a
    loud, checkable failure, whereas a run wearing the WRONG run's triggers
    would quietly write wrong positions into the images.
    """
    candidates = []
    for run_stamp in collection_stamps:
        t_run = _stamp_epoch_s(run_stamp)
        if t_run is None:
            continue
        for sbg_stamp in sbg_stamps:
            t_sbg = _stamp_epoch_s(sbg_stamp)
            if t_sbg is None:
                continue
            skew = t_sbg - t_run
            if abs(skew) <= max_skew_s:
                candidates.append((abs(skew), run_stamp, sbg_stamp, int(round(skew))))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    paired: dict[str, tuple[str, int]] = {}
    taken: set[str] = set()
    for _, run_stamp, sbg_stamp, skew in candidates:
        if run_stamp in paired or sbg_stamp in taken:
            continue
        paired[run_stamp] = (sbg_stamp, skew)
        taken.add(sbg_stamp)
    return paired


def _scan_stamps(root: Path) -> tuple[list[str], dict[str, Path]]:
    """(collection-system run stamps, SBG DataLogger stamp -> its folder)."""
    collection: set[str] = set()
    for sub in ("Images", "GoCatorData"):
        d = root / sub
        if d.is_dir():
            for p in d.iterdir():
                if p.is_dir() and RUN_STAMP_RE.match(p.name):
                    collection.add(p.name)
    sbg_dirs: dict[str, Path] = {}
    sbg = root / "SBGData"
    if sbg.is_dir():
        for p in sorted(sbg.rglob(f"*{SBG_LOGGER_SUFFIX}")):
            stamp = p.name[: -len(SBG_LOGGER_SUFFIX)]
            if p.is_dir() and RUN_STAMP_RE.match(stamp):
                sbg_dirs.setdefault(stamp, p)
    return sorted(collection), sbg_dirs


def _run_stamps_and_loggers(root: Path) -> tuple[list[str], dict, dict[str, Path]]:
    """Every run id in this root, plus the logger pairing behind it.

    A run is named by the COLLECTION system (Images/GoCatorData), because that
    stamp is what names the export CSV, the Ortho folders and the saved bar
    measurements — renaming a run to follow the SBG clock would orphan all of
    them. A DataLogger that pairs with none of them still becomes a run of its
    own so it stays visible in the list instead of vanishing.
    """
    collection, sbg_dirs = _scan_stamps(root)
    paired = pair_sbg_stamps(collection, sbg_dirs.keys())
    claimed = {sbg_stamp for sbg_stamp, _ in paired.values()}
    stamps = sorted(set(collection) | (set(sbg_dirs) - claimed))
    return stamps, paired, sbg_dirs


PRIMARY_CAMERA_NAMES = ("Rear", "Pave")


def discover_cameras(images_run_dir: Path) -> dict[str, Path]:
    """Every camera subfolder under Images/<run>/ that holds JPEGs.

    Cameras are discovered, not hard-coded: Rear, ROW, and anything else the
    collection system writes are all picked up for geotagging.
    """
    out: dict[str, Path] = {}
    if not images_run_dir.is_dir():
        return out
    for sub in sorted(images_run_dir.iterdir()):
        if sub.is_dir() and any(sub.glob("*.jpg")):
            out[sub.name] = sub
    return out


def primary_camera(cameras: dict[str, Path]) -> str | None:
    """The camera the viewer shows (Rear/Pave if present, else the first)."""
    for name in PRIMARY_CAMERA_NAMES:
        if name in cameras:
            return name
    return next(iter(cameras), None)


def _find_nav_export(root: Path) -> Path | None:
    """First ascii-output.txt found under SBGData/**/export/ (session-level;
    one export usually covers several runs — coverage QC decides applicability)."""
    sbg = root / "SBGData"
    if not sbg.is_dir():
        return None
    hits = sorted(sbg.rglob("export/ascii-output.txt"))
    return hits[0] if hits else None


def _find_events_export(root: Path) -> Path | None:
    """Beside the nav export, and session-level in the same way."""
    from .events import find_events_file
    return find_events_file(root)


def _daily_entries(root: Path) -> dict:
    """The Daily file's rows, or {} when there is no readable one."""
    from .daily import find_daily_file, parse_daily

    path = find_daily_file(Path(root))
    if path is None:
        return {}
    try:
        return parse_daily(path)
    except (OSError, UnicodeDecodeError):
        return {}


def excluded_stamps(root: Path) -> set[str]:
    """Run stamps the ACS Daily file marks Status X - do not process.

    Separate from daily_stamps() so the batch can say WHY a folder on disk was
    left alone. "Not in the Daily file" and "in the Daily file, marked X" are
    different facts about a folder and must not print the same sentence.
    """
    return {rid for rid, e in _daily_entries(root).items() if e.excluded}


def daily_stamps(root: Path) -> set[str] | None:
    """The run stamps the ACS Daily file registers AND does not exclude, or
    None when there is no Daily file to ask.

    The Daily file IS the record of what was collected. A stamp on disk that
    it does not list was never a section: on 20260824 a GoCatorData folder
    turned up at .101524 holding 3387 laser profiles, no images and no logger
    — a section the operator started and abandoned. Derek's rule
    (2026-08-31): if it is not in the Daily file, ignore it.

    A row marked Status X is listed but must not be processed - Derek's rule
    (2026-09-01). Those are false starts and abandoned sections: 20260821 has
    two, both about 32 m against sections of 1331 m and 462 m.

    Returns None, not an empty set, when the file is missing or unreadable - a
    collection whose Daily file has not been copied in yet must process
    normally, not have every run vanish. NOTE the consequence, which is
    deliberate: a Daily file whose every row is X leaves nothing to process,
    and that is reported as what it is rather than treated as "no Daily file"
    and used to justify processing everything.
    """
    entries = _daily_entries(root)
    if not entries:
        return None
    return {rid for rid, e in entries.items() if not e.excluded}


def discover_runs(root: Path, calibrations_dir: Path | None = None,
                  overrides: "OverrideStore | None" = None,
                  registered_only: bool = True) -> list[RunPaths]:
    """Discover all runs in a run root; apply saved user overrides for missing inputs.

    calibrations_dir=None resolves at call time via core.config: lia.ini if
    there is one, else DEFAULT_CALIBRATIONS_DIR (so it stays configurable and
    testable, and an installation with no INI is unaffected).

    registered_only=True drops stamps the ACS Daily file does not list (see
    daily_stamps). Pass False to see every stamp that is on disk.
    """
    from .calibration import LEVER_ARMS_FILENAME, find_pave_calibration

    from .config import calibrations_dir as _configured
    calibrations_dir = _configured(calibrations_dir)
    lever_arms = Path(calibrations_dir) / LEVER_ARMS_FILENAME
    lever_arms = lever_arms if lever_arms.is_file() else None
    root = Path(root)
    registered = daily_stamps(root) if registered_only else None
    nav_export = _find_nav_export(root)
    events_export = _find_events_export(root)
    calibration = find_pave_calibration(calibrations_dir)
    runs: list[RunPaths] = []
    stamps, paired, sbg_dirs = _run_stamps_and_loggers(root)
    for stamp in stamps:
        if registered is not None and stamp not in registered:
            continue
        date, time = stamp.split(".")
        p: dict[str, Path | None] = {}

        cameras = discover_cameras(root / "Images" / stamp)
        primary = primary_camera(cameras)
        p[KEY_IMAGES] = cameras[primary] if primary else None

        goc_dir = root / "GoCatorData" / stamp
        l_csv = goc_dir / f"{date}T{time}L.csv"
        r_csv = goc_dir / f"{date}T{time}R.csv"
        p[KEY_GOCATOR_L] = l_csv if l_csv.is_file() else None
        p[KEY_GOCATOR_R] = r_csv if r_csv.is_file() else None

        # the logger folder for this run — its own name where they agree, the
        # nearest one within MAX_SBG_STAMP_SKEW_S where the two clocks drifted
        sbg_stamp, sbg_skew = paired.get(
            stamp, (stamp if stamp in sbg_dirs else "", 0))
        logger = sbg_dirs.get(sbg_stamp) if sbg_stamp else None
        if logger is not None:
            ev = logger / "eventOutA.txt"
            ut = logger / "utcTime.txt"
            dmi_hits = sorted(logger.glob("DmiStationEx *.csv"))
            p[KEY_EVENT_A] = ev if ev.is_file() else None
            p[KEY_UTC_TIME] = ut if ut.is_file() else None
            p[KEY_DMI] = dmi_hits[0] if dmi_hits else None
        else:
            p[KEY_EVENT_A] = p[KEY_UTC_TIME] = p[KEY_DMI] = None

        p[KEY_NAV_EXPORT] = nav_export
        p[KEY_EVENTS_EXPORT] = events_export
        p[KEY_CALIBRATION] = calibration
        p[KEY_LEVER_ARMS] = lever_arms

        run = RunPaths(run_id=stamp, root=str(root), paths=p, cameras=cameras,
                       sbg_stamp=sbg_stamp, sbg_skew_s=sbg_skew,
                       calibrations_dir=str(calibrations_dir))
        if overrides is not None:
            overrides.apply(run)
        runs.append(run)
    return runs


class OverrideStore:
    """JSON sidecar mapping (run root, run id, key) -> user-chosen path.

    Lives in the app data dir (%LOCALAPPDATA%/LaserImageAlignment on Windows,
    ~/.local/share/LaserImageAlignment elsewhere).
    """

    def __init__(self, store_path: Path | None = None):
        if store_path is None:
            base = os.environ.get("LOCALAPPDATA")
            base_dir = Path(base) if base else Path.home() / ".local" / "share"
            store_path = base_dir / "LaserImageAlignment" / "path_overrides.json"
        self.store_path = Path(store_path)
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    @staticmethod
    def _scope(root: str, run_id: str) -> str:
        return f"{root}::{run_id}"

    def _load(self) -> None:
        try:
            self._data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def set(self, root: str, run_id: str, key: str, path: Path) -> None:
        self._data.setdefault(self._scope(root, run_id), {})[key] = str(path)
        self._save()

    def get(self, root: str, run_id: str, key: str) -> Path | None:
        raw = self._data.get(self._scope(root, run_id), {}).get(key)
        return Path(raw) if raw else None

    def apply(self, run: RunPaths) -> None:
        """Fill missing inputs from saved overrides; drop overrides that no longer exist."""
        scoped = self._data.get(self._scope(run.root, run.run_id), {})
        for key, raw in list(scoped.items()):
            path = Path(raw)
            exists = path.is_dir() if key == KEY_IMAGES else path.is_file()
            if not exists:
                continue  # keep stored but do not apply; re-validated next time
            if run.paths.get(key) is None:
                run.paths[key] = path
                # A located image folder has to join run.cameras too. Without
                # this the run has NO cameras, so pipeline.process_run never
                # builds camera_alignments, and both export_csv and the batch
                # fall back to result.alignment - which is computed with NO
                # lever arm. That silently writes the cart's position instead
                # of the image footprint's (0.928 m out on Rear, 1.69 m on
                # ROW) with no align.camera.*.arm row to say so.
                if key == KEY_IMAGES:
                    run.cameras.setdefault(path.name, path)
