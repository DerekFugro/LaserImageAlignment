"""Body-frame lever arms: putting each sensor where it actually is.

AllCalibrations/Leverarms.txt gives every sensor an (X forward, Y right,
Z down) offset from the SBG cover target, which is the IMU origin. The
cameras' along-track arm has been applied since 2026-08-20. The Gocators'
lateral arms were NOT, until 2026-08-31: both lasers were stamped with the
IMU's own position, so a left-laser profile and the right-laser profile
beside it claimed the same point on the ground, each wrong by 0.3575 m in
opposite directions. The two wheelpaths were indistinguishable.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.alignment import offset_by_lever_arm
from core.calibration import GOCATOR_LEVER_ARM_M, gocator_lever_arm_m
from core import discovery as disc
from core.formats import EARTH_M_PER_DEG_LAT

LAT0, LON0, ALT0 = 43.436, -80.315, 265.0


def metres_apart(a, b):
    """Ground distance between two (lat, lon) points, in metres."""
    dlat = (b[0] - a[0]) * EARTH_M_PER_DEG_LAT
    dlon = (b[1] - a[1]) * EARTH_M_PER_DEG_LAT * np.cos(np.radians(a[0]))
    return float(np.hypot(dlat, dlon))


class TestOffsetGeometry:
    @pytest.mark.parametrize("heading", [0.0, 45.0, 90.0, 180.0, 270.0, 331.7])
    def test_a_right_arm_lands_to_the_right_whatever_the_heading(self, heading):
        """The offset is body-fixed, so turning the cart must turn the offset
        with it. A bearing that stays put would mean the arm was applied in
        the world frame instead."""
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, heading, y_m=1.0)
        d_north = (lat - LAT0) * EARTH_M_PER_DEG_LAT
        d_east = (lon - LON0) * EARTH_M_PER_DEG_LAT * np.cos(np.radians(LAT0))
        bearing = np.degrees(np.arctan2(d_east, d_north)) % 360.0
        assert bearing == pytest.approx((heading + 90.0) % 360.0, abs=0.01)
        assert metres_apart((LAT0, LON0), (lat, lon)) == pytest.approx(1.0, abs=1e-3)

    def test_north_and_east_go_the_right_way(self):
        """Pinned in plain terms so a sign flip cannot hide behind trig:
        heading north, 'right' is east and 'forward' is north."""
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, y_m=1.0)
        assert lon > LON0 and lat == pytest.approx(LAT0, abs=1e-9)
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, x_m=1.0)
        assert lat > LAT0 and lon == pytest.approx(LON0, abs=1e-9)

    def test_z_is_down_so_it_subtracts_from_altitude(self):
        _, _, alt = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, z_m=1.0)
        assert alt == pytest.approx(ALT0 - 1.0)

    def test_a_zero_arm_changes_nothing(self):
        lat, lon, alt = offset_by_lever_arm(LAT0, LON0, ALT0, 137.0)
        assert (lat, lon, alt) == pytest.approx((LAT0, LON0, ALT0))

    def test_it_works_on_arrays(self):
        h = np.array([0.0, 90.0, 180.0])
        lat, lon, alt = offset_by_lever_arm(
            np.full(3, LAT0), np.full(3, LON0), np.full(3, ALT0), h, y_m=0.3575)
        assert len(lat) == len(lon) == len(alt) == 3
        for i in range(3):
            assert metres_apart((LAT0, LON0), (lat[i], lon[i])) == \
                pytest.approx(0.3575, abs=1e-3)


class TestGocatorArms:
    def test_the_two_lasers_are_the_measured_distance_apart(self):
        """0.715 m, straddling the cart. Corroborated independently by
        ReverseRunProcessor's separation_mm: 715.0, which it uses to turn the
        two lasers' heights into cross slope."""
        left = offset_by_lever_arm(LAT0, LON0, ALT0, 30.0, *GOCATOR_LEVER_ARM_M["L"])
        right = offset_by_lever_arm(LAT0, LON0, ALT0, 30.0, *GOCATOR_LEVER_ARM_M["R"])
        assert metres_apart(left[:2], right[:2]) == pytest.approx(0.715, abs=1e-3)

    def test_left_really_is_to_the_left(self):
        """The regression that would be invisible in a report but wrong in
        every deliverable: swap the sign and the wheelpaths trade places."""
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0,
                                          *GOCATOR_LEVER_ARM_M["L"])
        assert lon < LON0, "heading north, the left laser must be to the west"
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0,
                                          *GOCATOR_LEVER_ARM_M["R"])
        assert lon > LON0, "heading north, the right laser must be to the east"

    def test_the_arms_are_the_ones_in_leverarms_txt(self):
        assert GOCATOR_LEVER_ARM_M["L"] == (0.0, -0.3575, 0.0)
        assert GOCATOR_LEVER_ARM_M["R"] == (0.0, +0.3575, 0.0)

    def test_an_unknown_side_is_none_not_zeros(self, synth_cal_dir):
        """Not an invented arm, and not zeros either: a side with no arm
        holds its own GPS columns (qc content.gocator_*_arm)."""
        assert gocator_lever_arm_m("middle", synth_cal_dir) is None

    def test_lower_case_sides_work(self, synth_cal_dir):
        assert gocator_lever_arm_m("l", synth_cal_dir) == GOCATOR_LEVER_ARM_M["L"]


