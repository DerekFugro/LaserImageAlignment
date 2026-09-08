"""Pave camera intrinsic calibration: loading, undistortion, mm/px scale,
and the cart's lever arms.

Calibration YAMLs live in a calibrations folder (default
F:\\Sidewalk\\002_App\\AllCalibrations); the newest PAVE_*.yaml wins.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path

import numpy as np
import yaml

from .formats import ParseError

# --- Pave geometry: MEASURED, not nominal (Derek, 2026-08-20) ---------------
#
# Derek painted marks every 5 m, drove the run, then clicked each mark in the
# Pave image with ALL lever-arm and pitch/roll corrections switched OFF.
# Mark-to-mark result over 55 m: 5 m +/- 41 mm (1 sigma), max 73 mm, mean bias
# -17 mm (-0.35%), no drift.
#
# That test settles the lens height. At the old nominal 1.700 m the bias would
# have been +2% -- a consistent +100 mm on every 5 m interval, all one sign.
# It was not there; the errors alternate sign. So 1.667 m is right.
PAVE_LENS_HEIGHT_M = 1.667
NOMINAL_LENS_HEIGHT_M = 1.70        # mounting-table figure. Superseded; kept
                                    # so the old number is recognisable.

# EFFECTIVE camera-behind-laser lever arm, measured 2026-08-20 from the only
# observation that pins it: a joint located INDEPENDENTLY in the image and in
# the laser profiles (Derek's camlaser_labels.csv, run 20260817.175605).
#
#   5 labels, two independent rulers, same answer:
#       via the Qinertia nav ruler : -229 mm  (SD 39)
#       via the Gocator encoder    : -225 mm  (SD 42)   <- no GNSS at all
#   => arm 1.021 m  (standard error ~19 mm)
#   Corroborated by an earlier camera-app test that also landed near 1.01 m.
#
# WHY THIS AND NOT THE 1.270 m MOUNTING FIGURE: the 249 mm difference is what
# ~8.5 deg of forward camera tilt produces at a 1.667 m height. It does not
# matter which of tilt / mounting offset / constant cart pitch it really is --
# all three produce the SAME constant shift, and an effective arm measured from
# features visible in both streams absorbs all of them at once. That is also
# why pitch stays deferred: modelling it would double-count.
#
# WHY NOT the 1.246 m implied by ground_truth_samples.json: that measurement
# depends on where the laser started recording, which depends on where the cart
# began rolling -- and that varied by up to 450 mm across the revrus3 runs. It
# was measuring the start position, not the arm.
#
# CAVEAT: if the cause IS tilt, the effective arm scales with lens height
# (H*tan(theta)), so it must be re-measured if the mount or ride height changes.
# Residual nonlinearity from an 8.5 deg tilt is ~40 mm at the frame edges --
# below the ~40 mm labelling noise floor, so it is not modelled.
# SUPERSEDED 2026-08-28 (Derek). 1.021 came from 5 clicked labels; it is 95 mm
# too long. Measured again from the CALIBRATION BARS, which need no judgement
# about which laser scan shows the feature -- the cart drives over the bar and
# both lasers record a 6.35 mm step at the same station (core.bar
# the calibration bars, since removed from this app):
#
#   Derek's four-corner clicks + laser bars   n=10 : 0.9276 +/- 0.0261 m (sem 8)
#   automated bar sweep, 2 collections        n=16 : 0.9245 +/- 0.0251 m
#   lever_solution.json, painted marks vs INS       : 0.9372 m  (independent)
#
# Cross-checks that make this trustworthy:
#   * the SAME bar clicked in two consecutive images agrees to 7-8 mm, which
#     only happens if the row->ground scale is right;
#   * forward runs give +0.9303, reverse runs +0.9148 -- both POSITIVE, so the
#     sign is measured, not assumed. A body-fixed distance cannot flip when the
#     cart is turned around, and it did not.
#
# SIGN: positive means BEHIND the laser, as camera_ground_center_m() subtracts
# it. Note lever_solution.json states the same geometry as forward_m = -0.9372
# because its axis is +forward; do not copy that minus sign into this constant.
#
# WHAT IT INCLUDES: the ground point on the optical axis (image row = cy), so
# any camera pitch is absorbed here exactly as it was in the 1.021 figure. Do
# not add a separate pitch term on top -- that would double-count.
PAVE_CAM_BEHIND_LASER_M = 0.928
NOMINAL_CAM_BEHIND_LASER_M = 1.27

# --- Per-camera along-track lever arm -----------------------------------------
#
# How far BEHIND the laser each lens sits. This is what turns a trigger position
# into the ground the photo actually shows, so it is what the stamped GPS must be
# corrected by: a photo's position should be the CENTRE OF ITS FOOTPRINT, not the
# cart's position when the shutter fired.
#
# One entry per camera, and None means NOT CALIBRATED. None is deliberately not
# 0.0: zero would be a claim that the lens is level with the laser, and stamping
# a confidently wrong position on a client deliverable is worse than stamping the
# uncorrected one and saying so. An uncalibrated camera keeps the old behaviour
# and the batch report says which cameras were left uncorrected.
#
#   Rear : 0.928 m (see PAVE_CAM_BEHIND_LASER_M above).
#   ROW  : 1.69 m, measured 2026-08-28 by marching the calibration bars through
#          consecutive ROW frames on run 20260824.101724: the laser fixes each
#          bar's station, and the bar crosses ROW's centre row 1.685 m behind
#          the cart (bar 3 cross-check: 1.684 m — 1 mm apart). This also
#          settled the old "tilt SIGN undetermined" flag: the bar appears
#          AFTER the cart passes it and climbs the frame, so ROW looks
#          BACKWARD, arm positive-behind like Rear. For an oblique camera the
#          number means: the ground at the image CENTRE row trails the cart by
#          this much. ROUGH — two bars, one run, ±0.05 m; firm it up against
#          the laser bar stations in runs 101313/101417/101618. Full record:
#          AllCalibrations/LEVER_ARMS.md and EXTRINSIC_ROW_262503744.yaml.
ROW_CAM_BEHIND_LASER_M = 1.69
CAMERA_BEHIND_LASER_M: dict[str, float | None] = {
    "Rear": PAVE_CAM_BEHIND_LASER_M,
    "ROW": ROW_CAM_BEHIND_LASER_M,
}


# --- The lever-arm file -------------------------------------------------------
#
# AllCalibrations/LaserImageAlignmentLeverArms.md is Derek's file and the app
# reads it. It is a REQUIRED INPUT, like eventOutA.txt: a run whose file is
# missing, unreadable or empty is not processed, and a camera or laser that is
# on disk but not in the file holds its own deliverable back (Derek,
# 2026-09-02). The constants above are history, not a fallback. Until that day
# a missing file quietly fell back to them, and a batch on 20260821 wrote every
# position with arms the file no longer contained while its report said "every
# deliverable was written".
#
# The format is deliberately plain, so a human can change a number without
# knowing anything about this app:
#
#     ## Rear camera
#
#     X  -0.928
#     Y   0
#     Z   0
#
# Body frame, metres, from the SBG cover target: X forward, Y right, Z down.
# A heading ending in "camera" is a camera, keyed by its first word;
# "Gocator left" / "Gocator right" are the lasers.
LEVER_ARMS_FILENAME = "LaserImageAlignmentLeverArms.md"

_HEADING_RE = re.compile(r"^#+\s*(.+?)\s*$")
# The number may be followed by a unit or a remark ("X  -0.928 m", "X -0.928
# (was -1.27)"). Anchored at the end of the line, such a line matched NOTHING
# and the axis silently became 0.0 - the camera stamped at the IMU because
# somebody wrote "m" after the number (found 2026-09-02).
_AXIS_RE = re.compile(r"^\s*([XYZ])\s+([-+]?[0-9]*\.?[0-9]+)(?![0-9.])", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+)\s*$")
# not a camera name, so it can never collide with one
_LENS_KEY = "#lens_height"


def _lens_heading(low: str) -> bool:
    """Is this heading the lens-height section?

    WHY THIS IS MORE THAN `startswith(LENS_HEIGHT_HEADING)` (found 2026-09-04):
    Derek had been told to keep prose out of this file, so he trimmed the
    heading to plain `## Rear`. That matched nothing, the 1.719 under it was
    dropped, and `lens_height_m()` went on returning the built-in 1.667 with no
    warning anywhere - the file said one thing and the app did another. Exactly
    the silent-override we removed for the arms two days earlier.

    `low == "rear"` is an EXACT match on purpose: `startswith("rear")` would
    also swallow `## Rear camera` and turn the Rear lever arm into the lens
    height, which WOULD move written positions. The equality can never collide
    with a camera heading, because a camera heading always has a second word.
    """
    return low == "rear" or "lens height" in low


def parse_lever_arms(text: str) -> dict[str, tuple[float, float, float]]:
    """{name: (x, y, z)} from the file's text. Unknown headings are ignored."""
    return parse_lever_arms_checked(text)[0]


