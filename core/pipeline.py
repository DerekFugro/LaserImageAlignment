"""Run-level orchestration: QC -> solvers -> alignment -> export.

This is the single entry point the GUI (and tests) use per run.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import discovery as disc
from .alignment import (
    ImageAlignment, MatchResult, PtpOffsetResult, TriggerData,
    align_images, build_triggers, gocator_distances, match_images_to_triggers,
    solve_ptp_offset,
)
from .qc import (TARGET_CSV, TARGET_IMAGES, ParsedRun, QCReport, Severity,
                 check_content, check_presence, parse_all)

# How far the trigger cadence may drift from the postprocessed distance before
# it is worth reporting. This is realtime INS vs postprocessed INS, and the
# gap is SYSTEMATIC, not per-run: on 20260824 the export read short on ten of
# eleven runs, a = 0.9932 to 1.0000, with fit residuals of 4-38 mm, so each
# run's `a` is a real, tight scale rather than noise. A limit inside that
# spread just reports the fleet's normal behaviour twice a day. 1.0% sits
# above the whole spread and in line with the other rulers: the export, the
# SBG realtime distance and the Gocator wheel encoder all agree within ~1%.
# Derek set it there on 2026-08-31. Nothing downstream uses the counter --
# images are placed by trigger time into the export -- so this is diagnostic.
COUNTER_SCALE_MAX_DEV = 0.010


@dataclass
class RunResult:
    """Everything computed for one run."""

    run: disc.RunPaths
    report: QCReport
    parsed: ParsedRun
    triggers: TriggerData | None = None
    ptp_l: PtpOffsetResult | None = None
    ptp_r: PtpOffsetResult | None = None
    match: MatchResult | None = None
    alignment: ImageAlignment | None = None            # primary camera
    camera_alignments: dict = field(default_factory=dict)  # name -> ImageAlignment
    camera_matches: dict = field(default_factory=dict)     # name -> MatchResult
    gocator_l_dist_m: np.ndarray | None = None
    gocator_r_dist_m: np.ndarray | None = None

    @property
    def export_blocked(self) -> bool:
        return self.report.export_blocked


def _best_ptp(*candidates: PtpOffsetResult | None) -> PtpOffsetResult | None:
    """The best available PTP solution: a PASSing one if there is one, else the
    first available (so its numbers still reach the UI), else None."""
    available = [p for p in candidates if p is not None]
    if not available:
        return None
    return next((p for p in available if p.passed), available[0])


def process_run(run: disc.RunPaths) -> RunResult:
    """Full QC + alignment pass for one run. Never raises on data problems —
    they land in the QC report; only programming errors propagate."""
    report = QCReport(run_id=run.run_id)
    check_presence(run, report)
    parsed = parse_all(run, report)
    result = RunResult(run=run, report=report, parsed=parsed)

    # RULER = the corrected Qinertia export (parsed.nav). The raw DMI wheel is
    # never used for placement or solving — cross-check only (see qc.py).
    ruler = parsed.nav
    if parsed.triggers_sbg_us is not None and parsed.utc is not None and ruler is not None:
        trig_us = _recover_pre_collection_triggers(parsed, report)
        result.triggers = build_triggers(trig_us, parsed.utc, ruler)
    if parsed.gocator_l is not None and ruler is not None:
        result.ptp_l = solve_ptp_offset(parsed.gocator_l, ruler)
    if parsed.gocator_r is not None and ruler is not None:
        result.ptp_r = solve_ptp_offset(parsed.gocator_r, ruler)
    if parsed.images is not None and result.triggers is not None:
        result.match = match_images_to_triggers(parsed.images, result.triggers)
        report.add(
            "align.match", "Image-trigger match",
            Severity.PASS if result.match.n_matched else Severity.FAIL,
            result.match.message,
            shift_k=result.match.shift_k, d0_m=result.match.d0_m,
            n_matched=result.match.n_matched, n_unmatched=result.match.n_unmatched,
        )
        a = result.match.scale_a
        if np.isfinite(a):
            dev = abs(a - 1.0)
            # WHAT THIS ACTUALLY COMPARES (Derek, 2026-08-20 — see Spec
            # Amendment F): the cameras are fired by the SBG sync output in
            # VIRTUAL ODOMETER mode every 0.750 m, i.e. by the SBG's realtime
            # nav solution, not by the wheel. Image "distance" is just
            # ordinal x 0.75, so this is realtime-INS cadence vs
            # postprocessed-INS distance. It is a convergence signal, NOT an
            # independent check, and it says nothing about wheel calibration.
            report.add(
                "align.counter_scale", "Trigger cadence vs postprocessed distance",
                Severity.PASS if dev <= COUNTER_SCALE_MAX_DEV else Severity.WARN,
                f"scale a = {a:.5f} ({dev * 100:.2f}% from 1.0, limit "
                f"{COUNTER_SCALE_MAX_DEV * 100:.1f}%). Triggers fire every "
                "0.750 m of SBG realtime (virtual-odometer) travel; this compares that "
                "cadence with the postprocessed export, so a gap means the realtime "
                "solution disagreed with the final one — not a wheel problem",
                scale_a=a,
            )

    check_content(parsed, report, triggers=result.triggers,
                  ptp_l=result.ptp_l, ptp_r=result.ptp_r)

    # final alignment. Prefer a solver result that PASSED — a failed L offset
    # must not be used just because L was parsed first.
    ptp = _best_ptp(result.ptp_l, result.ptp_r)
    # every discovered camera gets its own match + alignment (same triggers,
    # matched independently so a camera with a different image count is safe)
    if result.triggers is not None and ptp is not None and parsed.cameras:
        for cam_name, cam_images in parsed.cameras.items():
            cam_match = match_images_to_triggers(cam_images, result.triggers)
            result.camera_matches[cam_name] = cam_match
            # The whole (X, Y, Z) from the lever-arm file - the one parsed
            # for THIS run (parsed.lever_arms, from run.paths), not a
            # module-level default - so every number in that file is in
            # force and a Locate...d file is honoured.
            arm_xyz = (parsed.lever_arms or {}).get(cam_name)
            arm = None if arm_xyz is None else -float(arm_xyz[0])
            result.camera_alignments[cam_name] = align_images(
                cam_images, result.triggers, cam_match, ptp, parsed.nav,
                arm_xyz=arm_xyz)
            report.add(
                f"align.camera.{cam_name}", f"Camera '{cam_name}' match",
                Severity.PASS if cam_match.n_matched else Severity.FAIL,
                cam_match.message, n_matched=cam_match.n_matched,
                n_unmatched=cam_match.n_unmatched, shift_k=cam_match.shift_k)
            # A camera with no arm in the file is NOT written (Derek,
            # 2026-09-02). Its positions would be the cart's, not the
            # footprint's, and a confidently wrong position on a client
            # deliverable is worse than none. FAIL, scoped to the images and
            # the table - the lasers have their own arms and their own row.
            report.add(
                f"align.camera.{cam_name}.arm", f"Camera '{cam_name}' lever arm",
                Severity.PASS if arm_xyz is not None else Severity.FAIL,
                (f"position = image footprint centre (arm X {arm_xyz[0]:+.3f}, "
                 f"Y {arm_xyz[1]:+.3f}, Z {arm_xyz[2]:+.3f} m from the file)"
                 if arm_xyz is not None else
                 f"no '{cam_name} camera' section in the lever-arm file - this "
                 "camera's images and the table are held until it has one"),
                blocks=(TARGET_IMAGES, TARGET_CSV), arm_m=arm)

        # ONE position per image. `result.alignment` is what the viewer reads;
        # camera_alignments[cam] is what the EXIF, the Gocator CSVs and the
        # export table are written from. Until 2026-09-01 the first of those
        # was a SECOND alignment computed a few lines above with no lever arm,
        # so the readout's lat/lon sat 0.928 m behind the value stamped into
        # the same photograph, along the heading. Same trigger, same instant,
        # same trajectory row - only the last step differed, and one number
        # per image is the only way to keep it that way (Derek, 2026-09-01).
        #
        # The primary camera IS pr.images (parse_all reuses the object rather
        # than scanning the folder twice), so identity finds it.
        for cam_name, cam_images in parsed.cameras.items():
            if cam_images is parsed.images:
                result.alignment = result.camera_alignments.get(cam_name)
                result.match = result.camera_matches.get(cam_name, result.match)
                break
        else:
            # No camera folder is the viewer's folder. Nothing discovers a run
            # that way, and the Locate... path registers its folder as a camera
            # too, so this is unreachable in practice - but if it ever happens,
            # showing NOTHING beats showing an unarmed position that looks
            # exactly like an armed one.
            result.alignment = None

    if parsed.gocator_l is not None and result.ptp_l is not None and ruler is not None:
        result.gocator_l_dist_m = gocator_distances(parsed.gocator_l, result.ptp_l, ruler)
    if parsed.gocator_r is not None and result.ptp_r is not None and ruler is not None:
        result.gocator_r_dist_m = gocator_distances(parsed.gocator_r, result.ptp_r, ruler)
    return result


def _recover_pre_collection_triggers(parsed, report) -> np.ndarray:
    """Put back the triggers that fired before ACS started logging.

    The SBG fires the camera every 0.750 m whether ACS is collecting or not,
    but the per-run DataLogger folder only exists while ACS runs — so its
    eventOutA.txt starts one trigger late and the run's first image can never
    be placed from it. Qinertia's whole-session event export has those rows.

    Returns the trigger list to use. Unchanged whenever the events file is
    absent, does not line up, or the run is not short of triggers — and the QC
    report always says which of those happened.
    """
    from .events import leading_trigger_utc_s, utc_to_sbg_us

    def full_count(imageset) -> int:
        """Every image the run produced, including any already filed away.

        Images before the section start are moved to BeforeCollection after
        renaming, so a plain scan under-counts on the NEXT run — which would
        change how many triggers get recovered, move the anchor, and rename
        the whole run again. Counting the set-aside ones keeps the run's shape
        the same however many times the batch is re-run.
        """
        return len(imageset) + imageset.n_set_aside

    logged = np.asarray(parsed.triggers_sbg_us, dtype=np.int64)
    counts = [full_count(im) for im in (parsed.cameras or {}).values()]
    if parsed.images is not None:
        counts.append(full_count(parsed.images))
    n_missing = (max(counts) - len(logged)) if counts else 0
    if n_missing <= 0:
        return logged
    if parsed.events is None:
        report.add(
            "align.pre_collection_triggers", "Pre-collection triggers", Severity.WARN,
            f"{n_missing} image(s) per camera have no logged trigger — they fired "
            "before ACS started. Add Qinertia's Events-output.txt to the export "
            "folder to place them; without it they stay unplaced",
            n_missing=n_missing)
        return logged
    logged_utc = parsed.utc.sbg_to_utc(logged)
    extra_utc, note = leading_trigger_utc_s(parsed.events, logged_utc, n_missing)
    if len(extra_utc) == 0:
        report.add("align.pre_collection_triggers", "Pre-collection triggers",
                   Severity.WARN, note, n_missing=n_missing)
        return logged
    extra_us = utc_to_sbg_us(parsed.utc, extra_utc).astype(np.int64)
    report.add("align.pre_collection_triggers", "Pre-collection triggers",
               Severity.PASS, note, n_recovered=int(len(extra_us)))
    return np.concatenate([extra_us, logged])


EXPORT_COLUMNS = [
    # `image_file` is the name the run was PROCESSED under; `image_file_final`
    # is what the file is called on disk once the batch has renamed it to its
    # section distance. They differ on every run, because the rename is the
    # last step of the batch and needs the positions computed here first.
    # Without the second column this table names files that do not exist:
    # on 20260824.100157 all 88 rows pointed at gone names.
    "image_file", "image_file_final", "set_aside",
    "camera", "run_id", "image_ordinal", "trigger_index", "matched",
    "utc_iso", "ptp_us", "dist_along_track_m", "section", "section_distance_mm",
    "latitude_deg", "longitude_deg",
    "altitude_msl_m", "match_residual_m", "ptp_offset_s", "ptp_offset_r", "notes",
]


def export_csv(result: RunResult, out_path: Path, rename_info: dict | None = None) -> Path:
    """Write the image->location table for EVERY camera (one row per image,
    with a `camera` column). Refuses when QC has FAILs.

    `rename_info` is {camera: RenameOutcome} from the batch's rename step, so
    the table can name the file as it is NOW as well as the name the run was
    processed under. Omit it and `image_file_final` simply repeats
    `image_file` — which is the truth for a folder nothing has just renamed,
    such as a single run exported from the viewer.

    `set_aside` marks the images the batch moved into BeforeCollection/ —
    their footprint is before the section start. They keep their row here,
    with their position, because they are still this run's photographs; the
    flag is what tells a downstream process to look in that subfolder and,
    usually, to leave them alone.
    """
    if result.export_blocked:
        failed = ", ".join(c.check_id for c in result.report.failed)
        raise RuntimeError(f"export blocked by failed QC checks: {failed}")
    alignments = dict(result.camera_alignments)
    if not alignments and result.alignment is not None:
        alignments = {"": result.alignment}
    if not alignments:
        raise RuntimeError("nothing to export: alignment did not run")
    notes = result.report.notes()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def num(x, fmt="{:.9f}"):
        return fmt.format(x) if np.isfinite(x) else ""

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for cam_name, al in sorted(alignments.items()):
            ren = (rename_info or {}).get(cam_name)
            finals = list(getattr(ren, "final_names", []) or [])
            dists = list(getattr(ren, "distances_mm", []) or [])
            aside = ren.set_aside() if ren is not None else []
            section = getattr(ren, "section", "") or ""
            for j in range(len(al.images)):
                i = int(al.match.trigger_for_image[j])
                matched = i >= 0
                utc_iso = ""
                if matched and np.isfinite(al.utc_s[j]):
                    utc_iso = datetime.fromtimestamp(
                        al.utc_s[j], tz=timezone.utc).isoformat(timespec="microseconds")
                row_notes = notes if matched else \
                    (notes + "; " if notes else "") + "UNMATCHED (no trigger)"
                # the rename lists run parallel to al.images; a short one means
                # this camera was not renamed, so the name on disk is the one
                # the run was processed under
                final = finals[j] if j < len(finals) else al.images.files[j]
                sec_mm = dists[j] if j < len(dists) else float("nan")
                w.writerow([
                    al.images.files[j], final,
                    (aside[j] if j < len(aside) else False),
                    cam_name, result.run.run_id, j,
                    i if matched else "", matched, utc_iso,
                    int(al.ptp_us[j]) if matched else "",
                    num(al.dmi_dist_m[j], "{:.3f}"),
                    section, num(sec_mm, "{:.0f}"),
                    num(al.lat_deg[j]),
                    num(al.lon_deg[j]), num(al.alt_m[j], "{:.3f}"),
                    num(al.match.residual_m[j], "{:.3f}"),
                    f"{al.ptp.offset_s:.2f}", f"{al.ptp.r:.4f}", row_notes,
                ])
    return out_path
