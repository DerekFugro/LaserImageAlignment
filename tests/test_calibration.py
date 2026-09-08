"""Calibration tests: YAML loading, undistortion, and the pixel-to-millimetre
scale that puts the laser's rows on the picture."""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.calibration import (  # noqa: E402
    PAVE_CAM_BEHIND_LASER_M, PAVE_LENS_HEIGHT_M, CameraCalibration, Undistorter,
    camera_ground_center_m, find_pave_calibration, ground_to_image_row,
)
from core.formats import ParseError  # noqa: E402

SQUARE_PX = 60  # px per 76.2 mm, the scale the fixture is built to


def make_flat_calibration(width: int, height: int) -> CameraCalibration:
    """A distortion-free calibration whose nadir scale makes one rendered
    square (SQUARE_PX px) equal exactly 76.2 mm at the nominal lens height."""
    mm_per_px_true = 76.2 / SQUARE_PX
    fx = PAVE_LENS_HEIGHT_M * 1000.0 / mm_per_px_true
    matrix = np.array([[fx, 0, width / 2], [0, fx, height / 2], [0, 0, 1.0]])
    return CameraCalibration(
        serial_number="SYNTH", calibration_datetime="2026-01-01T00:00:00",
        rms_reprojection_error=0.1, verdict="Excellent",
        camera_matrix=matrix, dist_coeffs=np.zeros(5),
        width=width, height=height,
        path="synthetic",
    )




@pytest.fixture(scope="module")
def test_image():
    """Something with edges in it, for undistort to move around.

    It used to be a rendered ChArUco board, because the same picture also fed
    the board check. That check went with the calibration tools; undistort
    stayed, and it does not care what the picture is - only that a round trip
    through zero distortion gives the same pixels back.
    """
    w, h = 12 * SQUARE_PX + 200, 8 * SQUARE_PX + 200
    gray = np.full((h, w), 255, dtype=np.uint8)
    gray[::40, :] = 0          # a grid, so an identity map is checkable
    gray[:, ::40] = 0
    return np.dstack([gray] * 3)


class TestYamlLoading:
    def test_loads_real_format(self, synth_cal_dir):
        path = find_pave_calibration(synth_cal_dir)
        assert path is not None
        cal = CameraCalibration.load(path)
        assert cal.fx == pytest.approx(2757.59, abs=0.1)
        assert cal.width == 2880 and cal.height == 1860
        assert cal.mm_per_px() == pytest.approx(PAVE_LENS_HEIGHT_M * 1000 / 2757.591943, rel=1e-6)

    def test_missing_field_fails(self, tmp_path):
        p = tmp_path / "PAVE_bad.yaml"
        p.write_text("serial_number: 'x'\n")
        with pytest.raises(ParseError, match="missing/invalid"):
            CameraCalibration.load(p)

    def test_find_newest(self, tmp_path, synth_cal_dir):
        assert find_pave_calibration(tmp_path / "nonexistent") is None


class TestUndistorter:
    def test_zero_distortion_is_identity(self, test_image):
        cal = make_flat_calibration(test_image.shape[1], test_image.shape[0])
        out = Undistorter(cal).undistort(test_image)
        assert out.shape == test_image.shape
        assert np.mean(np.abs(out.astype(int) - test_image.astype(int))) < 1.0

    def test_size_mismatch_raises(self, test_image):
        cal = make_flat_calibration(100, 100)
        with pytest.raises(ValueError, match="calibration expects"):
            Undistorter(cal).undistort(test_image)


