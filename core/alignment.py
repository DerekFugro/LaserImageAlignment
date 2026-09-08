"""Alignment engine — Instructions §A–§D.

§A  PTP→UTC offset solver (Gocator): correlation sweep of encoder-derived speed
    against DMI speed. Verified ground truth: run 20260818.142721 peaks at
    exactly 37.0 s with r = 0.993.
§B  Trigger→UTC→distance pipeline.
§C  Image↔trigger matching (solves the off-by-one from pre-collection triggers).
§D  Location lookup (never extrapolates; NaN outside the nav window).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .formats import (
    EARTH_M_PER_DEG_LAT, DmiTable, GocatorIndex, ImageSet, NavTable, UtcTable,
    TAI_UTC_NOMINAL_S,
)

# §A solver parameters (Instructions: sweep 30–45 s, 0.05 s step)
PTP_SWEEP_MIN_S = 30.0
PTP_SWEEP_MAX_S = 45.0
PTP_SWEEP_STEP_S = 0.05
PTP_FINE_HALF_SPAN_S = 0.2    # fine sweep around the coarse peak
PTP_FINE_STEP_S = 0.01
PTP_PASS_MIN_R = 0.95
PTP_PASS_MAX_DEV_S = 0.5
# The Gocator self-triggers off the encoder every 102 tics, so the interval
# between two profiles is fixed distance / speed — it cannot be anything else.
# A stamp that disagrees with that by more than this factor is a corrupted
# TIMESTAMP, not real motion, and is dropped before the correlation is scored.
# Two such intervals in 1096 (a 59 ms and a 4.7 ms where ~24 ms was due) took
# run 20260824.101724's right laser from r = 0.958 to r = 0.746 and failed it.
PTP_MAX_INTERVAL_RATIO = 2.0
PTP_MIN_SPEED_MS = 0.05       # below this the expected interval is meaningless

# §B trigger spacing
TRIGGER_SPACING_M = 0.75
TRIGGER_SPACING_TOL_M = 0.05

# §C matching
MATCH_MAX_SHIFT = 3
MATCH_MAX_RESIDUAL_M = 0.375


@dataclass
class PtpOffsetResult:
    """§A output."""

    offset_s: float
    r: float
    passed: bool
    n_used: int
    sweep_offsets: np.ndarray
    sweep_r: np.ndarray
    message: str
    n_bad_intervals: int = 0     # profiles dropped for an impossible timestamp


def _impossible_intervals(ptp_s, enc, ruler, offset: float):
    """Which profile intervals cannot be real, given the encoder and the cart.

    The Gocator self-triggers off the encoder, so between two profiles the
    interval is (fixed distance) / speed. Comparing measured dt against
    `d_encoder / speed` — scaled by the run's own median, so no tic constant
    is assumed — isolates a corrupted timestamp from honest acceleration.

    Returns a boolean mask over the PROFILES (not the intervals): a stamp that
    is wrong makes both the interval before it and the one after it wrong, and
    dropping the profile removes both.
    """
    dt = np.diff(ptp_s)
    d_enc = np.diff(enc)
    spd = ruler.speed_at(ptp_s[:-1] + dt / 2.0 - offset)
    usable = (spd > PTP_MIN_SPEED_MS) & (d_enc > 0) & (dt > 0)
    if int(np.sum(usable)) < 10:
        return np.zeros(len(ptp_s), dtype=bool)
    expect = np.where(usable, d_enc / np.where(usable, spd, 1.0), np.nan)
    scale = np.nanmedian(dt[usable] / expect[usable])
    if not np.isfinite(scale) or scale <= 0:
        return np.zeros(len(ptp_s), dtype=bool)
    ratio = dt / (expect * scale)
    bad_iv = usable & np.isfinite(ratio) & (
        (ratio > PTP_MAX_INTERVAL_RATIO) | (ratio < 1.0 / PTP_MAX_INTERVAL_RATIO))
    bad_profile = np.zeros(len(ptp_s), dtype=bool)
    bad_profile[:-1] |= bad_iv          # the profile that opens a bad interval
    bad_profile[1:] |= bad_iv           # and the one that closes it
    return bad_profile


def solve_ptp_offset(gocator: GocatorIndex, ruler: "NavTable | DmiTable") -> PtpOffsetResult:
    """Solve UTC = ptp_us/1e6 - offset by correlating encoder rate vs ruler speed.

    `ruler` is anything exposing epoch_s / speed_ms / speed_at(). In production
    that is the corrected Qinertia export (NavTable), NOT the raw DMI wheel —
    see Spec Amendment B1. DmiTable satisfies the same interface for tests.

    Solved twice. The first pass finds the offset; with it, profiles whose
    interval is physically impossible can be identified and dropped, and the
    correlation scored on what is left. The correlation is a Pearson r over
    encoder RATE, so a stamp that makes one interval 4.7 ms instead of 24 ms
    is a huge leverage point — two of them in 1096 profiles were the whole
    difference between r = 0.958 and a failed run.
    """
    first = _solve_once(gocator, ruler)
    if abs(first.offset_s - TAI_UTC_NOMINAL_S) > PTP_PASS_MAX_DEV_S or first.r < 0:
        return first          # offset not trustworthy enough to judge intervals
    ptp_s = gocator.ptp_us.astype(np.float64) / 1e6
    bad = _impossible_intervals(ptp_s, gocator.encoder.astype(np.float64),
                                ruler, first.offset_s)
    n_bad = int(np.sum(bad))
    if not n_bad or len(ptp_s) - n_bad < 50:
        return first
    second = _solve_once(gocator, ruler, keep=~bad)
    if second.r <= first.r:
        return first          # dropping them did not help; keep the honest score
    second.n_bad_intervals = n_bad
    second.message += (f"; {n_bad} profile(s) dropped for an impossible scan "
                       f"interval (r was {first.r:.3f} with them)")
    return second


def _solve_once(gocator: GocatorIndex, ruler: "NavTable | DmiTable",
                keep=None) -> PtpOffsetResult:
    ptp_s = gocator.ptp_us.astype(np.float64) / 1e6
    enc = gocator.encoder.astype(np.float64)
    if keep is not None:
        ptp_s, enc = ptp_s[keep], enc[keep]
    if len(ptp_s) < 10:
        return PtpOffsetResult(
            offset_s=TAI_UTC_NOMINAL_S, r=0.0, passed=False, n_used=len(ptp_s),
            sweep_offsets=np.empty(0), sweep_r=np.empty(0),
            message=f"only {len(ptp_s)} profiles — too few to solve; nominal 37 s assumed, NOT verified",
        )
    mid = (ptp_s[1:] + ptp_s[:-1]) / 2.0
    dt = np.diff(ptp_s)
    good = dt > 0
    enc_rate = np.where(good, np.diff(enc) / np.where(good, dt, 1.0), 0.0)
    peak = np.max(enc_rate) if np.max(enc_rate) > 0 else 1.0
    enc_rate = enc_rate / peak

    vmax = float(np.max(ruler.speed_ms)) or 1.0

    def corr_at(off: float) -> float:
        t = mid - off
        ok = (t >= ruler.epoch_s[0]) & (t <= ruler.epoch_s[-1]) & good
        if int(np.sum(ok)) < 50:
            return -1.0
        dv = ruler.speed_at(t[ok]) / vmax
        g = enc_rate[ok]
        if np.std(g) < 1e-12 or np.std(dv) < 1e-12:
            return -1.0
        return float(np.corrcoef(g, dv)[0, 1])

    # coarse sweep (kept in the result for the diagnostics plot)
    offsets = np.arange(PTP_SWEEP_MIN_S, PTP_SWEEP_MAX_S + PTP_SWEEP_STEP_S / 2, PTP_SWEEP_STEP_S)
    rs = np.array([corr_at(o) for o in offsets])
    coarse_best = float(offsets[int(np.argmax(rs))])

    # fine sweep around the coarse peak, then parabolic sub-step refinement.
    # WHY: the 0.05 s coarse grid alone quantizes the offset; at motion start
    # that quantization moves the "Gocator data starts" ground position by
    # speed x error — a per-run, either-sign shift (Derek observed it).
    fine = np.arange(coarse_best - PTP_FINE_HALF_SPAN_S,
                     coarse_best + PTP_FINE_HALF_SPAN_S + PTP_FINE_STEP_S / 2,
                     PTP_FINE_STEP_S)
    fine_rs = np.array([corr_at(o) for o in fine])
    b = int(np.argmax(fine_rs))
    offset, r = float(fine[b]), float(fine_rs[b])
    if 0 < b < len(fine) - 1 and fine_rs[b - 1] > -1 and fine_rs[b + 1] > -1:
        r0, r1, r2 = fine_rs[b - 1], fine_rs[b], fine_rs[b + 1]
        denom = r0 - 2 * r1 + r2
        if denom < 0:  # proper concave peak
            delta = 0.5 * (r0 - r2) / denom
            if abs(delta) <= 1.0:
                offset = float(fine[b] + delta * PTP_FINE_STEP_S)
    n_used = int(np.sum((mid - offset >= ruler.epoch_s[0]) & (mid - offset <= ruler.epoch_s[-1])))
    dev = abs(offset - TAI_UTC_NOMINAL_S)
    passed = (r >= PTP_PASS_MIN_R) and (dev <= PTP_PASS_MAX_DEV_S)
    if r < 0:
        msg = "solver failed: no overlap between Gocator times and the ruler window at any offset"
        passed = False
    else:
        msg = (
            f"PTP-UTC offset {offset:.2f} s (r = {r:.3f}, {n_used} profiles in window) — "
            + ("PASS" if passed else
               f"FAIL: need r >= {PTP_PASS_MIN_R} and within {PTP_PASS_MAX_DEV_S} s of {TAI_UTC_NOMINAL_S}")
        )
    return PtpOffsetResult(
        offset_s=offset, r=r, passed=passed, n_used=n_used,
        sweep_offsets=offsets, sweep_r=rs, message=msg,
    )


@dataclass
class TriggerData:
    """§B output: per-trigger UTC and travelled distance."""

    sbg_us: np.ndarray
    utc_s: np.ndarray
    dist_m: np.ndarray
    spacing_m: np.ndarray          # len n-1
    spacing_ok_frac: float
    message: str


def build_triggers(trigger_sbg_us: np.ndarray, utc: UtcTable,
                   ruler: "NavTable | DmiTable") -> TriggerData:
    """Per-trigger UTC and along-track distance. `ruler` is the corrected export
    in production, never the raw wheel — see Spec Amendment B1."""
    utc_s = utc.sbg_to_utc(trigger_sbg_us)
    dist = ruler.dist_at(utc_s)
    spacing = np.diff(dist)
    if len(spacing):
        ok = np.abs(spacing - TRIGGER_SPACING_M) <= TRIGGER_SPACING_TOL_M
        frac = float(np.mean(ok))
    else:
        frac = 1.0
    msg = (
        f"{len(trigger_sbg_us)} triggers; spacing {np.median(spacing):.3f} m median, "
        f"{frac * 100:.1f}% within {TRIGGER_SPACING_M} ± {TRIGGER_SPACING_TOL_M} m"
        if len(spacing) else f"{len(trigger_sbg_us)} triggers"
    )
    return TriggerData(
        sbg_us=np.asarray(trigger_sbg_us, dtype=np.int64), utc_s=utc_s, dist_m=dist,
        spacing_m=spacing, spacing_ok_frac=frac, message=msg,
    )


@dataclass
class MatchResult:
    """§C output. trigger_for_image[j] = trigger index matched to image j, or -1."""

    shift_k: int
    d0_m: float
    trigger_for_image: np.ndarray   # int64, len = n images, -1 = unmatched
    residual_m: np.ndarray          # float64, NaN where unmatched (diagnostic fit)
    scale_a: float                  # image-counter vs SBG-DMI scale (diagnostic)
    median_abs_residual_m: float
    n_matched: int
    n_unmatched: int
    message: str


def match_images_to_triggers(images: ImageSet, triggers: TriggerData) -> MatchResult:
    """Ordinal (tail-anchored) matching: trigger i <-> image i + k with
    k = n_images - n_triggers.

    WHY ordinal, not distance-fitting: both sequences ARE the same physical
    trigger events in order, and the image counter is driven by the camera
    system's own distance count while trigger distances come from the SBG DMI.
    The two disagree by a small scale factor (verified ~0.2% over 443 m on run
    20260816.110840), so a constant distance offset cannot hold across a run —
    and once a scale term is allowed, distance data cannot identify the shift
    at all (both sequences are near-linear ramps). The only identifying
    information is the COUNT difference plus the physical fact that recording
    stops with collection (so the sequences end together) while extra images
    come from pre-collection triggers at the START (Derek's observation,
    consistent with every sample run: 83 img / 82 trg, 593 img / 592 trg).

    The distance data is still used — as a diagnostic: a robust linear fit
    trigger_dist ≈ a * image_dist + d0 reports the counter-vs-DMI scale `a`
    and per-image residuals. Large |a - 1| means wheel-calibration mismatch.
    """
    img_d = images.dist_m
    trg_d = triggers.dist_m
    n_img, n_trg = len(img_d), len(trg_d)
    k = n_img - n_trg
    # Images filed into BeforeCollection are still this run's images; they are
    # simply no longer in the camera folder. Counting them keeps the sanity
    # limit measuring what it is for — an unexplained mismatch — instead of
    # firing on housekeeping the app did itself. The SHIFT stays k: the images
    # that remain are the tail, which is exactly what this matcher anchors on.
    unexplained = k + getattr(images, "n_set_aside", 0)
    if abs(unexplained) > MATCH_MAX_SHIFT:
        return MatchResult(
            shift_k=k, d0_m=0.0,
            trigger_for_image=np.full(n_img, -1, dtype=np.int64),
            residual_m=np.full(n_img, np.nan), scale_a=float("nan"),
            median_abs_residual_m=float("nan"), n_matched=0, n_unmatched=n_img,
            message=(
                f"image/trigger count difference is {unexplained:+d} "
                f"(limit ±{MATCH_MAX_SHIFT}) — "
                "run looks inconsistent; refusing to match"
            ),
        )
    trigger_for_image = np.full(n_img, -1, dtype=np.int64)
    for i in range(n_trg):
        j = i + k
        if 0 <= j < n_img:
            trigger_for_image[j] = i
    matched_j = np.where(trigger_for_image >= 0)[0]
    n_matched = len(matched_j)
    n_unmatched = n_img - n_matched

    # Diagnostic linear fit on matched pairs (least squares, then residuals)
    residual = np.full(n_img, np.nan)
    a, d0, med_abs = float("nan"), float("nan"), float("nan")
    if n_matched >= 3:
        x = img_d[matched_j]
        y = trg_d[trigger_for_image[matched_j]]
        a, d0 = np.polyfit(x, y, 1)
        res = y - (a * x + d0)
        residual[matched_j] = res
        med_abs = float(np.median(np.abs(res)))
    return MatchResult(
        shift_k=k, d0_m=float(d0), trigger_for_image=trigger_for_image,
        residual_m=residual, scale_a=float(a), median_abs_residual_m=med_abs,
        n_matched=n_matched, n_unmatched=n_unmatched,
        message=(
            f"ordinal match, shift k = {k:+d} (tail-anchored): {n_matched}/{n_img} images matched"
            + (f", {n_unmatched} unmatched at the start (pre-collection triggers)" if k > 0 else
               (f", {abs(k)} trigger(s) before the first image" if k < 0 else ""))
            + (f"; counter-vs-DMI scale a = {a:.5f}, median |residual| = {med_abs * 1000:.0f} mm"
               if np.isfinite(a) else "")
        ),
    )


@dataclass
class ImageAlignment:
    """§D final product for one run: everything the export and viewer need."""

    images: ImageSet
    triggers: TriggerData
    match: MatchResult
    ptp: PtpOffsetResult
    utc_s: np.ndarray       # per image; NaN if unmatched
    ptp_us: np.ndarray      # per image; -1 if unmatched
    lat_deg: np.ndarray
    lon_deg: np.ndarray
    alt_m: np.ndarray
    dmi_dist_m: np.ndarray  # per image; NaN if unmatched
    # Along-track arm applied to lat/lon/alt, or None when the camera has no
    # calibrated arm and the raw trigger position was stamped instead.
    arm_m: float | None = None


def offset_by_lever_arm(lat_deg, lon_deg, alt_m, heading_deg,
                        x_m: float = 0.0, y_m: float = 0.0, z_m: float = 0.0):
    """Move a nav position out to where a sensor actually sits on the cart.

    The arm is in the BODY frame of AllCalibrations/Leverarms.txt: X forward,
    Y right, Z down, metres, measured from the SBG cover target (= the IMU
    origin). Returns (lat, lon, alt) for the sensor.

    A RIGID BODY offset, not a walk back down the travelled path: at any
    instant the sensor is bolted that far from the IMU and the cart points
    where it points, so where the sensor is depends only on position and
    heading. Walking the trajectory answers "where was the cart earlier",
    which is a different question and wrong on curves and at run starts.

    Heading only — roll and pitch are NOT rotated in. For the Gocators'
    0.3575 m lateral arm, half a degree of roll moves the point 3 mm, while
    the camera mount's own flop is 12 degrees and unmodelled. Adding attitude
    here would be precision this rig cannot support, and it would make the
    laser positions disagree with the camera ones for no measured reason.
    """
    hdg = np.radians(np.asarray(heading_deg, dtype=np.float64))
    lat = np.asarray(lat_deg, dtype=np.float64)
    lon = np.asarray(lon_deg, dtype=np.float64)
    # forward is (cos h, sin h) in (north, east); right is that turned 90 deg
    # clockwise, (-sin h, cos h)
    d_north = x_m * np.cos(hdg) - y_m * np.sin(hdg)
    d_east = x_m * np.sin(hdg) + y_m * np.cos(hdg)
    m_per_deg_lon = EARTH_M_PER_DEG_LAT * np.cos(np.radians(lat))
    return (
        lat + d_north / EARTH_M_PER_DEG_LAT,
        lon + d_east / m_per_deg_lon,
        np.asarray(alt_m, dtype=np.float64) - z_m,   # Z is DOWN, altitude is up
    )


def align_images(images: ImageSet, triggers: TriggerData, match: MatchResult,
                 ptp: PtpOffsetResult, nav: NavTable | None,
                 arm_m: float | None = None,
                 arm_xyz: tuple | None = None) -> ImageAlignment:
    """Position every image.

    ``arm_m`` is how far BEHIND the laser this camera's lens sits
    (``calibration.camera_behind_laser_m``). When given, the stamped position is
    the CENTRE OF THE IMAGE FOOTPRINT rather than the cart's position at the
    moment of capture: the lens is bolted that far back, so its ground centre is
    the cart's position displaced ``arm_m`` opposite the heading at that instant.
    Downstream then needs no lever arm of its own — the position means what a
    reader assumes it means.

    ``arm_m=None`` keeps the raw trigger position (the historical behaviour) and
    is the honest answer for a camera nobody has measured.

    ``arm_xyz`` is the SAME arm as the lever-arm file states it - the full
    (X forward, Y right, Z down) body-frame triple - and when given it is what
    is applied, so every number a human puts in that file reaches the position.
    ``arm_m`` remains for callers that only know the along-track distance; it
    is exactly ``arm_xyz=(-arm_m, 0, 0)``.
    """
    n = len(images)
    utc_s = np.full(n, np.nan)
    dist = np.full(n, np.nan)
    for j in range(n):
        i = int(match.trigger_for_image[j])
        if i >= 0:
            utc_s[j] = triggers.utc_s[i]
            dist[j] = triggers.dist_m[i]
    ptp_us = np.where(np.isnan(utc_s), -1, ((utc_s + ptp.offset_s) * 1e6)).astype(np.int64)
    if nav is not None:
        loc = nav.locate(utc_s)
        lat, lon, alt = loc["lat_deg"], loc["lon_deg"], loc["alt_m"]
        arm = arm_xyz if arm_xyz is not None else (
            (-float(arm_m), 0.0, 0.0) if arm_m else None)
        if arm is not None and any(arm):
            # RIGID BODY offset, NOT a walk back down the travelled path.
            #
            # The lens is bolted arm_m behind the laser in the BODY frame, so at
            # the instant of capture its ground centre is the cart's position
            # displaced arm_m opposite the heading. That is a geometry question
            # answered entirely by where the cart is and which way it points.
            #
            # Walking back along the trajectory instead asks "where was the cart
            # earlier", which is a DIFFERENT question and answers wrong twice:
            #   * on a curve the path bends, so following it does not land on the
            #     rigid offset (~46 mm apart on real 20260824 turns);
            #   * for the FIRST image of a run the cart was never that far back,
            #     so the walk runs into the pre-run settling noise, where the
            #     export accumulates metres of path length while the cart stands
            #     still. Measured on 20260824: every run's first image was short,
            #     by -23 mm to -675 mm (worst 20260824.100512).
            # Heading needs no history at all, so the first image is as good as
            # any other. Verified on all 12 runs of 20260824: heading agrees with
            # the bearing actually travelled to within 0.3 deg, and the offset
            # lands BEHIND in both run directions (the rev-runs are 180 deg
            # apart, which is what would catch a sign error). That check was run
            # when the arm was 1.021 m; what it proved is the SIGN and the
            # rigid-body method, neither of which depends on the magnitude.
            # arm_m counts BEHIND, so it is -X in the body frame. All
            # three axes are passed: Y and Z are zero on this cart today,
            # but they come from the lever-arm file, and a number in that
            # file that nothing reads is worse than no number at all.
            lat, lon, alt = offset_by_lever_arm(
                lat, lon, alt, nav.heading_at(utc_s),
                x_m=float(arm[0]), y_m=float(arm[1]), z_m=float(arm[2]))
    else:
        lat = np.full(n, np.nan)
        lon = np.full(n, np.nan)
        alt = np.full(n, np.nan)
    return ImageAlignment(
        images=images, triggers=triggers, match=match, ptp=ptp,
        utc_s=utc_s, ptp_us=ptp_us, lat_deg=lat, lon_deg=lon, alt_m=alt, dmi_dist_m=dist,
        arm_m=arm_m if arm_m is not None else (
            -float(arm_xyz[0]) if arm_xyz is not None else None),
    )


def gocator_distances(gocator: GocatorIndex, ptp: PtpOffsetResult,
                      ruler: "NavTable | DmiTable") -> np.ndarray:
    """Along-track distance of every Gocator profile (via the solved PTP offset).

    This is the ground position of the LASER. A camera's ground position for
    the same instant is its own lever arm BEHIND this — see
    calibration.camera_ground_center_m() and CAMERA_BEHIND_LASER_M. Not
    repeated as a number here: this line said "1.27 m" for three weeks after
    the Rear arm became 0.928 m.
    """
    utc = gocator.ptp_us.astype(np.float64) / 1e6 - ptp.offset_s
    return ruler.dist_at(utc)


def profiles_in_window(profile_dist_m: np.ndarray, center_m: float, half_span_m: float) -> np.ndarray:
    """Indices of profiles within [center - half_span, center + half_span]."""
    lo, hi = center_m - half_span_m, center_m + half_span_m
    return np.where((profile_dist_m >= lo) & (profile_dist_m <= hi))[0]
