"""Data quality checks — Instructions "Data quality checks" section.

Three layers: presence, format, content. Results are structured so tests can
assert on them and the GUI just renders them. FAIL blocks export; WARN is
recorded in the export CSV notes column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from . import discovery as disc
from .alignment import (
    PtpOffsetResult, TriggerData, TRIGGER_SPACING_M, gocator_distances,
)
from .formats import (
    DmiTable, GocatorIndex, ImageSet, NavTable, ParseError, UtcTable,
    GOCATOR_COUNTS_PER_M, IMAGE_STEP_MM,
)


class Severity(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    INFO = "INFO"


# The things a run can produce. A failure is scoped to the ones it actually
# invalidates, so a fault in one is not charged to the others.
TARGET_IMAGES = "images"
TARGET_GOCATOR_L = "gocator_L"
TARGET_GOCATOR_R = "gocator_R"
TARGET_CSV = "csv"
TARGETS = (TARGET_IMAGES, TARGET_GOCATOR_L, TARGET_GOCATOR_R, TARGET_CSV)

# How much of a section may have images but no laser profiles before it is
# worth reporting. The lasers start recording after the run has begun — on
# 20260824 the left missed 0.64 m of every run on average and the right
# 1.16 m, and the right consistently started 1.38 s +-0.62 after the left.
# Derek set this limit at 1.0 m (2026-08-31) so that lag stays visible while
# it is being chased.
GOCATOR_MAX_MISSING_M = 1.0

# How far the SBG must have travelled before the run counts as under way.
# Used to skip a stall at the head of the trigger window (see below). It is
# well under one 0.750 m trigger interval, so it can never eat real travel
# that carries an image.
STALL_M = 0.10

# Biggest gap between consecutive triggers before it is a hole in the
# collection rather than ordinary spacing jitter. Triggers are nominally
# 0.750 m apart; measured spacing on real runs sits between 0.566 and
# 0.860 m, so 1.5 m is clear of the noise and still catches a single missed
# trigger. Named as a distance, not a ratio, on purpose - see the check.
MAX_TRIGGER_GAP_M = 1.5

# How close ACS's shortfall and our measured gap have to be before the two
# systems are describing the same loss. One trigger interval: if they agree to
# within a single image, that is agreement.
#
# There is deliberately no position tolerance to go with this. ACS's chainage
# is a count, not a place (see check_acs_qc_video), so comparing positions
# invented a 250 m disagreement on 20260819.190301 - the one run where the two
# systems actually agreed.
ACS_LENGTH_MATCH_M = TRIGGER_SPACING_M


@dataclass
class QCCheck:
    check_id: str
    name: str
    severity: Severity
    message: str
    values: dict = field(default_factory=dict)
    # Which deliverables a FAIL here stops. None = the whole run, which stays
    # the default: a check has to opt IN to being narrow, so a new check added
    # later is conservative until someone has thought about its blast radius.
    # Scoped so that one laser's clock cannot cost a run its images: the images
    # are placed from triggers + the corrected export and never touch the
    # Gocator at all (Derek, 2026-08-26 — run 20260824.101724, where 59 bad
    # profiles at the start of R blocked 76 perfectly good images).
    blocks: tuple = None


@dataclass
class QCReport:
    run_id: str
    checks: list[QCCheck] = field(default_factory=list)

    def add(self, check_id: str, name: str, severity: Severity, message: str,
            blocks: tuple = None, **values) -> QCCheck:
        c = QCCheck(check_id, name, severity, message, values, blocks)
        self.checks.append(c)
        return c

    @property
    def failed(self) -> list[QCCheck]:
        return [c for c in self.checks if c.severity == Severity.FAIL]

    @property
    def warned(self) -> list[QCCheck]:
        return [c for c in self.checks if c.severity == Severity.WARN]

    def blocked_targets(self) -> set:
        """Which deliverables must NOT be written, given the failures."""
        out: set = set()
        for c in self.failed:
            out |= set(c.blocks) if c.blocks is not None else set(TARGETS)
        return out

    def blocked_reasons(self, target: str) -> list:
        """The checks responsible for holding `target` back."""
        return [c for c in self.failed
                if target in (set(c.blocks) if c.blocks is not None else set(TARGETS))]

    @property
    def export_blocked(self) -> bool:
        """The image->position CSV cannot be written. NOT 'the run failed' —
        a sensor-scoped failure leaves this False on purpose."""
        return TARGET_CSV in self.blocked_targets()

    def notes(self) -> str:
        return "; ".join(f"{c.check_id}: {c.message}" for c in self.warned)


# ---------------------------------------------------------------------------
# Layer 1 — presence
# ---------------------------------------------------------------------------

def check_presence(run: disc.RunPaths, report: QCReport) -> list[str]:
    """Returns the list of missing input keys (the GUI turns these into
    'Locate…' browse prompts — a missing input is normal, not yet a FAIL)."""
    missing = run.missing()
    for key in disc.REQUIRED_KEYS:
        if key in missing:
            severity = Severity.WARN if key in disc.OPTIONAL_KEYS else Severity.FAIL
            report.add(
                f"presence.{key}", f"Input present: {key}", severity,
                f"NOT FOUND — {disc.KEY_DESCRIPTIONS[key]}. Use Locate… to point at it.",
                key=key,
            )
        else:
            report.add(
                f"presence.{key}", f"Input present: {key}", Severity.PASS,
                str(run.get(key)), key=key,
            )
    # A run whose logs came from a differently-named folder is still a normal
    # run — the two systems just started their folders a moment apart — but the
    # user must be told which folder was used, because the alternative reading
    # (these triggers belong to a DIFFERENT run) would write wrong positions.
    # WARN, not FAIL: the trigger-count match and the PTP fit below are what
    # actually catch a wrong pairing, and they run either way.
    if run.sbg_stamp and run.sbg_skew_s:
        report.add(
            "presence.sbg_stamp_skew", "SBG log folder pairing", Severity.WARN,
            f"triggers and clock came from '{run.sbg_stamp}{disc.SBG_LOGGER_SUFFIX}', "
            f"{run.sbg_skew_s:+d} s from this run's stamp {run.run_id} — the logger "
            "and the collection system start their folders independently. Paired "
            "on nearest time; confirm the image/trigger match and PTP fit below.",
            sbg_stamp=run.sbg_stamp, skew_s=run.sbg_skew_s,
        )
    return missing


# ---------------------------------------------------------------------------
# Layer 2 — format (parse everything; ParseError -> FAIL)
# ---------------------------------------------------------------------------

@dataclass
class ParsedRun:
    """Everything parsed for one run (fields None when missing/failed)."""

    run: disc.RunPaths
    images: ImageSet | None = None            # primary camera (viewer)
    cameras: dict = None                      # every camera: name -> ImageSet
    triggers_sbg_us: np.ndarray | None = None
    utc: UtcTable | None = None
    dmi: DmiTable | None = None
    nav: NavTable | None = None
    events: "object | None" = None            # EventTable — whole-session triggers
    gocator_l: GocatorIndex | None = None
    gocator_r: GocatorIndex | None = None
    calibration: "object | None" = None  # CameraCalibration
    lever_arms: dict | None = None            # {name: (x, y, z)} from the lever-arm file


def parse_all(run: disc.RunPaths, report: QCReport) -> ParsedRun:
    from .calibration import CameraCalibration, load_lever_arms_file
    from .events import EventTable
    from .formats import parse_event_triggers

    pr = ParsedRun(run=run)
    pr.cameras = {}

    def attempt(check_id: str, name: str, key: str, fn):
        path = run.get(key)
        if path is None:
            return None
        try:
            value = fn(path)
            report.add(check_id, name, Severity.PASS, f"parsed OK: {Path(path).name}")
            return value
        except ParseError as exc:
            # optional inputs (DMI, calibration) never block a GPS write
            sev = Severity.WARN if key in disc.OPTIONAL_KEYS else Severity.FAIL
            suffix = ("  (optional — GPS writing does not use it; viewer features "
                      "disabled for this run)") if sev is Severity.WARN else ""
            report.add(check_id, name, sev, str(exc) + suffix)
            return None

    pr.images = attempt("format.images", "Image folder scan", disc.KEY_IMAGES, ImageSet.scan)
    primary_dir = run.get(disc.KEY_IMAGES)
    for cam_name, cam_dir in run.cameras.items():
        try:
            # The primary camera is BOTH paths[KEY_IMAGES] and a member of
            # run.cameras, so scanning it here as well produced a second,
            # equal-but-distinct ImageSet. Every `is pr.images` guard
            # downstream then failed to fire and the primary was checked
            # twice, reporting content.image_sequence AND
            # content.image_sequence.<primary> with identical contents.
            # Reuse the object instead of comparing objects that can never
            # be the same one.
            if pr.images is not None and primary_dir is not None \
                    and Path(cam_dir) == Path(primary_dir):
                pr.cameras[cam_name] = pr.images
            else:
                pr.cameras[cam_name] = ImageSet.scan(cam_dir)
            report.add(f"format.camera.{cam_name}", f"Camera '{cam_name}' scan",
                       Severity.PASS, f"{len(pr.cameras[cam_name])} images in {cam_dir.name}")
        except ParseError as exc:
            report.add(f"format.camera.{cam_name}", f"Camera '{cam_name}' scan",
                       Severity.FAIL, str(exc))
    pr.triggers_sbg_us = attempt("format.event_a", "eventOutA.txt", disc.KEY_EVENT_A, parse_event_triggers)
    pr.utc = attempt("format.utc_time", "utcTime.txt", disc.KEY_UTC_TIME, UtcTable.parse)
    pr.dmi = attempt("format.dmi", "DmiStationEx CSV", disc.KEY_DMI, DmiTable.parse)
    pr.gocator_l = attempt("format.gocator_l", "Gocator L CSV", disc.KEY_GOCATOR_L, GocatorIndex.build)
    pr.gocator_r = attempt("format.gocator_r", "Gocator R CSV", disc.KEY_GOCATOR_R, GocatorIndex.build)
    pr.calibration = attempt("format.calibration", "Calibration YAML", disc.KEY_CALIBRATION, CameraCalibration.load)
    pr.lever_arms = attempt("format.lever_arms", "Lever-arm file", disc.KEY_LEVER_ARMS,
                            load_lever_arms_file)
    if pr.utc is not None:
        pr.nav = attempt(
            "format.nav_export", "Qinertia ascii-output.txt", disc.KEY_NAV_EXPORT,
            lambda p: NavTable.parse(p, anchor_utc_date=pr.utc.utc_date),
        )
        pr.events = attempt(
            "format.events_export", "Qinertia Events-output.txt", disc.KEY_EVENTS_EXPORT,
            lambda p: EventTable.parse(p, anchor_utc_date=pr.utc.utc_date),
        )
    elif run.get(disc.KEY_NAV_EXPORT) is not None:
        report.add(
            "format.nav_export", "Qinertia ascii-output.txt", Severity.FAIL,
            "cannot parse export without utcTime.txt (needed for the UTC date anchor)",
        )
    return pr


# ---------------------------------------------------------------------------
# Layer 3 — content
# ---------------------------------------------------------------------------

def leading_recovered(report: QCReport) -> int:
    """How many pre-collection triggers were prepended to this run's train.

    process_run() recovers them from the Qinertia event export and reports the
    count on align.pre_collection_triggers BEFORE check_content runs, so the
    report is the record of what the trigger list actually contains.
    """
    for c in report.checks:
        if c.check_id == "align.pre_collection_triggers":
            return int(c.values.get("n_recovered", 0) or 0)
    return 0


def trigger_gaps(triggers: TriggerData, skip_leading: int = 0) -> list[tuple[float, float]]:
    """Every stretch of the run with no trigger, as (start_m, end_m) measured
    from the FIRST TRIGGER of the run.

    triggers.dist_m is absolute along-track distance from the start of the
    Qinertia export, which on a full-session export is thousands of metres
    before this run began. Rebasing here, once, is what stops "at 2169 m into
    the run" being printed for a 330 m run - it was reported that way before
    this function existed, and it is why both callers share it.

    `skip_leading` is the number of recovered pre-collection triggers at
    the head of the train - see the comment on the filter below.
    """
    dist = np.asarray(triggers.dist_m, dtype=np.float64)
    spacing = np.asarray(triggers.spacing_m, dtype=np.float64)
    if len(spacing) == 0 or len(dist) == 0:
        return []
    base = float(dist[0])
    over = np.where(np.isfinite(spacing) & (spacing > MAX_TRIGGER_GAP_M))[0]
    # Intervals that START inside the recovered pre-collection prefix are not
    # dropouts. Those triggers fired before ACS began logging, while the cart
    # was being lined up on the section, so the distance between the last one
    # and the first logged trigger is however far the operator rolled - not a
    # camera fault. On 20260824.101313 that interval is 2.19 m and this check
    # called it "about 2 trigger(s) never fired". ACS's own QC_Video.csv
    # listed nothing wrong with that run, which is how the false positive was
    # caught: a second witness is worth more than a tighter limit.
    over = [int(i) for i in over if i >= skip_leading]
    return [(float(dist[i] - base), float(dist[i + 1] - base)) for i in over]


def check_acs_qc_video(pr: ParsedRun, report: QCReport,
                       triggers: TriggerData | None) -> None:
    """Cross-check our trigger dropouts against ACS's own QC_Video.csv.

    Two systems watched the same cameras. ACS counted the images it asked for
    against the images it got; we measure the distance between consecutive
    triggers in the corrected export.

    COMPARE AMOUNTS, NOT PLACES. ACS names every image `ordinal x 750 mm`, so
    an image's name is how many came before it, not where it was taken. Its
    QC_Video row is arithmetic on that: Begin Chainage is (images it got) x
    750 mm and End Chainage is the Daily file's CollLength, so the row is the
    shortfall booked against the tail of the section. Worked through on
    20260819.190301, where every number lands exactly:

        images ACS got         436
        436 x 750 mm       327,000 mm   = its Begin Chainage 0.327
        CollLength         331,654 mm   = its End Chainage 0.331654491
        shortfall            4,654 mm   = 7 image slots

    and the same arithmetic explains why the three clean runs that day have no
    row at all (each within one image of its CollLength). A mid-run dropout
    pulls every later name backwards, so the deficit always surfaces at the
    end. ACS therefore knows HOW MUCH was lost and cannot know WHERE.

    We know where: the 4.798 m gap sits at 76 m into that run, in the trigger
    train and in the renamed filenames of both cameras independently. So the
    two are not rivals - ACS is the second opinion on the amount, and this app
    is the only source for the location. Comparing the positions produced a
    250 m "disagreement" on the one run where they actually agreed, which is a
    false alarm on precisely the case the check exists for.

      amounts agree     -> PASS. The hole itself is reported once, by
                           content.trigger_gap; repeating it here would be the
                           same defect counted twice.
      ACS short, we saw
      nothing           -> WARN. We are blind to something real, and ACS is the
                           system that actually asked for the picture.
      we saw a gap, ACS
      is not short      -> WARN. Either ACS cannot see it, or our gap is an
                           artefact of the corrected export.
      both, but the
      amounts differ    -> WARN. Two different defects, or one of us is
                           measuring it wrong.

    Never a FAIL, and it blocks nothing. A disagreement between two QC systems
    is a reason for a human to look, not a reason to withhold positions we have
    every other reason to believe.
    """
    from .daily import find_qc_video_file, read_qc_video, video_gaps_for_run

    cid, name = "content.acs_qc_video", "ACS QC_Video cross-check"
    path = find_qc_video_file(Path(pr.run.root))
    if path is None:
        # NOT a pass. "ACS reports no missing images" and "ACS never told us"
        # are different statements and must not print the same.
        report.add(cid, name, Severity.INFO,
                   "no QC_Video.csv in the day folder - nothing to cross-check "
                   "against (ACS writes it beside Daily_ARAN104_*.csv)")
        return

    acs = video_gaps_for_run(read_qc_video(path), pr.run.run_id)
    if triggers is None:
        report.add(cid, name, Severity.INFO,
                   f"{len(acs)} missing-image row(s) in {path.name} for this run; "
                   "no trigger data on our side to compare them against",
                   n_acs=len(acs))
        return

    ours = trigger_gaps(triggers, leading_recovered(report))

    # ACS writes one row PER CAMERA VIEW for the same shortfall - 20260819 has
    # an identical ROW row and Rear row. Summing them raw would double the
    # loss, so divide by the number of views to get the loss per camera, which
    # is what our trigger gaps measure (one trigger, all cameras).
    views = max(1, len({g.view for g in acs}))
    acs_len = sum(g.length_m for g in acs) / views
    acs_imgs = sum(g.count for g in acs) / views
    our_len = sum(b - a for a, b in ours)
    where = ""
    if ours:
        a, b = max(ours, key=lambda g: g[1] - g[0])
        where = f" at {a:.0f} m into the run"
    vals = dict(n_acs=len(acs), n_ours=len(ours),
                acs_len_m=acs_len, our_len_m=our_len)

    if not acs and not ours:
        report.add(cid, name, Severity.PASS,
                   f"agrees with ACS: {path.name} lists no missing images for this "
                   "run, and no trigger gap was found", **vals)
    elif acs and not ours:
        g = acs[0]
        report.add(cid, name, Severity.WARN,
                   f"ACS is {acs_len:.2f} m short ({acs_imgs:.0f} image(s) per camera, "
                   f"{g.first_file} to {g.last_file}) and this app found no trigger "
                   "gap at all. ACS is the system that asked for those pictures, so "
                   "treat its count as the one to explain. Note ACS cannot say WHERE "
                   "- its chainage is a count, booked against the end of the section.",
                   **vals)
    elif ours and not acs:
        report.add(cid, name, Severity.WARN,
                   f"this app found {len(ours)} trigger gap(s) totalling "
                   f"{our_len:.2f} m{where}, and ACS reports nothing missing for this "
                   "run. Either ACS cannot see it, or the gap is an artefact of the "
                   "corrected export rather than a real dropout.", **vals)
    elif abs(acs_len - our_len) <= ACS_LENGTH_MATCH_M:
        report.add(cid, name, Severity.PASS,
                   f"agrees with ACS: {acs_len:.2f} m short by ACS's count "
                   f"({acs_imgs:.0f} image(s) per camera), found as {our_len:.2f} m "
                   f"of trigger gap{where}. ACS has the amount, this app has the "
                   "place (reported by content.trigger_gap).", **vals)
    else:
        report.add(cid, name, Severity.WARN,
                   f"ACS and this app disagree on how much is missing: ACS "
                   f"{acs_len:.2f} m ({acs_imgs:.0f} image(s) per camera), this app "
                   f"{our_len:.2f} m across {len(ours)} gap(s){where}. More than one "
                   f"image ({ACS_LENGTH_MATCH_M:.2f} m) apart, so this is not the "
                   "two systems rounding differently.", **vals)


def check_content(pr: ParsedRun, report: QCReport,
                  triggers: TriggerData | None = None,
                  ptp_l: PtpOffsetResult | None = None,
                  ptp_r: PtpOffsetResult | None = None) -> None:
    # Every camera fires on the SAME trigger, so every camera must hold the
    # same number of photographs. When they disagree, one of them has lost
    # files — and that is worth saying by name, because the alternative is
    # what actually happened on 20260824_revruns_FullProcessingRun: Rear held
    # 38 where ROW held 43, and the only thing the report said was "image/
    # trigger count difference is -5", which names neither the camera nor the
    # number it should have been.
    #
    # Counts INCLUDE BeforeCollection: those images belong to the run, they
    # are just filed away, and a camera whose footprint reaches further back
    # sets aside more of them (ROW's arm is 0.76 m longer than Rear's, so it
    # routinely sets aside one more). Comparing only what is left in the
    # camera folder would fire on that every single run.
    counts = {name: len(im) + getattr(im, "n_set_aside", 0)
              for name, im in (pr.cameras or {}).items()}
    if len(set(counts.values())) > 1:
        detail = ", ".join(f"{n}: {c}" for n, c in sorted(counts.items()))
        short = min(counts, key=counts.get)
        report.add(
            "content.camera_counts", "Cameras hold the same image count",
            Severity.WARN,
            f"cameras disagree ({detail}) — every camera fires on the same "
            f"trigger, so {short} is missing {max(counts.values()) - counts[short]} "
            "image(s). Check for files left mid-rename or a failed copy",
            counts=counts,
        )
    elif counts:
        report.add("content.camera_counts", "Cameras hold the same image count",
                   Severity.PASS,
                   f"{len(counts)} camera(s), {next(iter(counts.values()))} images each",
                   counts=counts)

    # Sequence continuity, for EVERY camera. The primary keeps the original
    # check id so existing readers still find it; the others get their own.
    # A hole here is the dangerous kind of loss: matching is tail-anchored, so
    # images missing from the START line up anyway, but a gap in the MIDDLE
    # shifts everything before it against the triggers.
    for cam_name, cam_images in sorted((pr.cameras or {}).items()):
        if pr.images is not None and cam_images is pr.images:
            continue
        gaps = cam_images.sequence_gaps(IMAGE_STEP_MM)
        report.add(
            f"content.image_sequence.{cam_name}", f"Image sequence ({cam_name})",
            Severity.WARN if gaps else Severity.PASS,
            (f"{len(gaps)} missing counter(s) in the {IMAGE_STEP_MM} mm sequence "
             f"(first few: {gaps[:5]})" if gaps else
             f"{len(cam_images)} images, continuous {IMAGE_STEP_MM} mm steps"),
            missing=gaps,
        )

    # Images: sequence continuity + first/last readable
    if pr.images is not None:
        gaps = pr.images.sequence_gaps(IMAGE_STEP_MM)
        if gaps:
            report.add(
                "content.image_sequence", "Image sequence", Severity.WARN,
                f"{len(gaps)} missing counter(s) in the {IMAGE_STEP_MM} mm sequence "
                f"(first few: {gaps[:5]})", missing=gaps,
            )
        else:
            report.add("content.image_sequence", "Image sequence", Severity.PASS,
                       f"{len(pr.images)} images, continuous {IMAGE_STEP_MM} mm steps")
        probe = [0, len(pr.images) - 1] if len(pr.images) > 1 else [0]
        unreadable = [pr.images.files[i] for i in probe if not pr.images.is_readable_jpeg(i)]
        if unreadable:
            report.add("content.image_readable", "Images readable", Severity.FAIL,
                       f"not valid JPEG: {unreadable}")
        else:
            report.add("content.image_readable", "Images readable", Severity.PASS,
                       "first/last images have valid JPEG headers")

    # Trigger count vs image count
    if pr.images is not None and pr.triggers_sbg_us is not None:
        n_img, n_trg = len(pr.images), len(pr.triggers_sbg_us)
        sev = Severity.PASS if abs(n_img - n_trg) <= 3 else Severity.WARN
        report.add(
            "content.trigger_count", "Trigger vs image count", sev,
            f"{n_trg} triggers vs {n_img} images (off-by-{abs(n_img - n_trg)}; "
            "small offsets are normal — the matcher resolves them)",
            n_triggers=n_trg, n_images=n_img,
        )

    # utcTime continuity + trigger coverage
    if pr.utc is not None:
        gap = pr.utc.max_gap_s
        sev = Severity.PASS if gap <= 2.0 else Severity.WARN
        report.add("content.utc_gap", "utcTime continuity", sev,
                   f"max gap {gap:.2f} s (limit 2 s)", max_gap_s=gap)
        if pr.triggers_sbg_us is not None and len(pr.triggers_sbg_us):
            lo, hi = int(pr.triggers_sbg_us[0]), int(pr.triggers_sbg_us[-1])
            covered = pr.utc.sbg_us[0] <= lo and hi <= pr.utc.sbg_us[-1]
            report.add(
                "content.utc_covers_triggers", "utcTime covers triggers",
                Severity.PASS if covered else Severity.WARN,
                "trigger time span inside utcTime table" if covered else
                "triggers extend past the utcTime table edge (edge extrapolation used)",
            )

    # Ruler checks (along-track distance from the CORRECTED export)
    if pr.nav is not None and triggers is not None and len(triggers.dist_m):
        expect = len(triggers.dist_m) * TRIGGER_SPACING_M
        span = float(triggers.dist_m[-1] - triggers.dist_m[0]) + TRIGGER_SPACING_M
        dev = abs(span - expect) / expect * 100 if expect else 0.0
        sev = Severity.PASS if dev <= 5.0 else Severity.WARN
        report.add("content.trigger_span", "Trigger span (export ruler)", sev,
                   f"{span:.2f} m vs {expect:.2f} m expected from trigger count ({dev:.1f}% off)",
                   span_m=span, expected_m=expect)
        sev = Severity.PASS if triggers.spacing_ok_frac >= 0.95 else Severity.WARN
        report.add("content.trigger_spacing", "Trigger spacing", sev, triggers.message,
                   ok_frac=triggers.spacing_ok_frac)

        # The gap the FRACTION above cannot see.
        #
        # spacing_ok_frac asks "what proportion of intervals are the right
        # size", so one enormous interval is 1/435 of a run and sails past a
        # 95% limit. On 20260819.190301 the camera stopped firing for 3.456 s
        # while the cart drove on at 1.40 m/s: a 4.8 m stretch of sidewalk with
        # no photographs, and every ratio-based check in the app reported that
        # run as fine. The only thing that pointed at it was
        # align.counter_scale, and only because the flat 750 mm counter step
        # against 4797 mm of real travel wrecked a straight-line fit.
        #
        # THE LASERS ARE NOT AFFECTED, and this check must not say they are.
        # The Gocators are triggered off the wheel encoder, not off the camera
        # sync line, so a camera dropout costs images only. Measured over that
        # exact window on 2026-09-01: 205 profiles per side between 75.8 and
        # 80.7 m, 24.2 mm worst spacing, zero holes - full density straight
        # through the hole in the photographs. I had this wrong when the check
        # was written and it said "no images and no profiles"; the laser's own
        # coverage is content.gocator_*_coverage and nothing else.
        #
        # So this asks the question directly: is there a hole, and where. It
        # is the same lesson as gocator_*_coverage — a proportion hides a
        # single large defect, and the defect is the thing that matters.
        gaps = np.asarray(triggers.spacing_m, dtype=np.float64)
        gaps = gaps[np.isfinite(gaps)]
        if len(gaps):
            worst = float(np.max(gaps))
            over = trigger_gaps(triggers, leading_recovered(report))
            missed = int(round(worst / TRIGGER_SPACING_M)) - 1
            where = ""
            if over:
                biggest = max(over, key=lambda g: g[1] - g[0])
                where = f" at {biggest[0]:.0f} m into the run"
            report.add(
                "content.trigger_gap", "Trigger dropouts",
                Severity.WARN if over else Severity.PASS,
                (f"{len(over)} gap(s) over {MAX_TRIGGER_GAP_M:.1f} m; worst "
                 f"{worst:.2f} m{where} — about {missed} trigger(s) never fired, "
                 "so that stretch has no images. The lasers run off the wheel "
                 "encoder, not the camera sync, so their profiles are unaffected "
                 "- see content.gocator_*_coverage"
                 if over else
                 f"no gap over {MAX_TRIGGER_GAP_M:.1f} m (worst {worst:.2f} m)"),
                n_gaps=len(over), worst_gap_m=worst,
            )

    check_acs_qc_video(pr, report, triggers)

    # Realtime-vs-postprocessed distance cross-check.
    #
    # WHAT THESE TWO ARE (verified with Derek 2026-08-20 — do not "simplify"):
    # DmiStationEx is the SBG's own ESTIMATED travelled distance. The raw wheel
    # encoder counts are never logged: the shared encoder aids the SBG's Kalman
    # filter and fires the camera triggers, but post-processing only outputs
    # velocity. So this compares two ESTIMATES — SBG realtime vs Qinertia
    # postprocessed — not a wheel against a solution.
    #
    # WHY THE MOVING WINDOW: over the whole logged window this is dominated by
    # GNSS noise integrated while the cart sits parked. On 20260817_revrus3 the
    # export invented 10-12 m during ~37 s of parking, which read as "14% apart"
    # and looked like a hardware fault. It is not.
    #
    # The trigger window alone is not enough, because parking happens INSIDE
    # it. The SBG fires the camera every 0.750 m of realtime travel, so the
    # run's first trigger fires as the cart is manoeuvred into place and then
    # the cart can sit still for a long time before the run proper begins —
    # 156 s on 20260824.101313, during which the SBG advanced 0.01 m and the
    # export accumulated 1.50 m of wander. That single dead slice was the
    # whole of the 4.40% this check reported on that run. Skip the stall and
    # every run of 20260824 lands inside 0.7%.
    #
    # The stall is found on the SBG's own DISTANCE rather than its speed: the
    # question being asked is precisely "when did the SBG start counting
    # travel", and the trigger fires on the last moving sample before the
    # cart is set down, so a speed test can be satisfied by that one sample
    # and skip nothing.
    if pr.dmi is not None and pr.nav is not None:
        if triggers is not None and len(triggers.utc_s) > 1:
            a, b = float(triggers.utc_s[0]), float(triggers.utc_s[-1])
            # end of any stall at the head of the window
            win = (pr.dmi.epoch_s >= a) & (pr.dmi.epoch_s <= b)
            d0 = float(pr.dmi.dist_at(a))
            stalled = win & (pr.dmi.dist_m - d0 <= STALL_M)
            a_mov = float(pr.dmi.epoch_s[stalled][-1]) if np.any(stalled) else a
            parked_s = a_mov - a
            if b - a_mov > 5.0:
                sbg = float(pr.dmi.dist_at(b) - pr.dmi.dist_at(a_mov))
                export = float(pr.nav.dist_at(b) - pr.nav.dist_at(a_mov))
                dev = abs(sbg - export) / max(abs(export), 1e-9) * 100
                sev = Severity.PASS if dev <= 2.0 else Severity.WARN
                held = (f", ignoring {parked_s:.0f} s parked at the start"
                        if parked_s > 1.0 else "")
                report.add(
                    "content.realtime_vs_export", "SBG realtime vs postprocessed distance", sev,
                    f"over the moving window: SBG {sbg:.2f} m vs export {export:.2f} m "
                    f"({dev:.2f}% apart{held}; both are estimates, placement uses "
                    "the export)",
                    sbg_m=sbg, export_m=export, dev_pct=dev, parked_s=parked_s,
                )
            # Phantom distance before collection starts: a GNSS-quality signal,
            # never a hardware fault. Reported separately so it cannot be
            # mistaken for one.
            lead_lo = max(float(pr.dmi.epoch_s[0]), float(pr.nav.epoch_s[0]))
            if a - lead_lo > 5.0:
                moved = float(pr.dmi.dist_at(a) - pr.dmi.dist_at(lead_lo))
                drifted = float(pr.nav.dist_at(a) - pr.nav.dist_at(lead_lo))
                phantom = drifted - moved
                # Not actionable, so not a warning. It is normal GNSS
                # behaviour while the cart stands still, it is entirely
                # BEFORE the first trigger, and nothing is placed from it:
                # images are positioned from absolute lat/lon at their own
                # trigger time, and the only consumer of cumulative distance
                # uses differences, which a constant offset cancels out of.
                # The one thing that used to reach back into this window —
                # the section-start anchor — no longer can (core.rename).
                sev = Severity.PASS
                report.add(
                    "content.nav_settling", "Nav solution before collection", sev,
                    f"in the {a - lead_lo:.0f} s before the first trigger the cart moved "
                    f"{moved:.2f} m but the export accumulated {drifted:.2f} m "
                    f"({phantom:+.2f} m of position noise). Harmless for placement — it is "
                    f"outside the run — but it indicates how well converged the solution was.",
                    phantom_m=phantom, moved_m=moved, lead_s=a - lead_lo,
                )

    # Gocator checks
    for side, goc, ptp in (("L", pr.gocator_l, ptp_l), ("R", pr.gocator_r, ptp_r)):
        if goc is None:
            continue
        # This laser's arm has to be IN the file. Without it the profiles
        # would be stamped at the IMU, 0.3575 m from the wheelpath - and
        # until 2026-09-02 that is exactly what an unlisted side got, as
        # zeros. When the file itself is missing, format.lever_arms has
        # already failed the whole run and this row would only repeat it.
        if pr.lever_arms is not None:
            arm = pr.lever_arms.get(side)
            report.add(
                f"content.gocator_{side}_arm", f"Gocator {side} lever arm",
                Severity.PASS if arm is not None else Severity.FAIL,
                (f"arm (X {arm[0]:+.4f}, Y {arm[1]:+.4f}, Z {arm[2]:+.4f}) m from the file"
                 if arm is not None else
                 f"no 'Gocator {'left' if side == 'L' else 'right'}' section in the "
                 "lever-arm file - this laser's GPS columns are held until it has one"),
                blocks=(f"gocator_{side}",), arm=arm,
            )
        frame_gaps = int(np.sum(np.diff(goc.frame) != 1))
        if frame_gaps:
            report.add(f"content.gocator_{side}_frames", f"Gocator {side} frame continuity",
                       Severity.WARN, f"{frame_gaps} frameIndex gap(s)", gaps=frame_gaps)
        else:
            report.add(f"content.gocator_{side}_frames", f"Gocator {side} frame continuity",
                       Severity.PASS, f"{len(goc)} frames, continuous")
        de = np.diff(goc.encoder.astype(np.int64))
        drops = np.where(de < 0)[0]
        if len(drops):
            worst = int(de.min())
            # A drop confined to the last profiles is the logger stopping, not
            # travel. Seen on 20260817.180042: one -6528-count step on the final
            # profile of BOTH sensors at once (Derek, 2026-08-20).
            terminal_only = bool(np.all(drops >= len(de) - 2))
            first, last = int(drops[0]), int(drops[-1])
            # WHERE the decreases are, not a two-way verdict about them.
            # This used to print "1 encoder decrease(s) ... spread through the
            # run", which is a contradiction: one event is somewhere, it is not
            # spread. 20260821.130056 said exactly that about a single -102
            # count step at profile 18435 of 19479, and a single 24 mm step
            # back at 95% of a run is a different thing to investigate than a
            # scattering of them.
            if len(drops) == 1:
                where = (f"one encoder decrease, at profile {first} of {len(goc)} "
                         f"({100.0 * first / max(len(de), 1):.0f}% through the run)")
            else:
                where = (f"{len(drops)} encoder decreases, from profile {first} to "
                         f"{last} of {len(goc)}")
            msg = (f"{where}; largest {worst} counts "
                   f"(~{abs(worst) / GOCATOR_COUNTS_PER_M * 1000:.0f} mm)")
            if terminal_only:
                msg += (" — on the final profiles, so a logger-shutdown artifact "
                        "rather than travel")
            elif len(drops) == 1:
                msg += (" — one step back and nothing else, so a lost count or a "
                        "momentary reverse rather than a trend")
            else:
                msg += (" — spread through the run, so check for reverse travel "
                        "or lost counts")
            msg += ". Profile positions come from PTP time, not the encoder, so placement is unaffected."
            report.add(f"content.gocator_{side}_encoder", f"Gocator {side} encoder",
                       Severity.WARN, msg, n_drops=len(drops), worst_counts=worst,
                       terminal_only=terminal_only)
        else:
            report.add(f"content.gocator_{side}_encoder", f"Gocator {side} encoder",
                       Severity.PASS, "encoder monotonic")
        valid_ratio = float(np.median(goc.n_valid / np.maximum(goc.n_points, 1)))
        sev = Severity.PASS if valid_ratio >= 0.5 else Severity.WARN
        report.add(f"content.gocator_{side}_valid", f"Gocator {side} valid points", sev,
                   f"median valid-point ratio {valid_ratio * 100:.0f}% (limit 50%)",
                   valid_ratio=valid_ratio)
        if ptp is not None:
            report.add(
                f"content.gocator_{side}_ptp", f"Gocator {side} PTP offset",
                Severity.PASS if ptp.passed else Severity.FAIL, ptp.message,
                # only this sensor's own GPS columns depend on this solve
                blocks=(f"gocator_{side}",),
                offset_s=ptp.offset_s, r=ptp.r,
            )
            if pr.nav is not None and ptp.passed and triggers is not None and len(triggers.dist_m):
                # Measured in METRES of section missing, not as a percentage
                # of the run. The loss is always at the START — across all 22
                # sensor-runs of 20260824 the lasers overshot the last trigger
                # every time and never fell short of it — and it is a startup
                # delay, so it is the same physical size whatever the section
                # length. As a percentage the very same 2.4 m gap reads 8% on
                # a 30 m section and 0.8% on a 300 m one, which would hide it
                # on exactly the long sections where it is easiest to miss.
                d = gocator_distances(goc, ptp, pr.nav)
                lead_m = float(np.min(d)) - float(triggers.dist_m[0])
                tail_m = float(triggers.dist_m[-1]) - float(np.max(d))
                missing = max(lead_m, 0.0) + max(tail_m, 0.0)
                sev = (Severity.PASS if missing <= GOCATOR_MAX_MISSING_M
                       else Severity.WARN)
                where = []
                if lead_m > 0.01:
                    where.append(f"{lead_m:.2f} m at the start")
                if tail_m > 0.01:
                    where.append(f"{tail_m:.2f} m at the end")
                report.add(
                    f"content.gocator_{side}_coverage", f"Gocator {side} section coverage", sev,
                    (f"no profiles for {' and '.join(where)} of the run "
                     f"(limit {GOCATOR_MAX_MISSING_M:.1f} m)"
                     if where else "profiles span the whole run"),
                    missing_m=missing, lead_m=lead_m, tail_m=tail_m,
                )

    # Nav export checks
    if pr.nav is not None:
        if pr.nav.n_dropped_nofix:
            report.add("content.nav_nofix", "Nav no-fix rows", Severity.WARN,
                       f"{pr.nav.n_dropped_nofix} unconverged position row(s) dropped "
                       "from the export — check they do not fall under a run",
                       n_dropped=pr.nav.n_dropped_nofix)
        gap = pr.nav.max_gap_s
        sev = Severity.PASS if gap <= 1.0 else Severity.WARN
        report.add("content.nav_gap", "Nav export continuity", sev,
                   f"max gap {gap:.2f} s (limit 1 s)", max_gap_s=gap)
        gps_utc = pr.nav.gps_minus_utc_s
        sev = Severity.PASS if abs(gps_utc - 18.0) <= 1.0 else Severity.WARN
        report.add("content.nav_gps_utc", "Nav GPS-UTC consistency", sev,
                   f"GPS - UTC = {gps_utc:.1f} s (expected 18 s leap offset)", gps_minus_utc=gps_utc)
        lat, lon = pr.nav.lat_deg, pr.nav.lon_deg
        plausible = (
            np.all(np.abs(lat) <= 90) and np.all(np.abs(lon) <= 180)
            and (np.std(lat) > 0 or np.std(lon) > 0)
        )
        report.add("content.nav_plausible", "Nav positions plausible",
                   Severity.PASS if plausible else Severity.FAIL,
                   "lat/lon in bounds and non-constant" if plausible else
                   "positions out of bounds or constant")
        if triggers is not None and len(triggers.utc_s):
            cov = pr.nav.covers(triggers.utc_s)
            n_out = int(np.sum(~cov))
            if n_out == 0:
                report.add("content.nav_covers_triggers", "Nav covers trigger times",
                           Severity.PASS, "all trigger times inside the export window")
            else:
                report.add(
                    "content.nav_covers_triggers", "Nav covers trigger times", Severity.FAIL,
                    f"{n_out}/{len(cov)} trigger times OUTSIDE the export window — "
                    "location lookup never extrapolates; is this the right export for this run?",
                    n_outside=n_out,
                )
        for side, goc, ptp in (("L", pr.gocator_l, ptp_l), ("R", pr.gocator_r, ptp_r)):
            if goc is None or ptp is None or not ptp.passed:
                continue
            utc = goc.ptp_us.astype(np.float64) / 1e6 - ptp.offset_s
            n_out = int(np.sum(~pr.nav.covers(utc)))
            sev = Severity.PASS if n_out == 0 else Severity.FAIL
            report.add(
                f"content.nav_covers_gocator_{side}", f"Nav covers Gocator {side} times", sev,
                "all profile times inside the export window" if n_out == 0 else
                f"{n_out}/{len(utc)} profile times outside the export window",
                # this laser's own columns only: the images are placed from
                # the triggers and never touch the Gocator (same scoping as
                # content.gocator_*_ptp - it was the one laser check that
                # still blocked the whole run)
                blocks=(f"gocator_{side}",),
                n_outside=n_out,
            )

    # Calibration sanity
    if pr.calibration is not None:
        cal = pr.calibration
        # NOTE: image-resolution vs calibration is enforced at display time —
        # Undistorter.undistort() raises on a mismatch and the GUI surfaces it.
        # Checking it here would mean decoding a JPEG header inside QC.
        sev = Severity.PASS if cal.rms_reprojection_error <= 1.0 else Severity.WARN
        report.add(
            "content.calibration_rms", "Calibration RMS", sev,
            f"RMS reprojection {cal.rms_reprojection_error:.3f} px "
            f"(verdict '{cal.verdict}', serial {cal.serial_number})",
            rms=cal.rms_reprojection_error,
        )
