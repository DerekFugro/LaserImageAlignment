"""Run-level orchestration: QC -> solvers -> alignment -> export.

This is the single entry point the GUI (and tests) use per run.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import calibration as cal
from . import discovery as disc
from .atomic import atomic_open
from .alignment import (
    TRIGGER_SPACING_M, ImageAlignment, MatchResult, PtpOffsetResult, TriggerData,
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
    # Missing images mode: per camera, the (first, last) photo ordinals whose
    # pulse was worked out rather than proven (see _pairing)
    estimated_spans: dict = field(default_factory=dict)
    # Missing images mode: per camera, {photo index: index of the photo it
    # repeats} for photos of the same spot (see _duplicates)
    duplicates: dict = field(default_factory=dict)

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
        trig_us, n_pre = _recover_pre_collection_triggers(parsed, report)
        result.triggers = build_triggers(trig_us, parsed.utc, ruler)
        result.triggers.n_prepended = n_pre
    if parsed.gocator_l is not None and ruler is not None:
        result.ptp_l = solve_ptp_offset(parsed.gocator_l, ruler)
    if parsed.gocator_r is not None and ruler is not None:
        result.ptp_r = solve_ptp_offset(parsed.gocator_r, ruler)
    # which trigger took photo n, per camera (camera log first - see
    # core/cameralog.py). photo_lag = -k for photo n <- trigger n+k.
    lags: dict = {}
    usable = None
    if result.triggers is not None:
        usable = _camera_usable_triggers(parsed, result.triggers, report)
        lags, usable_by_cam, result.estimated_spans = _pairing(
            run, parsed, result.triggers, report, usable)
    else:
        usable_by_cam = {}
    if parsed.images is not None and result.triggers is not None:
        primary = _camera_name_of(parsed, parsed.images)
        result.match = match_images_to_triggers(
            parsed.images, result.triggers,
            photo_lag=lags.get(primary, _photo_lag(parsed, primary)),
            usable=usable_by_cam.get(primary, _usable_for(parsed, primary, usable)))
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
    # Photos are placed by trigger time and need no laser. When neither
    # laser file holds a single profile (20261016.172652: both "no profile
    # rows") there is no PTP result at all, and until 2026-10-23 that stopped
    # the alignment - 92 good photos got no GPS while a run with 1 and 3
    # profiles (20261015.165518) had its photos written. Derek: photos alone
    # are a deliverable; process what is good and report the rest. The PTP
    # columns of the alignment table are then the nominal offset, unverified.
    if ptp is None and result.triggers is not None and parsed.cameras:
        from .formats import TAI_UTC_NOMINAL_S
        ptp = PtpOffsetResult(
            offset_s=TAI_UTC_NOMINAL_S, r=0.0, passed=False, n_used=0,
            sweep_offsets=np.empty(0), sweep_r=np.empty(0),
            message="no laser data - photos placed by trigger time; PTP columns "
                    "nominal, not verified")
    # every discovered camera gets its own match + alignment (same triggers,
    # matched independently so a camera with a different image count is safe)
    if result.triggers is not None and ptp is not None and parsed.cameras:
        for cam_name, cam_images in parsed.cameras.items():
            delay = cal.trigger_delay_s(parsed.lever_arms, cam_name)
            cam_match = match_images_to_triggers(
                cam_images, result.triggers,
                photo_lag=lags.get(cam_name, _photo_lag(parsed, cam_name)),
                usable=usable_by_cam.get(cam_name, _usable_for(parsed, cam_name, usable)))
            result.camera_matches[cam_name] = cam_match
            # The whole (X, Y, Z) from the lever-arm file - the one parsed
            # for THIS run (parsed.lever_arms, from run.paths), not a
            # module-level default - so every number in that file is in
            # force and a Locate...d file is honoured.
            arm_xyz = (parsed.lever_arms or {}).get(cam_name)
            arm = None if arm_xyz is None else -float(arm_xyz[0])
            view = cal.view_distance_m(parsed.lever_arms, cam_name)
            result.camera_alignments[cam_name] = align_images(
                cam_images, result.triggers, cam_match, ptp, parsed.nav,
                arm_xyz=arm_xyz, delay_s=delay or 0.0, view_m=view or 0.0)
            report.add(
                f"align.camera.{cam_name}.delay", f"Camera '{cam_name}' trigger delay",
                Severity.PASS,
                (f"picture taken {delay * 1000:.0f} ms after its trigger (lever-arm "
                 f"file); pairing: see align.camera.{cam_name}.pairing"
                 if delay is not None else
                 "no trigger delay in the lever-arm file - photo n paired with "
                 "trigger n, placed at the trigger time (not verified for this camera)"),
                delay_s=delay)
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
                ((f"position = image footprint centre (arm X {arm_xyz[0]:+.3f}, "
                  f"Y {arm_xyz[1]:+.3f}, Z {arm_xyz[2]:+.3f} m from the file)"
                  if not view else
                  f"position = the ground the picture shows, {view:.2f} m ahead of "
                  f"the camera (view distance) - camera arm X {arm_xyz[0]:+.3f}, "
                  f"Y {arm_xyz[1]:+.3f}, Z {arm_xyz[2]:+.3f} m; the camera's own "
                  "position is in the table's camera_latitude/longitude columns")
                 if arm_xyz is not None else
                 f"no '{cam_name} camera' section in the lever-arm file - this "
                 "camera's images and the table are held until it has one"),
                blocks=(TARGET_IMAGES, TARGET_CSV), arm_m=arm)

        # Missing images mode: photos taken while the van stood still
        if parsed.triggers_dropped or any(c.check_id.endswith(".missing_images")
                                          for c in report.checks):
            # The picture check needs a camera looking DOWN: ROW looks ahead,
            # and its far scenery barely moves even when the van does (15-94 px
            # a step on 093228). So Rear decides, and every other camera takes
            # the same verdict for the same pulse (Derek: ROW's go too).
            als = result.camera_alignments
            judge = "Rear" if "Rear" in als else next(iter(als), None)
            verdict = _duplicates(als[judge]) if judge else {}
            by_pulse = {int(als[judge].match.trigger_for_image[j]):
                        int(als[judge].match.trigger_for_image[k])
                        for j, k in verdict.items()} if judge else {}
            for cam_name, al in als.items():
                dups = verdict if cam_name == judge else \
                    _duplicates_by_pulse(al, by_pulse)
                if dups:
                    result.duplicates[cam_name] = dups
                    report.add(
                        f"align.camera.{cam_name}.duplicates",
                        f"Camera '{cam_name}' duplicate photos", Severity.WARN,
                        f"{len(dups)} photo(s) of the same spot as the photo before them "
                        "(the van standing still while the loose encoder fired pulses; "
                        f"checked on the {judge} pictures) - given that photo's GPS and "
                        "moved to Duplicates/ by the batch",
                        photos=[al.images.files[j] for j in sorted(dups)])

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


def _camera_name_of(parsed, imageset) -> str | None:
    """The camera folder name an ImageSet came from (identity), or None."""
    for name, im in (parsed.cameras or {}).items():
        if im is imageset:
            return name
    return None


def _photo_lag(parsed, cam_name) -> int:
    """1 when the lever-arm file gives this camera a trigger delay (its photo
    n is the picture taken after trigger n-1), else 0."""
    return 1 if cal.trigger_delay_s(parsed.lever_arms, cam_name) is not None else 0


def _camera_usable_triggers(parsed, triggers, report):
    """The triggers a camera with a trigger delay can photograph (see
    alignment.CAMERA_MIN_TRIGGER_GAP_S), or None when no camera has a delay.
    A report row only when a trigger is passed over - WARN, so it gets a look:
    it has happened once (20261017.154422) and the cause is in the SBG."""
    from .alignment import CAMERA_MIN_TRIGGER_GAP_S, camera_trigger_indices
    if not any(cal.trigger_delay_s(parsed.lever_arms, c) is not None
               for c in (parsed.cameras or {})):
        return None
    usable, skipped = camera_trigger_indices(triggers.utc_s)
    if skipped:
        shown = ", ".join(f"trigger {i + 1} ({gap:.3f} s after trigger {i})"
                          for i, gap in skipped[:5])
        more = f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""
        report.add(
            "align.trigger_too_soon", "Trigger too soon for the camera",
            Severity.WARN,
            f"{len(skipped)} trigger(s) came less than "
            f"{CAMERA_MIN_TRIGGER_GAP_S:.2f} s after the one before, too soon for "
            f"the camera to take a picture: {shown}{more}. They got no photo, so "
            "the photos after them are counted from the next trigger",
            skipped=[int(i) for i, _ in skipped],
            gaps_s=[round(g, 4) for _, g in skipped])
    return usable


def _usable_for(parsed, cam_name, usable):
    """`usable` for a camera with a trigger delay, else None (every trigger)."""
    if usable is None or cal.trigger_delay_s(parsed.lever_arms, cam_name) is None:
        return None
    return usable


def _pairing(run, parsed, triggers, report, usable=None):
    """photo_lag per camera that has a trigger delay, decided in Derek's order
    (2026-10-20): camera log frame times, then the camera start, then photo n
    = trigger n-1 assumed. Cameras without a delay keep the old rule (lag 0).
    One report row per camera; the run is written in every case, but anything
    not confirmed is a WARN so it gets a look.

    `usable`: the triggers the camera could photograph. The camera log and
    the photo count are judged against those, so a trigger that came too soon
    does not throw the offset or the count off.

    Returns (lags, usable per camera, estimated spans per camera). The last
    two are only filled by missing images mode (see _missing_images)."""
    from . import cameralog
    log_path = run.get(disc.KEY_CAMERA_LOG)
    lags, usable_by_cam, spans_by_cam = {}, {}, {}
    n_trg = len(triggers.utc_s)
    for cam_name, cam_images in (parsed.cameras or {}).items():
        delay = cal.trigger_delay_s(parsed.lever_arms, cam_name)
        if delay is None:
            continue
        n_photos = len(cam_images) + int(getattr(cam_images, "n_set_aside", 0) or 0)
        base = np.arange(n_trg) if usable is None else np.asarray(usable)
        trig_utc = np.asarray(triggers.utc_s)[base]
        r = cameralog.decide(log_path, run.run_id, cam_name, trig_utc,
                             delay, n_photos)
        fixed = None
        if not (r.confident and r.method == "camera log"):
            fixed = _missing_images(log_path, run.run_id, cam_name, base,
                                    np.asarray(triggers.utc_s), r, report)
        if fixed is not None:
            base, k, spans = fixed
            usable_by_cam[cam_name] = base
            spans_by_cam[cam_name] = spans
            r = cameralog.decide(log_path, run.run_id, cam_name,
                                 np.asarray(triggers.utc_s)[base], delay, n_photos)
            if int(r.k) != k:
                # the camera log must now agree on one k; if it does not,
                # the fix is not trusted and the run keeps the old pairing
                report.add(f"align.camera.{cam_name}.missing_images",
                           f"Camera '{cam_name}' missing images", Severity.WARN,
                           f"pulses with no photo were found, but the camera log "
                           f"did not agree afterwards (k {int(r.k)} vs {k}) - "
                           "pairing left as it was; check this run")
                usable_by_cam.pop(cam_name, None)
                spans_by_cam.pop(cam_name, None)
                base = np.arange(n_trg) if usable is None else np.asarray(usable)
                r = cameralog.decide(log_path, run.run_id, cam_name,
                                     np.asarray(triggers.utc_s)[base], delay, n_photos)
        lags[cam_name] = -int(r.k)
        ok = r.confident and "COUNT DOES NOT FIT" not in r.message
        report.add(
            f"align.camera.{cam_name}.pairing", f"Camera '{cam_name}' photo-trigger pairing",
            Severity.PASS if ok else Severity.WARN, r.message,
            k=int(r.k), method=r.method, confident=bool(r.confident),
            camera_log=str(log_path) if log_path else "")
    # different photo counts per camera are judged in qc.check_content
    # (content.camera_counts), which reads these rows
    return lags, usable_by_cam, spans_by_cam


def _missing_images(log_path, run_id, cam_name, base, all_utc, r, report):
    """Missing images mode (Derek, 2026-11-10): when one pairing does not fit
    the whole run, follow the camera log's every-100th-photo lines through the
    pulses, pass over the pulses that took no photo, and say which photos were
    therefore worked out rather than proven.

    Returns (usable trigger indices, k, [(first, last) photo ordinal]) or None
    when there is nothing to fix or it can't be fixed safely."""
    from . import cameralog
    if log_path is None:
        return None
    try:
        frames = cameralog.load(Path(log_path)).frames.get((run_id, cam_name), [])
    except OSError:
        return None
    if len(frames) < 2:
        return None
    trig = all_utc[base]
    seq = cameralog.follow_frames(frames, trig)
    if seq is None:
        return None
    # the pairing at the start of the run: the camera start when the log has
    # one, else the start-up-frame rule (photo n = pulse n-1) that every
    # normal run follows. It closes the stretch before the first log line.
    k_start = int(r.k) if r.method in ("camera start", "assumed") and r.k is not None else None
    ks = {k for _, k in seq}
    if len(ks) == 1 and (k_start is None or seq[0][1] == k_start):
        return None                      # one pairing fits: nothing missing
    found = cameralog.extra_pulses(seq, trig, k_start)
    if found is None:
        report.add(f"align.camera.{cam_name}.missing_images",
                   f"Camera '{cam_name}' missing images", Severity.WARN,
                   "the camera log shows the pairing changing in a way passing over "
                   "pulses can't explain (a photo with no pulse) - pairing left as it "
                   f"was; check this run. Log frames (photo, k): {seq}")
        return None
    drop, spans = found
    if not drop:
        return None
    k = k_start if k_start is not None else seq[0][1]
    new = np.delete(np.asarray(base), drop)
    if cameralog.follow_frames(frames, all_utc[new]) is None or \
            {kk for _, kk in cameralog.follow_frames(frames, all_utc[new])} != {k}:
        report.add(f"align.camera.{cam_name}.missing_images",
                   f"Camera '{cam_name}' missing images", Severity.WARN,
                   f"{len(drop)} pulse(s) look like they took no photo, but the camera "
                   "log does not fit one pairing without them - pairing left as it "
                   "was; check this run")
        return None
    where = ", ".join(f"photos {a}-{b}" for a, b in spans)
    report.add(f"align.camera.{cam_name}.missing_images",
               f"Camera '{cam_name}' missing images", Severity.WARN,
               f"{len(drop)} pulse(s) took no photo ({where}) - found from the camera "
               "log and passed over; every camera-log check point now fits one "
               "pairing. The photos in those stretches are 'estimated' in the "
               "alignment table (the exact pulse inside the stretch is the "
               "likeliest one, not proven)",
               passed_over=[int(np.asarray(base)[i]) for i in drop], spans=spans)
    return new, int(k), spans