class TestGroundToImageRow:
    """Real Pave numbers: cy = 918.775, mm/px = 0.61648, image 1860 rows.
    Camera trails the laser by PAVE_CAM_BEHIND_LASER_M; bottom of image = toward
    cart front. Constants come from core.calibration so this cannot drift out of
    step with the app (it did, when the measured height replaced the nominal)."""

    CY = 918.775062
    MM_PX = PAVE_LENS_HEIGHT_M * 1000.0 / 2757.591943

    def test_camera_center_offset(self):
        assert camera_ground_center_m(2.0) == pytest.approx(2.0 - PAVE_CAM_BEHIND_LASER_M)

    def test_center_row_at_camera_center(self):
        cam = camera_ground_center_m(2.0)
        row = ground_to_image_row(cam, cam, self.CY, self.MM_PX)
        assert row == pytest.approx(self.CY)

    def test_forward_ground_is_lower_in_image(self):
        cam = camera_ground_center_m(2.0)
        row_fwd = ground_to_image_row(cam + 0.3, cam, self.CY, self.MM_PX)
        assert row_fwd > self.CY  # larger ground coordinate -> further down
        assert row_fwd - self.CY == pytest.approx(300.0 / self.MM_PX, rel=1e-6)

    def test_gocator_start_visible_on_early_image(self):
        """Run 110840: the Gocator starts recording at ~0.06 m, and the image
        taken at trigger distance 1.054 m must SHOW that ground.

        Asserting which side of centre it lands on would be pinning the arm's
        value, not the geometry: at the old 1.27 m the point sat below centre,
        at 0.928 m it sits above. What must hold either way is that it is in
        the frame at all.
        """
        cam = camera_ground_center_m(1.054)
        row = ground_to_image_row(0.06, cam, self.CY, self.MM_PX)
        assert 0 < row < 1860

    def test_far_ground_falls_off_image(self):
        cam = camera_ground_center_m(100.0)
        row = ground_to_image_row(0.06, cam, self.CY, self.MM_PX)
        assert row < 0  # far behind: way off the top of the image


class TestProfileWindowCentring:
    """The profile window must sit on the CAMERA's ground, not the laser's.

    Regression guard for the viewer bug where the trigger (laser) distance was
    passed straight to ProfilePairView.show_window, putting the plotted profiles
    ~1.25 m ahead of the picture — outside its footprint entirely.
    """

    def test_window_centre_is_lever_arm_corrected(self, synth_cal_dir):
        cal = CameraCalibration.load(find_pave_calibration(synth_cal_dir))
        trigger_dist = 12.0
        centre = camera_ground_center_m(trigger_dist)
        assert centre == pytest.approx(trigger_dist - PAVE_CAM_BEHIND_LASER_M)
        # the error the bug produced is bigger than the whole window
        assert abs(trigger_dist - centre) > cal.along_track_half_span_m()

    def test_half_span_matches_the_image_footprint(self, synth_cal_dir):
        cal = CameraCalibration.load(find_pave_calibration(synth_cal_dir))
        half = cal.along_track_half_span_m()
        assert half == pytest.approx(cal.height * cal.mm_per_px_y() / 2000.0)
        assert 2 * half == pytest.approx(cal.height * cal.mm_per_px_y() / 1000.0)

    def test_laser_ground_at_trigger_is_outside_the_image(self, synth_cal_dir):
        """The old behaviour, stated as a fact: what the laser was over at
        trigger time is not visible in that image."""
        cal = CameraCalibration.load(find_pave_calibration(synth_cal_dir))
        cam = camera_ground_center_m(12.0)
        row = ground_to_image_row(12.0, cam, float(cal.camera_matrix[1, 2]),
                                  cal.mm_per_px_y())
        assert row > cal.height          # off the bottom edge




class TestRefinedIsIgnored:
    """The bar-run tool writes PAVE_*_Refined.yaml into the same folder. It has
    been seen with a 6x-wrong focal length and no `board` block, so the app must
    never auto-select it (Derek, 2026-08-19)."""

    def test_refined_never_selected(self, tmp_path):
        d = tmp_path / "cal" / "20260815_PaveIntrinsic"
        d.mkdir(parents=True)
        good = d / "PAVE_234500499.yaml"
        good.write_text("ok", encoding="utf-8")
        import os, time
        bad = d / "PAVE_234500499_Refined.yaml"
        bad.write_text("bad", encoding="utf-8")
        os.utime(bad, (time.time() + 100, time.time() + 100))   # newest by far
        picked = find_pave_calibration(tmp_path / "cal")
        assert picked == good, f"picked {picked}"

    def test_no_usable_calibration_returns_none(self, tmp_path):
        d = tmp_path / "cal"; d.mkdir()
        (d / "PAVE_x_Refined.yaml").write_text("x", encoding="utf-8")
        assert find_pave_calibration(d) is None