class TestCameraArmStillBehaves:
    """The camera path now shares this helper. Same answer as before: the
    lens is BEHIND, so it lands opposite the heading."""

    def test_behind_means_opposite_the_heading(self):
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, x_m=-0.928)
        assert lat < LAT0, "heading north, a camera behind the IMU is to the south"
        assert metres_apart((LAT0, LON0), (lat, lon)) == pytest.approx(0.928, abs=1e-3)

    def test_reversing_the_cart_reverses_the_offset(self):
        """A body-fixed distance cannot flip when the cart is turned around -
        the same physical spot on the cart, opposite bearings."""
        north = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, x_m=-0.928)
        south = offset_by_lever_arm(LAT0, LON0, ALT0, 180.0, x_m=-0.928)
        assert north[0] < LAT0 < south[0]
        assert metres_apart(north[:2], (LAT0, LON0)) == \
            pytest.approx(metres_apart(south[:2], (LAT0, LON0)), abs=1e-6)


class TestALocatedFolderStillGetsItsArm:
    """The silent-wrong-data path found in the pre-ship audit, 2026-09-01.

    When a run's image folder is supplied through Locate... the override store
    filled run.paths[KEY_IMAGES] and stopped there. run.cameras stayed empty,
    so pipeline.process_run built no camera_alignments, and BOTH export_csv and
    the batch fell back to result.alignment - which is computed with no arm at
    all. That run's JPEGs and CSV got the cart's position instead of the image
    footprint's, 0.928 m out on Rear and 1.69 m on ROW, with no
    align.camera.*.arm row anywhere to say so.
    """

    def store(self, tmp_path, run, folder):
        from core.discovery import OverrideStore
        s = OverrideStore(tmp_path / "ov.json")
        s.set(run.root, run.run_id, disc.KEY_IMAGES, folder)
        return s

    def run_paths(self, tmp_path):
        cam = tmp_path / "elsewhere" / "Rear"
        cam.mkdir(parents=True)
        (cam / "000000000750.jpg").write_bytes(b"")
        return disc.RunPaths(run_id="20260816.150000", root=str(tmp_path)), cam

    def test_the_located_folder_becomes_a_camera(self, tmp_path):
        run, cam = self.run_paths(tmp_path)
        self.store(tmp_path, run, cam).apply(run)
        assert run.get(disc.KEY_IMAGES) == cam
        assert run.cameras == {"Rear": cam}, \
            "no camera means no lever arm is ever applied to this run"

    def test_the_camera_keeps_the_folder_name_so_the_arm_is_found(self, tmp_path, synth_cal_dir):
        """The arm is looked up BY CAMERA NAME, so the key has to be the
        folder's name and nothing else."""
        from core.calibration import camera_behind_laser_m
        run, cam = self.run_paths(tmp_path)
        self.store(tmp_path, run, cam).apply(run)
        name = next(iter(run.cameras))
        assert camera_behind_laser_m(name, synth_cal_dir) is not None
        assert camera_behind_laser_m(name, synth_cal_dir) > 0

    def test_a_camera_already_discovered_is_not_overwritten(self, tmp_path):
        """Locate... only fills what is missing; a real discovered folder wins."""
        run, cam = self.run_paths(tmp_path)
        real = tmp_path / "Images" / "20260816.150000" / "Rear"
        real.mkdir(parents=True)
        run.cameras["Rear"] = real
        self.store(tmp_path, run, cam).apply(run)
        assert run.cameras["Rear"] == real