def _recover_pre_collection_triggers(parsed, report) -> tuple[np.ndarray, int]:
    """Put back the triggers that fired before ACS started logging.

    The SBG fires the camera every 0.750 m whether ACS is collecting or not,
    but the per-run DataLogger folder only exists while ACS runs — so its
    eventOutA.txt starts one trigger late and the run's first image can never
    be placed from it. Qinertia's whole-session event export has those rows.

    Returns (the trigger list to use, how many were put in front of the logged
    ones). Unchanged whenever the events file is absent, does not line up, or
    the run is not short of triggers — and the QC report always says which of
    those happened.
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
    from .alignment import PAIRING_MODE
    if PAIRING_MODE == "head":
        # Nothing is put in front of the logged triggers. A camera with a
        # trigger delay pairs photo n with trigger n-1, and its photo 1 is the
        # frame the camera makes when acquisition STARTS - no trigger at all.
        # The camera log proves it (2026-10-10, all 7 stick runs): the camera
        # started 1.5-10 s AFTER the last trigger before logging and 0.9-9.5 s
        # before the first logged one, with no trigger in between. So photo 1
        # gets no position (Derek, 2026-10-11): it is filed in BeforeCollection
        # under a start-up name, with no GPS (batch.py / rename.py).
        lagged = [c for c in (parsed.cameras or {})
                  if cal.trigger_delay_s(parsed.lever_arms, c) is not None]
        report.add("align.pre_collection_triggers", "Pre-collection triggers",
                   Severity.PASS,
                   ("not used - photo 1 is the camera start-up frame (no trigger), "
                    f"left without GPS ({', '.join(lagged)})" if lagged else
                    "not used - pairing counts from the first logged trigger "
                    "(no extra front photo assumed)"), n_recovered=0)
        return logged, 0
    counts = [full_count(im) for im in (parsed.cameras or {}).values()]
    if parsed.images is not None:
        counts.append(full_count(parsed.images))
    n_missing = (max(counts) - len(logged)) if counts else 0
    if n_missing <= 0:
        return logged, 0
    if parsed.events is None:
        report.add(
            "align.pre_collection_triggers", "Pre-collection triggers", Severity.WARN,
            f"{n_missing} image(s) per camera have no logged trigger — they fired "
            "before ACS started. Add Qinertia's Events-output.txt to the export "
            "folder to place them; without it they stay unplaced",
            n_missing=n_missing)
        return logged, 0
    logged_utc = parsed.utc.sbg_to_utc(logged)
    extra_utc, note = leading_trigger_utc_s(parsed.events, logged_utc, n_missing)
    if len(extra_utc) == 0:
        report.add("align.pre_collection_triggers", "Pre-collection triggers",
                   Severity.WARN, note, n_missing=n_missing)
        return logged, 0
    extra_us = utc_to_sbg_us(parsed.utc, extra_utc).astype(np.int64)
    report.add("align.pre_collection_triggers", "Pre-collection triggers",
               Severity.PASS, note, n_recovered=int(len(extra_us)))
    return np.concatenate([extra_us, logged]), int(len(extra_us))


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
    # Added at the END (2026-10-30) so readers that go by position still work.
    # latitude/longitude above are what the EXIF says. For a camera with a
    # view distance (ROW) that is the ground the picture shows; these two are
    # where the camera itself was. For every other camera they repeat lat/lon.
    "view_distance_m", "camera_latitude_deg", "camera_longitude_deg",
    # Missing images mode (2026-11-10), at the end too. gap_before_m: metres of
    # ground with no photo just before this one (0 normally) - a stitcher or
    # reader should not join this photo to the one before. photo_status:
    # confirmed, or estimated (its pulse worked out, not proven - see
    # align.camera.<cam>.missing_images).
    "gap_before_m", "photo_status",
]

# a step this much longer than one trigger spacing is a hole, not jitter:
# normal steps measure 0.566-0.860 m (see content.trigger_gap) and one missing
# photo makes a 1.5 m step, so the line sits between (a step over 1.25 m)
HOLE_MIN_EXTRA_M = 0.5


# Two photos whose pulses are this close were taken at the same spot: on
# 20261031.101107 photos 90-92 were 3 cm apart (pulses 1.6 s apart, the van
# standing still) and look the same; normal photos are 0.57-0.86 m apart.
# The pulse distance alone is NOT enough (see DUPLICATE_MAX_SHIFT_PX below).
DUPLICATE_MAX_M = 0.05


def _no_rename() -> set:
    from .batch import NO_RENAME_CAMERAS
    return set(NO_RENAME_CAMERAS)


# ...and the PICTURE must agree. The pulse distance only finds candidates: in a
# stretch whose pulses were worked out ("estimated"), it can be wrong -
# 20261101.093228 photo 4 was 6 cm from photo 2 by its pulse but its picture
# had moved 454 px (34 cm). A candidate is a duplicate only when its picture
# moved less than this from the photo it repeats (about 1330 px per metre).
DUPLICATE_MAX_SHIFT_PX = 100
DUPLICATE_MIN_MATCHES = 30


def _picture_shift_px(path_a, path_b) -> float | None:
    """How far the ground moved between two photos, in full-size pixels, from
    matched features (the most common shift). None when it can't be measured."""
    try:
        import cv2
    except ImportError:
        return None
    scale = 0.5

    def load(p):
        g = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if g is None:
            return None
        g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return cv2.createCLAHE(2.0, (8, 8)).apply(g)

    a, b = load(path_a), load(path_b)
    if a is None or b is None:
        return None
    orb = cv2.ORB_create(4000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None:
        return None
    m = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(m) < DUPLICATE_MIN_MATCHES:
        return None
    d = (np.float32([kb[x.trainIdx].pt for x in m])
         - np.float32([ka[x.queryIdx].pt for x in m]))
    # the shift most matches agree on (a fixed sample of candidates, so the
    # answer is the same every run)
    best, best_n = None, 0
    for c in d[:: max(1, len(d) // 300)]:
        n = int(np.sum(np.linalg.norm(d - c, axis=1) < 3))
        if n > best_n:
            best, best_n = c, n
    if best_n < DUPLICATE_MIN_MATCHES:
        return None
    sel = np.linalg.norm(d - best, axis=1) < 3
    return float(np.linalg.norm(np.median(d[sel], axis=0)) / scale)


def _duplicates_by_pulse(al, by_pulse: dict) -> dict:
    """Duplicates for a camera that can't judge its own pictures: the photo
    taken on a pulse the judging camera called a duplicate repeats the photo
    taken on that camera's kept pulse. Gets that photo's position."""
    trg = al.match.trigger_for_image
    photo_of = {int(t): j for j, t in enumerate(trg) if int(t) >= 0}
    out = {}
    for j, t in enumerate(trg):
        k = photo_of.get(by_pulse.get(int(t), -1)) if int(t) >= 0 else None
        if k is None:
            continue
        out[j] = k
        for arr in (al.lat_deg, al.lon_deg, al.alt_m, al.camera_lat_deg, al.camera_lon_deg):
            if arr is not None:
                arr[j] = arr[k]
    return out


def _duplicates(al) -> dict:
    """{photo index: index of the photo it repeats}, and give each duplicate
    that photo's position (Derek, 2026-11-10: same GPS). The first photo of a
    group is kept; the distance is measured to it, so a slow creep can't chain.
    A candidate found by its pulse is only a duplicate when its picture agrees
    (see DUPLICATE_MAX_SHIFT_PX)."""
    out = {}
    folder = Path(al.images.dir)
    trg = al.match.trigger_for_image
    dist = np.asarray(al.dmi_dist_m, dtype=np.float64)
    ordinal = np.rint(np.asarray(al.images.counter_mm, dtype=np.float64) / 750.0)
    keep = prev = None
    for j in np.argsort(ordinal):
        if int(trg[j]) < 0 or not np.isfinite(dist[j]):
            continue
        # under 5 cm from the photo before AND under 10 cm from the one kept:
        # 101107 photos 90, 91, 92 were 3 cm apart each (6 cm end to end) and
        # all look the same, while a slow creep can't chain on for metres
        near = (prev is not None and abs(dist[j] - dist[prev]) < DUPLICATE_MAX_M
                and abs(dist[j] - dist[keep]) < 2 * DUPLICATE_MAX_M)
        if near:
            shift = _picture_shift_px(folder / al.images.files[j],
                                      folder / al.images.files[keep])
            near = shift is not None and shift < DUPLICATE_MAX_SHIFT_PX
        prev = j
        if near:
            out[int(j)] = int(keep)
            for arr in (al.lat_deg, al.lon_deg, al.alt_m):
                arr[j] = arr[keep]
            for arr in (al.camera_lat_deg, al.camera_lon_deg):
                if arr is not None:
                    arr[j] = arr[keep]
            continue
        keep = j
    return out


def _gaps_and_status(al, spans, dups=None) -> tuple[np.ndarray, list]:
    """Per image: metres missing before it, and 'confirmed'/'estimated'."""
    n = len(al.images)
    gap = np.zeros(n)
    trg = al.match.trigger_for_image
    dist = np.asarray(al.triggers.dist_m, dtype=np.float64)
    ordinal = np.rint(np.asarray(al.images.counter_mm, dtype=np.float64)
                      / (TRIGGER_SPACING_M * 1000.0)).astype(np.int64)
    order = np.argsort(ordinal)
    prev = None
    for j in order:
        i = int(trg[j])
        if i < 0:
            continue
        if prev is not None:
            step = dist[i] - dist[prev]
            if np.isfinite(step) and step - TRIGGER_SPACING_M > HOLE_MIN_EXTRA_M:
                gap[j] = step - TRIGGER_SPACING_M
        prev = i
    status = ["estimated" if any(a <= o <= b for a, b in (spans or [])) else "confirmed"
              for o in ordinal]
    for j in (dups or {}):
        status[j] = "duplicate"
    return gap, status


def write_missing_photos_csv(result: "RunResult", out_dir: Path,
                             rename_info: dict | None = None) -> Path | None:
    """`MissingPhotos_<run>.csv`: one row per hole (ground with no photo) per
    camera, with the photos either side. Removed when the run has none, so a
    re-run never leaves a stale list. Returns the path written, or None."""
    out = Path(out_dir) / f"MissingPhotos_{result.run.run_id}.csv"
    rows = []
    for cam_name, al in sorted(result.camera_alignments.items()):
        gap, _ = _gaps_and_status(al, result.estimated_spans.get(cam_name),
                                  result.duplicates.get(cam_name))
        ren = (rename_info or {}).get(cam_name)
        finals = list(getattr(ren, "final_names", []) or [])
        ordinal = np.rint(np.asarray(al.images.counter_mm, dtype=np.float64) / 750.0)
        dups = result.duplicates.get(cam_name, {})
        order = list(np.argsort(ordinal))
        for pos, j in enumerate(order):
            if gap[j] <= 0 or pos == 0:
                continue
            a = order[pos - 1]
            a = dups.get(a, a)     # name the photo kept, not one moved to Duplicates/
            name = lambda x: finals[x] if x < len(finals) else al.images.files[x]
            length = gap[j] + TRIGGER_SPACING_M
            t0, t1 = al.utc_s[a], al.utc_s[j]
            iso = lambda t: (datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S")
                             if np.isfinite(t) else "")
            rows.append([result.run.run_id, cam_name, name(a), name(j), iso(t0), iso(t1),
                         f"{length:.2f}", max(1, int(round(length / TRIGGER_SPACING_M)) - 1),
                         f"{al.lat_deg[a]:.9f}", f"{al.lon_deg[a]:.9f}",
                         f"{al.lat_deg[j]:.9f}", f"{al.lon_deg[j]:.9f}"])
    if not rows:
        if out.exists():
            out.unlink()
        return None
    with atomic_open(out) as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "camera", "last_photo_before", "first_photo_after",
                    "utc_before", "utc_after", "length_m", "about_missing_photos",
                    "lat_before", "lon_before", "lat_after", "lon_after"])
        w.writerows(rows)
    return out


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

    # atomic: the table other processes read, one row per image
    with atomic_open(out_path) as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for cam_name, al in sorted(alignments.items()):
            ren = (rename_info or {}).get(cam_name)
            finals = list(getattr(ren, "final_names", []) or [])
            dists = list(getattr(ren, "distances_mm", []) or [])
            aside = ren.set_aside() if ren is not None else []
            section = getattr(ren, "section", "") or ""
            dups = getattr(result, "duplicates", {}).get(cam_name, {})
            gap, status = _gaps_and_status(al, getattr(result, "estimated_spans", {}).get(cam_name),
                                           dups)
            # a duplicate the batch moved into Duplicates/ is set aside too
            # (every camera, ROW included - Derek 2026-11-10)
            if rename_info and dups:
                aside = list(aside) + [False] * max(0, len(al.images) - len(aside))
                for j in dups:
                    aside[j] = True
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
                    f"{getattr(al, 'view_m', 0.0) or 0.0:.3f}",
                    num((al.camera_lat_deg if getattr(al, "camera_lat_deg", None)
                         is not None else al.lat_deg)[j]),
                    num((al.camera_lon_deg if getattr(al, "camera_lon_deg", None)
                         is not None else al.lon_deg)[j]),
                    f"{gap[j]:.2f}", status[j],
                ])
    try:
        write_missing_photos_csv(result, out_path.parent, rename_info)
    except Exception:
        pass   # the hole list is a convenience; the table above is the record
    return out_path
