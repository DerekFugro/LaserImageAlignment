"""Rename images to their SECTION DISTANCE, reversibly.

The filename convention does not change: a zero-padded distance in
millimetres, exactly as ACS writes it (000000000750.jpg). What changes is
what the number MEANS.

Old: the camera's realtime odometer — millimetres since the system armed.
     Its zero is wherever the cart woke up, and it drifts 0.3-0.7% against
     the corrected trajectory.
New: millimetres from the SECTION START, measured on the corrected (PPK)
     trajectory, with the section start taken from the ACS Daily file
     (core.daily). Same units, same width, same look — a real location.

Because the two look identical, the manifest is not a convenience, it is
load-bearing. Every rename is written FIRST to a rename_manifest.csv beside
the images — old name, new name, distance — and ImageSet.scan reads it to
recover the odometer counter that matching depends on. Delete the manifest
and the folder can no longer be matched or undone. undo_renames puts
everything back from it.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .daily import DailyEntry

MANIFEST_NAME = "rename_manifest.csv"
MANIFEST_COLUMNS = ["original_name", "new_name", "section", "distance_mm",
                    "run_id", "camera", "note"]

DEFAULT_DIGITS = 12
# Renaming inside one folder can move a file onto a name another file in the
# same folder still holds (the numbers shift by less than one image spacing).
# So every move goes via a temporary name: nothing is ever written over.
TEMP_SUFFIX = ".__renaming__"

# The section-start anchor is searched over this run's own triggers, padded
# FORWARD only. Runs sit ~60 s apart, so the pad must stay well under that or
# the search can reach into a neighbouring run's pass.
#
# Nothing pads BACKWARDS. ACS starts collecting at the section start, so a
# real closest approach is never seconds before the first trigger — but the
# minutes before a run are where the PPK solution is still settling, and it
# accumulates metres of position noise while the cart stands still (up to
# 8.05 m on 20260824). A 15 s backward pad reached straight into it, and two
# runs of that collection did anchor there. Clamping costs nothing measurable
# — the closest approach moved 272 mm and 487 mm, and the miss distance was
# unchanged at 1.17 m and 1.37 m — and it removes the chance of anchoring on
# a position the solution had not converged on.
ANCHOR_PAD_S = 15.0
# Real closest approaches on 20260824 were 0.01-0.72 m. Anything past this and
# the Daily row is not describing this run: refuse rather than name it wrong.
MAX_ANCHOR_MISS_M = 5.0

# Images whose footprint lies BEFORE the section start are real photographs of
# real ground, but they are not part of the section. They are set aside here so
# nothing downstream picks them up by accident. The name lives in core.formats
# because ImageSet has to count them too.
from .formats import BEFORE_DIR  # noqa: E402  (re-exported for callers here)


def station_name(distance_mm: float, digits: int = DEFAULT_DIGITS) -> str | None:
    """The original convention kept exactly: zero-padded millimetres.

    A footprint BEFORE the section start gets the same name with a leading
    minus — it is a real measurement in the other direction, not a failure,
    and the sign says so plainly.

    Returns None only when the value will not fit the field.
    """
    mm = int(round(distance_mm))
    if len(str(abs(mm))) > digits:
        return None
    sign = "-" if mm < 0 else ""
    return f"{sign}{abs(mm):0{digits}d}.jpg"


def section_distance_mm(al, nav, entry: DailyEntry, arm_m: float | None):
    """Distance (mm) from the section start to each image, NaN where unplaced.

    Measured along the corrected trajectory, from the nav point nearest the
    Daily file's section-start coordinate to the image's footprint. Always
    counts UP with travel, whichever way the LRS chainage runs — the number
    is "how far into the section this picture is", which is what the old
    filename meant too.

    Picking the anchor is the delicate part, and the trap is not the one it
    looks like. The nav export covers the whole session, and the cart drives
    the same ground on every run, so a session-wide nearest-point search can
    anchor on a DIFFERENT run's pass — measured at 570 m out. But hunting for
    "local minima of the miss distance" is worse: while the cart sits parked
    at the session start, GPS jitter makes every other sample a local minimum,
    and on 20260824 the parking spot happened to sit 11-20 m from a section
    start. That produced ~11,000 candidates per run, and which one won shifted
    with the run's first image time — every filename in the run moved 390 mm
    when one image was added at the front.

    What is actually true: ACS starts collecting at the section start, so the
    closest approach lies inside this run's own stretch. Search only that and
    take the single global minimum. It is unique and on 20260824 it lands
    0.01-0.72 m from the Daily coordinate. Anything worse than
    MAX_ANCHOR_MISS_M means the Daily row does not describe this run, and
    everything is returned unplaced (NaN) rather than wrong.

    The window comes from the run's TRIGGERS, not from the images on disk.
    That matters: images before the section start are moved out to
    BeforeCollection afterwards, and a window built from the surviving images
    would then start later, move the anchor, shift every distance, and push
    yet more images out on the next run — a ratchet that moved 3 more images
    on the second pass over 20260824. Triggers do not change when files do.
    """
    span = getattr(getattr(al, "triggers", None), "utc_s", None)
    if span is None or not np.any(np.isfinite(span)):
        span = al.utc_s
    finite_t = np.asarray(span, dtype=float)
    finite_t = finite_t[np.isfinite(finite_t)]
    if finite_t.size == 0:
        return np.full(len(np.atleast_1d(al.utc_s)), np.nan)
    d_lat = (nav.lat_deg - entry.lat_beg) * 111320.0
    d_lon = (nav.lon_deg - entry.lon_beg) * 111320.0 * np.cos(np.radians(entry.lat_beg))
    miss_m = np.hypot(d_lat, d_lon)
    in_run = ((nav.epoch_s >= finite_t.min())          # never before the run
              & (nav.epoch_s <= finite_t.max() + ANCHOR_PAD_S))
    if not np.any(in_run):
        return np.full(len(np.atleast_1d(al.utc_s)), np.nan)
    k = int(np.argmin(np.where(in_run, miss_m, np.inf)))
    if miss_m[k] > MAX_ANCHOR_MISS_M:
        return np.full(len(np.atleast_1d(al.utc_s)), np.nan)
    s_start = float(nav.along_m[k])
    s_img = nav.dist_at(al.utc_s) - (float(arm_m) if arm_m else 0.0)
    out = (s_img - s_start) * 1000.0
    out[~np.isfinite(al.utc_s)] = np.nan
    return out


def recover_interrupted(folder: Path) -> tuple[list, list]:
    """Put back any image left mid-rename. Returns (recovered, problems).

    The rename moves every file to `name + TEMP_SUFFIX` first and only then
    onto its new name, so that a file can be given a name another file still
    holds. Kill the batch between those two passes and the temp names are
    what survives.

    That is quiet damage, and it does not heal. A temp file does not match
    `*.jpg`, so the next run scans FEWER images than the run has triggers,
    the matcher refuses the whole run as inconsistent, and it stays refused
    for ever. Seen on 20260824_revruns_FullProcessingRun: run .100258 Rear
    had 5 images stranded this way, so 38 were found where 43 exist, and the
    run was flagged with nothing on disk to explain why.

    Restoring the ORIGINAL name is right whichever pass was interrupted:
    the next batch rebuilds the whole plan from scratch anyway. A temp file
    whose original name is somehow occupied is left alone and reported —
    that is a state this code cannot reason about, and guessing could
    overwrite a real image.
    """
    folder = Path(folder)
    recovered: list[str] = []
    problems: list[str] = []
    for p in sorted(folder.glob(f"*{TEMP_SUFFIX}")):
        target = folder / p.name[: -len(TEMP_SUFFIX)]
        if target.exists():
            problems.append(f"{p.name}: {target.name} is already taken — left as is")
            continue
        try:
            os.rename(p, target)
            recovered.append(target.name)
        except OSError as exc:
            problems.append(f"{p.name}: could not restore ({exc})")
    return recovered, problems


def true_origins(folder: Path) -> dict:
    """Current on-disk name -> the TRUE original odometer name.

    A folder can be renamed more than once (a rerun with better positions
    moves every image again). The manifest is the only record of where a file
    came from, so it must always name the ORIGINAL ACS file, never the
    intermediate name from the previous pass. Reading the whole history and
    following each chain is what makes that possible — and getting it wrong
    once already cost a folder its scan: a signed intermediate name is not
    all-digits, so ImageSet.scan could not recover the counter and dropped the
    image entirely.
    """
    man = Path(folder) / MANIFEST_NAME
    if not man.is_file():
        return {}
    origin: dict[str, str] = {}
    with open(man, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            src, dst = (r.get("original_name") or ""), (r.get("new_name") or "")
            if not dst:
                continue
            origin[dst] = origin.pop(src, src)
    return origin


@dataclass
class RenameOutcome:
    run_id: str
    camera: str
    section: str = ""             # the LRS section these distances are into
    renamed: int = 0
    skipped: int = 0
    manifest_path: str = ""
    errors: list = field(default_factory=list)
    # Parallel to the `files` list handed in: what each image is called NOW,
    # and the section distance behind that name. This is the only place that
    # knows — a caller recomputing the name would disagree with the folder
    # whenever a collision or a NaN distance made a file stay put. The
    # alignment CSV is written from these, so what it names is what is on disk.
    final_names: list = field(default_factory=list)
    distances_mm: list = field(default_factory=list)

    def set_aside(self) -> list:
        """Which of those names mean 'before the section start' — the ones
        move_before_collection puts in BeforeCollection/. Same rule it uses:
        the leading minus, not a recomputation."""
        return [str(n).startswith("-") for n in self.final_names]


def rename_camera_folder(folder: Path, files: list[str], distance_mm,
                         entry: DailyEntry, run_id: str, camera: str,
                         dry_run: bool = False) -> RenameOutcome:
    """Plan every move, write the manifest, then move via temporary names.

    Idempotent: a file already sitting on its correct name is a no-op, so a
    second batch over a renamed folder changes nothing.
    """
    folder = Path(folder)
    out = RenameOutcome(run_id=run_id, camera=camera, section=str(entry.section))
    origin = true_origins(folder)
    rows: list[tuple] = []
    plan: list[tuple[str, str]] = []
    # what each input file ends up called; starts as "unchanged" so that every
    # path out of the loop below - and every collision undone further down -
    # leaves a truthful entry without needing its own bookkeeping
    out.final_names = list(files)
    out.distances_mm = [float(mm) for mm in distance_mm]
    index_of = {name: i for i, name in enumerate(files)}
    # A file this pass leaves alone still has to be recorded under the name
    # it carries NOW. Writing a blank new_name for it is only right on the
    # first pass, when its name IS the original. On a later pass the file is
    # already sitting on a section distance, and a blank there erased the
    # link: true_origins() skips blank rows, so ImageSet.scan read the
    # distance as an odometer counter and undo_renames could not find the
    # file at all (found 2026-09-02: two images placed in pass 1, unplaced
    # in pass 2, and the folder came out of it unmatchable).
    current_of = {origin.get(n, n): n for n in files}

    def held_as(original: str) -> str:
        cur = current_of.get(original, "")
        return cur if cur != original else ""

    for name, mm in zip(files, distance_mm):
        original = origin.get(name, name)
        stem = Path(original).stem.lstrip("-")
        digits = len(stem) if stem.isdigit() else DEFAULT_DIGITS
        if not np.isfinite(mm):
            rows.append((original, held_as(original), entry.section, "",
                         run_id, camera, "no position — not renamed"))
            out.skipped += 1
            continue
        new = station_name(mm, digits)
        if new is None:
            rows.append((original, held_as(original), entry.section,
                         int(round(mm)), run_id, camera,
                         "distance too long for the name — not renamed"))
            out.skipped += 1
            continue
        if new == name:
            rows.append((original, new, entry.section, int(round(mm)), run_id,
                         camera, ""))
            out.renamed += 1              # already on its section distance
            continue
        out.final_names[index_of[name]] = new
        plan.append((name, new))
        rows.append((original, new, entry.section, int(round(mm)), run_id,
                     camera, ""))

    # A target is safe if nothing holds it, or the file holding it is itself
    # moving away in this same plan (the temp-name pass makes that work).
    moving = {src for src, _ in plan}
    taken: set[str] = set()
    safe: list[tuple[str, str]] = []
    for src, dst in plan:
        if dst in taken:
            out.errors.append(f"{src}: two images both want {dst}")
            out.skipped += 1
            out.final_names[index_of[src]] = src        # it stays where it is
        elif (folder / dst).exists() and dst not in moving:
            out.errors.append(f"{src}: {dst} is already taken by another file")
            out.skipped += 1
            out.final_names[index_of[src]] = src
        else:
            taken.add(dst)
            safe.append((src, dst))
    dropped = {origin.get(s, s) for s, _ in plan} - {origin.get(s, s) for s, _ in safe}
    rows = [r if r[0] not in dropped else
            (r[0], held_as(r[0]), r[2], r[3], r[4], r[5],
             "name collision — not renamed")
            for r in rows]

    manifest = folder / MANIFEST_NAME
    out.manifest_path = str(manifest)
    if dry_run:
        out.renamed += len(safe)
        return out
    # The manifest is REWRITTEN, not appended: it states where every file
    # stands now, keyed by its true original. Appending would leave the
    # previous pass's rows contradicting this one.
    #
    # But a rewrite must not forget files this pass could not see. Images
    # already set aside in BeforeCollection are not in `files`, so without
    # this their rows would vanish and undo_renames could never bring them
    # home — 64 images were stranded that way on 20260824.
    rows.extend(_rows_for_files_not_seen(manifest, {r[0] for r in rows}, folder))
    if rows:
        with open(manifest, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(MANIFEST_COLUMNS)
            w.writerows(rows)
    for src, _ in safe:
        os.rename(folder / src, folder / (src + TEMP_SUFFIX))
    for src, dst in safe:
        # Phase 2 must never raise: a target that appeared anyway leaves this
        # file on its old name and an error in the report, not a half-renamed
        # folder with a stray temp file in it.
        if (folder / dst).exists():
            os.rename(folder / (src + TEMP_SUFFIX), folder / src)
            out.errors.append(f"{src}: {dst} appeared during the rename")
            out.skipped += 1
            out.final_names[index_of[src]] = src        # back on its old name
            continue
        os.rename(folder / (src + TEMP_SUFFIX), folder / dst)
        out.renamed += 1
    return out


def _rows_for_files_not_seen(manifest: Path, handled: set, folder: Path) -> list:
    """Manifest rows this pass did not touch but whose file is still around.

    That means the ones sitting in BeforeCollection: they belong to the run,
    they are just not in the camera folder any more, so nothing rescans them.
    Their lineage has to survive the rewrite.
    """
    if not manifest.is_file():
        return []
    keep = []
    with open(manifest, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            new = r.get("new_name") or ""
            if not new or r.get("original_name") in handled:
                continue
            if (folder / new).is_file() or (folder / BEFORE_DIR / new).is_file():
                keep.append(tuple(r.get(c, "") for c in MANIFEST_COLUMNS))
    return keep


def move_before_collection(folder: Path, dry_run: bool = False) -> tuple[int, list]:
    """Move every image named with a negative distance into BeforeCollection/.

    A negative name means the footprint sits before the section start: the
    camera was already firing while the cart rolled up to the section, so the
    picture is of ground that is not in it. Keeping those in the camera folder
    invites someone to stitch or measure them by mistake.

    Driven by the NAME, not by a recomputation — the name is what the rename
    step already decided, and re-deriving it here could disagree. Reads the
    manifest only to leave a note beside each moved row, so the record still
    says where every ACS file went and undo_renames can find it.
    """
    folder = Path(folder)
    doomed = sorted(p for p in folder.glob("-*.jpg"))
    if not doomed:
        return 0, []
    problems: list[str] = []
    dest = folder / BEFORE_DIR
    moved = []
    for p in doomed:
        target = dest / p.name
        if target.exists():
            problems.append(f"{p.name}: already in {BEFORE_DIR}")
            continue
        moved.append(p)
    if dry_run:
        return len(moved), problems
    if moved:
        dest.mkdir(exist_ok=True)
    for p in moved:
        os.rename(p, dest / p.name)
    # the manifest keeps pointing at these files, so say where they went
    man = folder / MANIFEST_NAME
    if man.is_file() and moved:
        names = {p.name for p in moved}
        with open(man, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            if r["new_name"] in names:
                r["note"] = f"moved to {BEFORE_DIR}/ (before the section start)"
        with open(man, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
            w.writeheader()
            w.writerows(rows)
    return len(moved), problems


def undo_renames(folder: Path) -> tuple[int, list]:
    """Put every renamed file back, driven purely by the manifest.
    The manifest itself is kept (renamed to .undone) as the record."""
    folder = Path(folder)
    manifest = folder / MANIFEST_NAME
    if not manifest.exists():
        return 0, ["no rename_manifest.csv here — nothing to undo"]
    problems: list[str] = []
    with open(manifest, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    pairs = [(r["new_name"], r["original_name"]) for r in rows if r["new_name"]]
    # an image set aside as before-the-section still belongs to this folder;
    # undo has to reach into BeforeCollection/ or it would report it missing
    # and silently leave it stranded under a name nothing else understands.
    before = folder / BEFORE_DIR
    for n, o in pairs:
        stray = before / n
        if stray.is_file() and not (folder / n).exists():
            os.rename(stray, folder / n)
    if before.is_dir() and not any(before.iterdir()):
        before.rmdir()
    live = [(n, o) for n, o in pairs if (folder / n).exists()]
    for n, o in pairs:
        if not (folder / n).exists() and not (folder / o).exists():
            problems.append(f"{n}: missing, and {o} missing too")
    # same two-phase move as the rename, for the same reason
    for n, _ in live:
        os.rename(folder / n, folder / (n + TEMP_SUFFIX))
    undone = 0
    for n, o in live:
        if (folder / o).exists():
            problems.append(f"{o}: already exists — left {n} in place")
            os.rename(folder / (n + TEMP_SUFFIX), folder / n)
            continue
        os.rename(folder / (n + TEMP_SUFFIX), folder / o)
        undone += 1
    os.replace(manifest, folder / (MANIFEST_NAME + ".undone"))
    return undone, problems