class TestOnePositionPerImage:
    """The viewer and the EXIF must be the SAME number, not two that agree.

    Derek, 2026-09-01: "it is the only way to ensure alignment."

    process_run used to build the alignment twice - once for the viewer with no
    lever arm, then once per camera with that camera's arm. Same trigger, same
    instant, same trajectory row; only the last step differed. So the readout's
    lat/lon sat 0.928 m behind the value stamped into the same photograph, and
    nothing on screen said which one you were looking at.
    """

    def result(self, synth_run_root, synth_cal_dir):
        from core.pipeline import process_run
        runs = disc.discover_runs(synth_run_root, calibrations_dir=synth_cal_dir,
                                  registered_only=False)
        return process_run(runs[0])

    def test_the_viewer_reads_the_primary_camera_s_own_alignment(
            self, synth_run_root, synth_cal_dir):
        res = self.result(synth_run_root, synth_cal_dir)
        assert res.alignment is not None
        assert res.camera_alignments
        primary = next(n for n, im in res.parsed.cameras.items()
                       if im is res.parsed.images)
        assert res.alignment is res.camera_alignments[primary], \
            "the same object, so the two can never drift apart"

    def test_the_readout_and_the_exif_are_the_same_position(
            self, synth_run_root, synth_cal_dir):
        """What the bug actually looked like: two arrays, one arm apart."""
        res = self.result(synth_run_root, synth_cal_dir)
        primary = next(n for n, im in res.parsed.cameras.items()
                       if im is res.parsed.images)
        shown = res.alignment                     # gui/main_window.py reads this
        written = res.camera_alignments[primary]  # batch.py writes this
        assert np.array_equal(shown.lat_deg, written.lat_deg, equal_nan=True)
        assert np.array_equal(shown.lon_deg, written.lon_deg, equal_nan=True)

    def test_the_shown_position_really_has_the_arm_in_it(
            self, synth_run_root, synth_cal_dir):
        """Not just 'the two agree' - they must agree on the ARMED value. An
        unarmed pair would pass the test above and still be wrong."""
        from core.alignment import align_images
        from core.calibration import camera_behind_laser_m

        res = self.result(synth_run_root, synth_cal_dir)
        primary = next(n for n, im in res.parsed.cameras.items()
                       if im is res.parsed.images)
        arm = camera_behind_laser_m(primary, synth_cal_dir)
        assert arm, "this test needs a camera with a calibrated arm"

        ptp = res.ptp_l or res.ptp_r
        unarmed = align_images(res.parsed.images, res.triggers,
                               res.camera_matches[primary], ptp, res.parsed.nav)
        ok = np.isfinite(res.alignment.lat_deg) & np.isfinite(unarmed.lat_deg)
        assert ok.any()
        moved = metres_apart(
            (res.alignment.lat_deg[ok][0], res.alignment.lon_deg[ok][0]),
            (unarmed.lat_deg[ok][0], unarmed.lon_deg[ok][0]))
        assert moved == pytest.approx(arm, abs=0.02), \
            "the shown position is the cart, not the footprint centre"


REAL_FILE = """# LaserImageAlignment lever arms

Metres from the SBG cover target (the IMU origin).
X forward, Y right, Z down.

Edit the numbers. The app reads this file.


## Rear camera

X  -0.928
Y   0
Z   0


## ROW camera

X  -1.69
Y   0
Z   0


## Gocator left

X   0
Y  -0.3575
Z   0


## Gocator right

X   0
Y  +0.3575
Z   0
"""