def parse_lever_arms_checked(
        text: str) -> tuple[dict[str, tuple[float, float, float]], list[str]]:
    """({name: (x, y, z)}, complaints about lines that were thrown away).

    Names are normalised to what the rest of the app already uses: a camera
    heading gives its own first word ("Rear camera" -> "Rear"), and a Gocator
    heading gives the side letter ("Gocator left" -> "L"). An axis line missing
    from a section leaves that axis at 0.0, which is what the file's own zeros
    say anyway.

    The second half of the return is the point: a number the parser did not
    understand used to vanish in silence. Now it is named, so a heading nobody
    recognises shows up as a complaint instead of as an old constant quietly
    staying in force.
    """
    out: dict[str, tuple[float, float, float]] = {}
    warnings: list[str] = []
    name, axes, head = "", {}, ""

    def flush():
        if name:
            out[name] = (float(axes.get("X", 0.0)),
                         float(axes.get("Y", 0.0)),
                         float(axes.get("Z", 0.0)))

    for line in text.splitlines():
        m = _HEADING_RE.match(line) if line.lstrip().startswith("#") else None
        if m:
            flush()
            axes = {}
            head = m.group(1).strip()
            low = head.lower()
            if low.startswith("gocator"):
                name = "L" if "left" in low else "R" if "right" in low else ""
            elif low.endswith("camera"):
                name = head.split()[0]
            elif _lens_heading(low):
                name = _LENS_KEY
            else:
                name = ""
            continue
        if name == _LENS_KEY:
            m = _BARE_NUMBER_RE.match(line)
            if m:
                axes["X"] = m.group(1)
            continue
        m = _AXIS_RE.match(line)
        if m and name:
            axes[m.group(1).upper()] = m.group(2)
            continue
        # A number sitting under a heading this parser does not recognise. It
        # is being discarded, and whatever it was meant to set keeps its old
        # value - so say so rather than let it disappear.
        if not name and _BARE_NUMBER_RE.match(line):
            where = f"under heading '{head}'" if head else "before any heading"
            warnings.append(
                f"ignored the number {line.strip()} {where} - no section of "
                f"that name is understood, so nothing was set from it")
    flush()
    return out, warnings