class TestTheFileIsInCharge:
    """AllCalibrations/LaserIMgeAlignmentLeverArms.md is Derek's file and the
    app reads it. A number he changes there has to reach the positions, or the
    file is decoration."""

    def write(self, tmp_path, text=REAL_FILE):
        from core.calibration import LEVER_ARMS_FILENAME
        (tmp_path / LEVER_ARMS_FILENAME).write_text(text, encoding="utf-8")
        return tmp_path

    def test_the_real_file_parses_to_the_arms_in_force(self, tmp_path):
        from core.calibration import load_lever_arms
        arms, source = load_lever_arms(self.write(tmp_path))
        assert arms == {
            "Rear": (-0.928, 0.0, 0.0),
            "ROW": (-1.69, 0.0, 0.0),
            "L": (0.0, -0.3575, 0.0),
            "R": (0.0, 0.3575, 0.0),
        }
        from core.calibration import LEVER_ARMS_FILENAME
        assert LEVER_ARMS_FILENAME in source

    def test_editing_a_number_changes_the_arm(self, tmp_path):
        """The whole point. Change ROW's X and the app's ROW arm moves."""
        from core.calibration import camera_behind_laser_m

        d = self.write(tmp_path)
        assert camera_behind_laser_m("ROW", d) == pytest.approx(1.69)
        self.write(tmp_path, REAL_FILE.replace("X  -1.69", "X  -0.77"))
        assert camera_behind_laser_m("ROW", d) == pytest.approx(0.77)

    def test_the_lasers_come_from_the_file_too(self, tmp_path):
        from core.calibration import gocator_lever_arm_m

        d = self.write(tmp_path, REAL_FILE.replace("Y  -0.3575", "Y  -0.4000"))
        assert gocator_lever_arm_m("L", d) == (0.0, -0.4, 0.0)
        assert gocator_lever_arm_m("R", d) == (0.0, 0.3575, 0.0)

    def test_no_file_is_an_error_not_a_fallback(self, tmp_path):
        """Derek, 2026-09-02, after a batch ran on the built-in constants
        because the file had been moved: the app must not process without
        the correct lever arms. No file, no arms, no positions."""
        from core.calibration import camera_behind_laser_m, load_lever_arms
        from core.formats import ParseError
        with pytest.raises(ParseError, match="not found"):
            load_lever_arms(tmp_path)
        with pytest.raises(ParseError):
            camera_behind_laser_m("Rear", tmp_path)

    def test_a_file_that_parses_to_nothing_is_an_error_too(self, tmp_path):
        """Somebody deletes the headings. Silently stamping zeros would be the
        worst outcome: every sensor at the IMU and nothing saying so."""
        from core.calibration import load_lever_arms
        from core.formats import ParseError
        d = self.write(tmp_path, "just some notes, no arms here\n")
        with pytest.raises(ParseError, match="no lever arms"):
            load_lever_arms(d)

    @pytest.mark.parametrize("encoding", ["cp1252", "utf-16", "utf-8-sig"])
    def test_a_file_saved_by_notepad_still_reads(self, tmp_path, encoding):
        """Derek edits this file by hand, and Notepad still offers ANSI and
        UTF-16 in its Save dialog. read_text(encoding="utf-8") on an
        ANSI-saved file raises UnicodeDecodeError - a ValueError, which the
        `except OSError` around it did not catch. One degree sign typed into
        this file killed the whole batch before it wrote a single report,
        because gocator_lever_arm_m is called from process_collection outside
        any try. Found 2026-09-02.

        The numbers are ASCII; only a comment or a unit symbol can be the
        thing that will not decode. So the arms must still come out right.
        """
        from core.calibration import LEVER_ARMS_FILENAME, camera_behind_laser_m
        text = REAL_FILE + "\n# measured at 21 °C, ± 5 mm\n"
        (tmp_path / LEVER_ARMS_FILENAME).write_text(text, encoding=encoding)
        assert camera_behind_laser_m("Rear", tmp_path) == pytest.approx(0.928)
        assert camera_behind_laser_m("ROW", tmp_path) == pytest.approx(1.69)

    def test_bytes_that_decode_as_nothing_sensible_are_a_parse_error(self, tmp_path):
        """The batch must never die on this file: a ParseError lands in the
        run's QC report as format.lever_arms FAIL, and the run is not written.
        Anything else raised out of process_collection would take the whole
        batch down without a report."""
        from core.calibration import LEVER_ARMS_FILENAME, load_lever_arms
        from core.formats import ParseError
        (tmp_path / LEVER_ARMS_FILENAME).write_bytes(bytes(range(256)) * 4)
        with pytest.raises(ParseError):
            load_lever_arms(tmp_path)

    def test_the_profile_window_moves_with_the_file_too(self, tmp_path):
        """camera_ground_center_m is what puts the laser plot beside the right
        patch of ground, and it read the built-in constant, ignoring the file.
        So the viewer - the ONLY place the arm can be judged by eye - kept the
        old number while the written EXIF used the new one. Edit the file, look
        at the plot, see nothing move, conclude the file does nothing.

        Found 2026-09-02, the day Derek asked what to change the arm to.
        """
        from core.calibration import camera_ground_center_m
        d = self.write(tmp_path)
        assert camera_ground_center_m(100.0, d) == pytest.approx(100.0 - 0.928)
        self.write(tmp_path, REAL_FILE.replace("X  -0.928", "X  -0.976"))
        assert camera_ground_center_m(100.0, d) == pytest.approx(100.0 - 0.976), \
            "an edit to the file must move the plot window, not just the EXIF"

    def test_the_window_and_the_written_position_use_the_same_number(self, tmp_path):
        """The rule Derek set on 2026-09-01: one image, one position. Two
        readers of the same arm are two chances to disagree."""
        from core.calibration import camera_behind_laser_m, camera_ground_center_m
        d = self.write(tmp_path, REAL_FILE.replace("X  -0.928", "X  -1.234"))
        assert (100.0 - camera_ground_center_m(100.0, d)
                == pytest.approx(camera_behind_laser_m("Rear", d)))

    def test_a_camera_not_in_the_file_is_left_uncorrected(self, tmp_path):
        """None, never 0.0. Zero would claim the lens sits on the IMU."""
        from core.calibration import camera_behind_laser_m, camera_lever_arm_m
        d = self.write(tmp_path)
        assert camera_lever_arm_m("Left_Side", d) is None
        assert camera_behind_laser_m("Left_Side", d) is None

    def test_an_edit_in_the_same_clock_tick_is_still_seen(self, tmp_path):
        """The regression. The first version cached on (path, mtime), and
        Windows mtime resolution is coarse enough that two edits inside one
        tick share a timestamp - so the second was ignored and the app kept
        numbers the file no longer contained. Silently, and it moves where a
        photograph says it was taken.

        No sleep here on purpose: these writes land as fast as the filesystem
        allows, which is the case that failed. Note this test CANNOT fail on
        Linux, where mtime has nanosecond resolution - it caught the bug on
        the Windows box, which is where the app runs.
        """
        from core.calibration import camera_behind_laser_m
        d = self.write(tmp_path)
        assert camera_behind_laser_m("Rear", d) == pytest.approx(0.928)
        for x in ("-1.100", "-0.500", "-0.928"):
            self.write(tmp_path, REAL_FILE.replace("X  -0.928", f"X  {x}"))
            assert camera_behind_laser_m("Rear", d) == pytest.approx(-float(x))


class TestYAndZReachThePosition:
    """Y and Z are 0 on this cart today, so nothing moves - but a number in
    the file that nothing reads is worse than no number at all."""

    def test_a_sideways_arm_moves_the_position_sideways(self):
        lat, lon, _ = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, y_m=0.5)
        assert metres_apart((LAT0, LON0), (lat, lon)) == pytest.approx(0.5, abs=1e-3)
        assert lon > LON0, "heading north, +Y is to the east"

    def test_a_down_arm_lowers_the_altitude(self):
        _, _, alt = offset_by_lever_arm(LAT0, LON0, ALT0, 0.0, z_m=1.267)
        assert alt == pytest.approx(ALT0 - 1.267, abs=1e-6)

    def test_align_images_passes_all_three_axes(self, synth_run_root, synth_cal_dir):
        """Not just X. Same run aligned with a Y and a Z, and both must show."""
        from core.alignment import align_images
        from core.pipeline import process_run

        runs = disc.discover_runs(synth_run_root, calibrations_dir=synth_cal_dir,
                                  registered_only=False)
        res = process_run(runs[0])
        cam = next(iter(res.parsed.cameras))
        base = align_images(res.parsed.cameras[cam], res.triggers,
                            res.camera_matches[cam], res.ptp_l or res.ptp_r,
                            res.parsed.nav, arm_xyz=(0.0, 0.0, 0.0))
        moved = align_images(res.parsed.cameras[cam], res.triggers,
                             res.camera_matches[cam], res.ptp_l or res.ptp_r,
                             res.parsed.nav, arm_xyz=(0.0, 0.5, 2.0))
        ok = np.isfinite(base.lat_deg) & np.isfinite(moved.lat_deg)
        assert ok.any()
        assert metres_apart((base.lat_deg[ok][0], base.lon_deg[ok][0]),
                            (moved.lat_deg[ok][0], moved.lon_deg[ok][0])) \
            == pytest.approx(0.5, abs=0.01), "Y never reached the position"
        assert moved.alt_m[ok][0] == pytest.approx(base.alt_m[ok][0] - 2.0, abs=1e-6), \
            "Z never reached the altitude"