def _decode(raw: bytes) -> str:
    """Text out of a file a person edits in whatever editor they have.

    This one is Derek's, and Notepad still offers ANSI and UTF-16 in its Save
    dialog. `read_text(encoding="utf-8")` on an ANSI-saved file raises
    UnicodeDecodeError, which is a ValueError, which the caller's `except
    OSError` did not catch - so a degree sign typed into the lever-arm file
    killed the whole batch before it wrote a single report (found 2026-09-02).

    cp1252 is last and decodes any byte at all, so this never raises. Guessing
    is right here: the numbers are plain ASCII, and only a comment or a unit
    symbol can be the thing that will not decode.
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def load_lever_arms_file(path) -> dict[str, tuple[float, float, float]]:
    """{name: (x, y, z)} from the lever-arm file at `path`. Raises ParseError
    when the file is missing, unreadable, or parses to no arms at all.

    READ EVERY TIME, deliberately not cached. The first version keyed a cache
    on (path, mtime) and it was wrong: mtime resolution on Windows is coarse
    enough that two edits inside the same tick share a timestamp, so the second
    was ignored and the app went on using numbers the file no longer contained.
    Some editors also write a file back with its old mtime. Either way the
    failure is silent, and it moves where a photograph says it was taken.

    The file is a few hundred bytes and is read a handful of times per run.
    Correctness is worth more than the read.
    """
    path = Path(path)
    if not path.is_file():
        raise ParseError(path, "lever-arm file not found")
    try:
        arms = parse_lever_arms(_decode(path.read_bytes()))
    except OSError as exc:
        raise ParseError(path, f"cannot read lever-arm file: {exc}")
    # A file that parsed to nothing is a file somebody broke. Refuse rather
    # than silently stamping every sensor at the IMU.
    if not arms:
        raise ParseError(path, "no lever arms in the file (expected headings "
                               "like '## Rear camera' with X/Y/Z lines)")
    return arms


def lever_arm_warnings(path) -> list[str]:
    """Lines the lever-arm file at `path` contains that the parser threw away.

    Empty for a healthy file, and empty (not an exception) for a file that is
    missing or unreadable - the callers of this are reporting, and a run whose
    file is absent already fails loudly elsewhere. This exists so the batch
    report can say 'your 1.719 is being ignored' instead of the app quietly
    using something else."""
    try:
        path = Path(path)
        if not path.is_file():
            return []
        return parse_lever_arms_checked(_decode(path.read_bytes()))[1]
    except OSError:
        return []


def load_lever_arms(calibrations_dir=None) -> tuple[dict, str]:
    """({name: (x, y, z)}, the file it came from). Raises ParseError when the
    calibrations folder has no readable lever-arm file - there is no fallback."""
    if not calibrations_dir:
        raise ParseError(LEVER_ARMS_FILENAME, "no calibrations folder given")
    path = Path(calibrations_dir) / LEVER_ARMS_FILENAME
    return load_lever_arms_file(path), str(path)


# The one SCALAR in the file. Written as a heading and a single number:
#
#     ## Rear lens height above the pavement
#
#     1.667
#
# Not a lever arm - it is a height above the GROUND, not an offset from the
# IMU - but it is a physical measurement of this cart that the app applies, so
# it belongs where a human can change it. It sets millimetres-per-pixel, the
# two-point measure, the ChArUco board check and the width of the laser-profile
# window. It does NOT touch any written position.
LENS_HEIGHT_HEADING = "rear lens height"


def lens_height_m(calibrations_dir=None) -> float:
    """Rear lens height above the pavement, from the file or the built-in.

    The one reader that still falls back: this sets a DISPLAY scale (mm per
    pixel, the profile window), never a written position, and the viewer has
    to draw something for a run whose file is missing - the QC panel is where
    that run's FAIL is shown.

    NO FOLDER GIVEN falls back to the installation's own, not to the constant
    (2026-09-08). Every caller in the app passes one, so this changed nothing
    today - but "called with no argument" is not a reason to ignore a file that
    is sitting right there with a number in it, and that is the shape of the
    two bugs this file has already had. The import is local because `config`
    reads `discovery` for its default, and `discovery` would otherwise close a
    circle back to here."""
    if not calibrations_dir:
        from .config import calibrations_dir as _configured
        calibrations_dir = _configured()
    try:
        v = load_lever_arms(calibrations_dir)[0].get(_LENS_KEY)
    except ParseError:
        return PAVE_LENS_HEIGHT_M
    return float(v[0]) if v else PAVE_LENS_HEIGHT_M


def camera_lever_arm_m(camera_name, calibrations_dir=None):
    """(X, Y, Z) body-frame arm for a camera, or None when it is not listed.

    None, not zeros: zero would be a claim that the lens sits exactly on the
    IMU, and stamping a confidently wrong position on a client deliverable is
    worse than stamping the uncorrected one and saying so.
    """
    if not camera_name:
        return None
    return load_lever_arms(calibrations_dir)[0].get(camera_name)
    # (raises ParseError when there is no file - see load_lever_arms)


def camera_behind_laser_m(camera_name: str | None,
                          calibrations_dir=None) -> float | None:
    """How far BEHIND the laser a camera's lens sits, or None when the camera
    is not listed in the lever-arm file.

    The file states the arm as a body-frame X, which points FORWARD; this
    counts backward, so it is -X. Unknown names return None so a camera added
    later is left uncorrected until somebody measures it, rather than silently
    inheriting the Rear arm.
    """
    arm = camera_lever_arm_m(camera_name, calibrations_dir)
    return None if arm is None else -float(arm[0])



def camera_ground_center_m(trigger_dist_m: float, calibrations_dir=None) -> float:
    """Ground coordinate (in laser-passage DMI meters) under the rear lens
    when the cart's DMI reads trigger_dist_m. The camera trails the laser by
    its arm, so it sits over ground the laser scanned earlier.

    The figure is named, not written out, because it has already moved twice:
    1.270 m nominal, then 1.021 m from clicked labels, now 0.928 m from the
    calibration bars. A comment quoting a number goes stale silently; this one
    said "1.27 m" until 2026-08-31, three weeks after the value changed.

    PASS calibrations_dir. Without it this falls back to the built-in constant,
    and then the VIEWER - which is the only place the arm can be judged by eye -
    goes on using the old number while the written EXIF uses the file. Derek
    would edit the file, look at the plot, see nothing move, and reasonably
    conclude the file does nothing (found 2026-09-02, the same day he asked
    what to change the arm to).
    """
    try:
        arm = camera_behind_laser_m("Rear", calibrations_dir)
    except ParseError:
        arm = None          # display only; the run's QC row carries the FAIL
    return trigger_dist_m - (PAVE_CAM_BEHIND_LASER_M if arm is None else arm)


# --- Gocator lever arms -------------------------------------------------------
#
# (X forward, Y right, Z down) metres from the SBG cover target, which is the
# IMU origin. Straight out of AllCalibrations/Leverarms.txt.
#
# The two lasers straddle the cart: left 0.3575 m to port, right 0.3575 m to
# starboard, 0.715 m apart. Until 2026-08-31 neither arm was applied — both
# sensors were stamped with the IMU's own position, so a left-laser profile
# and the right-laser profile beside it claimed the SAME point on the ground,
# each wrong by 0.3575 m in opposite directions. Nothing downstream could
# tell the two wheelpaths apart.
#
# The 0.715 m separation is corroborated independently: ReverseRunProcessor's
# Rev-runBoresight_ppk.json carries "separation_mm": 715.0 and uses it to turn
# the two lasers' heights into cross slope.
#
# X and Z are zero, so today only Y moves anything — but the arms are stored
# in full so a re-measure drops straight in.
GOCATOR_LEVER_ARM_M: dict[str, tuple[float, float, float]] = {
    "L": (0.0, -0.3575, 0.0),
    "R": (0.0, +0.3575, 0.0),
}


def gocator_lever_arm_m(side: str, calibrations_dir=None):
    """(X, Y, Z) body-frame arm for a Gocator side, or None when the file has
    no section for it. None, not zeros: a laser with no arm holds its own GPS
    columns back (qc content.gocator_*_arm) rather than being stamped at the
    IMU, 0.3575 m from where it is."""
    arms = load_lever_arms(calibrations_dir)[0]
    return arms.get(str(side).upper())


def ground_to_image_row(ground_m: float, cam_center_m: float, cy_px: float,
                        mm_per_px: float) -> float:
    """Pixel row (0 = top) where ground coordinate `ground_m` appears in a
    nadir rear image centered (row cy_px) on `cam_center_m`.

    Orientation (verified with Derek): bottom of the image is closest to the
    cart, i.e. toward the direction of travel — larger ground coordinate is
    further DOWN the image. Exact on the undistorted image; within a few px
    on the raw image."""
    return cy_px + (ground_m - cam_center_m) * 1000.0 / mm_per_px


@dataclass
class CameraCalibration:
    serial_number: str
    calibration_datetime: str
    rms_reprojection_error: float
    verdict: str
    camera_matrix: np.ndarray  # 3x3
    dist_coeffs: np.ndarray    # (5,)
    width: int
    height: int
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> "CameraCalibration":
        path = Path(path)
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ParseError(path, f"cannot load calibration YAML: {exc}")
        try:
            cm = doc["camera_matrix"]
            dc = doc["dist_coeffs"]
            res = doc["image_resolution"]
            # The `board` block is not read any more - the ChArUco check went
            # with the calibration tools - but its PRESENCE is still what tells
            # a real PAVE calibration from a "refined" one, which lacks it and
            # has been seen with a 6x-wrong focal length. find_pave_calibration
            # filters those by filename; this is the second lock.
            doc["board"]
            cal = cls(
                serial_number=str(doc["serial_number"]),
                calibration_datetime=str(doc["calibration_datetime"]),
                rms_reprojection_error=float(doc["rms_reprojection_error"]),
                verdict=str(doc.get("verdict", "")),
                camera_matrix=np.asarray(cm["data"], dtype=np.float64).reshape(cm["rows"], cm["cols"]),
                dist_coeffs=np.asarray(dc["data"], dtype=np.float64).reshape(-1),
                width=int(res["width"]),
                height=int(res["height"]),
                path=str(path),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(path, f"calibration YAML missing/invalid field: {exc}")
        if cal.camera_matrix.shape != (3, 3):
            raise ParseError(path, f"camera_matrix is {cal.camera_matrix.shape}, expected (3, 3)")
        if cal.dist_coeffs.size != 5:
            raise ParseError(path, f"dist_coeffs has {cal.dist_coeffs.size} values, expected 5")
        return cal

    @property
    def fx(self) -> float:
        return float(self.camera_matrix[0, 0])

    @property
    def fy(self) -> float:
        return float(self.camera_matrix[1, 1])

    def mm_per_px(self, lens_height_m: float = PAVE_LENS_HEIGHT_M) -> float:
        """Ground sample distance ACROSS track (image columns), nadir, nominal."""
        return lens_height_m * 1000.0 / self.fx

    def mm_per_px_y(self, lens_height_m: float = PAVE_LENS_HEIGHT_M) -> float:
        """Ground sample distance ALONG track (image rows), nadir, nominal.

        Rows are the along-track axis, so anything mapping ground distance to a
        pixel row must use fy, not fx. (On the Pave they differ by 0.02%, but
        the next camera may not be so kind.)
        """
        return lens_height_m * 1000.0 / self.fy

    def along_track_half_span_m(self, lens_height_m: float = PAVE_LENS_HEIGHT_M) -> float:
        """Half the along-track ground footprint of one image, in metres.

        This is the radius of the Gocator-profile window shown beside an image.
        Derived from the calibration so it tracks a lens or sensor change; the
        Pave gives 1860 px x 0.6165 mm/px / 2 = 0.573 m.
        """
        return self.height * self.mm_per_px_y(lens_height_m) / 2000.0


EXCLUDED_CALIBRATION_MARKERS = ("refined",)


def find_pave_calibration(calibrations_dir: Path) -> Path | None:
    """Newest PAVE_*.yaml under the calibrations folder (recursive).

    *Refined* files are IGNORED (Derek, 2026-08-19): they are produced by a
    separate bar-run tool, have been observed with a 6x-wrong focal length and
    without the required `board` block, and must never be picked up here.
    """
    calibrations_dir = Path(calibrations_dir)
    if not calibrations_dir.is_dir():
        return None
    candidates = [p for p in calibrations_dir.rglob("PAVE_*.yaml")
                  if not any(m in p.name.lower() for m in EXCLUDED_CALIBRATION_MARKERS)]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


class Undistorter:
    """Cached undistortion maps for one calibration."""

    def __init__(self, cal: CameraCalibration):
        import cv2  # local import: cv2 is heavy; keep module importable without it

        self.cal = cal
        self._map1, self._map2 = cv2.initUndistortRectifyMap(
            cal.camera_matrix, cal.dist_coeffs, None, cal.camera_matrix,
            (cal.width, cal.height), cv2.CV_16SC2,
        )

    def undistort(self, image_bgr: np.ndarray) -> np.ndarray:
        import cv2

        if image_bgr.shape[1] != self.cal.width or image_bgr.shape[0] != self.cal.height:
            raise ValueError(
                f"image is {image_bgr.shape[1]}x{image_bgr.shape[0]}, "
                f"calibration expects {self.cal.width}x{self.cal.height}"
            )
        return cv2.remap(image_bgr, self._map1, self._map2, interpolation=cv2.INTER_LINEAR)