class TestTheLensHeightComesFromTheFileToo:
    """Not a lever arm - a height above the GROUND, not an offset from the IMU
    - but it is a physical measurement of this cart that the app applies, so it
    belongs where a human can change it. It sets millimetres-per-pixel, the
    two-point measure, the board check and the laser-profile window. It touches
    no written position."""

    FILE = REAL_FILE + """

## Rear lens height above the pavement

1.667
"""

    def write(self, tmp_path, text=None):
        from core.calibration import LEVER_ARMS_FILENAME
        (tmp_path / LEVER_ARMS_FILENAME).write_text(text or self.FILE, encoding="utf-8")
        return tmp_path

    def test_it_is_read(self, tmp_path):
        from core.calibration import lens_height_m
        assert lens_height_m(self.write(tmp_path)) == pytest.approx(1.667)

    def test_editing_it_changes_the_scale(self, tmp_path):
        from core.calibration import lens_height_m
        d = self.write(tmp_path, self.FILE.replace("\n1.667", "\n1.800"))
        assert lens_height_m(d) == pytest.approx(1.800)

    def test_it_does_not_become_a_camera(self, tmp_path):
        """The heading is not "<word> camera", so it must not turn into one."""
        from core.calibration import camera_lever_arm_m, load_lever_arms
        d = self.write(tmp_path)
        arms = load_lever_arms(d)[0]
        assert set(arms) - {"#lens_height"} == {"Rear", "ROW", "L", "R"}
        assert camera_lever_arm_m("Rear", d) == (-0.928, 0.0, 0.0)

    def test_a_file_without_it_falls_back(self, tmp_path):
        from core.calibration import PAVE_LENS_HEIGHT_M, lens_height_m
        d = self.write(tmp_path, REAL_FILE)          # arms only, no height
        assert lens_height_m(d) == PAVE_LENS_HEIGHT_M


class TestAxisLineTolerance:
    """A unit or a remark after the number must not zero the axis. The line
    was anchored at end-of-line, so "X  -0.928 m" matched nothing and the
    camera was silently stamped at the IMU."""

    def test_a_unit_after_the_number_is_fine(self):
        from core.calibration import parse_lever_arms
        arms = parse_lever_arms("## Rear camera\nX  -0.928 m\nY 0\nZ 0.1 m\n")
        assert arms["Rear"] == pytest.approx((-0.928, 0.0, 0.1))

    def test_a_remark_after_the_number_is_fine(self):
        from core.calibration import parse_lever_arms
        arms = parse_lever_arms("## Rear camera\nX -0.928   (was -1.27 until 2026-08-28)\n")
        assert arms["Rear"][0] == pytest.approx(-0.928)

    def test_a_number_run_together_with_text_is_not_a_value(self):
        from core.calibration import parse_lever_arms
        # "X 1.2.3" is a typo, not 1.2 - leave the axis at its default
        arms = parse_lever_arms("## Rear camera\nX 1.2.3\n")
        assert arms["Rear"][0] == 0.0


class TestNoArmsNoProcessing:
    """Derek, 2026-09-02: "the app should not process without the correct
    lever arms" and "it should return missing files if any file is missing".
    The file is a required input, exactly like eventOutA.txt."""

    def daily(self, root):
        day = root / "20260816"
        day.mkdir(exist_ok=True)
        (day / "Daily_ARAN104_20260816.csv").write_text(
            "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed\n"
            "Video [20260816.150000] run,200180,North,0.000,0.700,43.436300,-80.315200,20\n",
            encoding="utf-8-sig")

    def test_a_missing_file_is_a_missing_input(self, synth_run_root, synth_cal_dir, tmp_path):
        from core.batch import preflight, process_collection
        from core.calibration import LEVER_ARMS_FILENAME
        from core.discovery import OverrideStore
        (synth_cal_dir / LEVER_ARMS_FILENAME).unlink()
        runs = disc.discover_runs(synth_run_root, calibrations_dir=synth_cal_dir,
                                  registered_only=False)
        assert disc.KEY_LEVER_ARMS in runs[0].missing()
        store = OverrideStore(tmp_path / "ov.json")
        pf = preflight(synth_run_root, calibrations_dir=synth_cal_dir, overrides=store)
        assert pf.not_ready and not pf.ready
        assert "lever_arms" in pf.not_ready[0].reason
        report = process_collection(synth_run_root, calibrations_dir=synth_cal_dir,
                                    overrides=store)
        o = report.outcomes[0]
        assert o.status == "skipped"
        assert o.images_tagged == 0 and o.profiles_tagged == 0
        assert "NO LEVER-ARM FILE" in report.to_text()
        from core.geotag import read_image_gps
        for p in (synth_run_root / "Images" / "20260816.150000" / "Rear").glob("*.jpg"):
            assert read_image_gps(p) is None

    def test_the_report_names_the_file_the_arms_came_from(self, synth_run_root, synth_cal_dir, tmp_path):
        from core.batch import process_collection
        from core.calibration import LEVER_ARMS_FILENAME
        from core.discovery import OverrideStore
        report = process_collection(synth_run_root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        assert report.outcomes[0].status == "written"
        assert LEVER_ARMS_FILENAME in report.to_text().splitlines()[3]

    def test_a_camera_missing_from_the_file_holds_the_images_only(
            self, synth_run_root, synth_cal_dir, tmp_path):
        """ROW is on disk but not in the file: images and table held, both
        lasers still written, and nothing is renamed."""
        from core.batch import process_collection
        from core.calibration import LEVER_ARMS_FILENAME
        from core.discovery import OverrideStore
        from core.geotag import read_image_gps
        f = synth_cal_dir / LEVER_ARMS_FILENAME
        f.write_text(f.read_text(encoding="utf-8").replace("## ROW camera", "## ROW (unmeasured)"),
                     encoding="utf-8")
        self.daily(synth_run_root)
        report = process_collection(synth_run_root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        o = report.outcomes[0]
        assert o.status == "partial", (o.status, o.failures)
        assert "images" in o.held and "csv" in o.held
        assert "ROW" in o.held["images"]
        assert o.images_tagged == 0 and o.profiles_tagged > 0
        assert any("RENAME skipped" in n for n in o.notes)
        rear = synth_run_root / "Images" / "20260816.150000" / "Rear"
        assert not (rear / "rename_manifest.csv").exists()
        for p in rear.glob("*.jpg"):
            assert read_image_gps(p) is None

    def test_a_laser_missing_from_the_file_holds_that_laser_only(
            self, synth_run_root, synth_cal_dir, tmp_path):
        from core.batch import process_collection
        from core.calibration import LEVER_ARMS_FILENAME
        from core.discovery import OverrideStore
        f = synth_cal_dir / LEVER_ARMS_FILENAME
        f.write_text(f.read_text(encoding="utf-8").replace("## Gocator right", "## Laser right (not measured)"),
                     encoding="utf-8")
        report = process_collection(synth_run_root, calibrations_dir=synth_cal_dir,
                                    overrides=OverrideStore(tmp_path / "ov.json"))
        o = report.outcomes[0]
        assert o.status == "partial", (o.status, o.failures)
        assert set(o.held) == {"gocator_R"}
        assert o.images_tagged > 0 and o.profiles_tagged > 0

    def test_a_zero_arm_in_the_file_is_applied_not_refused(self, tmp_path):
        """A camera listed with X 0 is a claim somebody made on purpose."""
        from core.calibration import parse_lever_arms
        arms = parse_lever_arms("## Rear camera\nX 0\nY 0\nZ 0\n")
        assert arms["Rear"] == (0.0, 0.0, 0.0)
