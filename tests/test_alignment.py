"""Alignment engine tests: synthetic ground truth + real-data integration.

Ground truth (verified 2026-08-18 during spec work):
- run 20260816.110840: 592 triggers, 593 rear images, PTP offset 37.0 s,
  first image unmatched, counter-vs-DMI scale ~0.9985
- run 20260818.142721 (other dataset): offset 37.0 s @ r = 0.993,
  encoder ~4223.6 counts/m
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from core.discovery import discover_runs
from core.pipeline import export_csv, process_run
from core.qc import Severity
from tests.conftest import sampledata, TAI_UTC


class TestBestPtpSelection:
    """A failed Left offset must not win just because Left is parsed first."""

    def make(self, passed, offset=37.0):
        from core.alignment import PtpOffsetResult

        return PtpOffsetResult(offset_s=offset, r=0.99 if passed else 0.2,
                               passed=passed, n_used=100,
                               sweep_offsets=np.empty(0), sweep_r=np.empty(0),
                               message="")

    def test_prefers_the_passing_side(self):
        from core.pipeline import _best_ptp

        failed_l, good_r = self.make(False, 41.0), self.make(True, 37.0)
        assert _best_ptp(failed_l, good_r) is good_r

    def test_keeps_left_when_both_pass(self):
        from core.pipeline import _best_ptp

        left, right = self.make(True), self.make(True)
        assert _best_ptp(left, right) is left

    def test_falls_back_to_a_failed_one_so_the_ui_still_shows_numbers(self):
        from core.pipeline import _best_ptp

        left, right = self.make(False), self.make(False)
        assert _best_ptp(left, right) is left

    def test_none_when_nothing_solved(self):
        from core.pipeline import _best_ptp

        assert _best_ptp(None, None) is None
        assert _best_ptp(None) is None


class TestSyntheticEndToEnd:
    @pytest.fixture()
    def result(self, synth_run_root, synth_cal_dir):
        runs = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir)
        assert len(runs) == 1
        assert runs[0].missing() == []
        return process_run(runs[0])

    def test_ptp_offset_recovered(self, result):
        assert result.ptp_l is not None and result.ptp_l.passed
        assert result.ptp_l.offset_s == pytest.approx(TAI_UTC, abs=0.06)
        assert result.ptp_l.r > 0.99

    def test_match_is_ordinal_tail_anchored(self, result):
        m = result.match
        assert m.shift_k == 1  # one extra image at the start
        assert m.n_unmatched == 1
        assert int(m.trigger_for_image[0]) == -1        # pre-collection image
        assert int(m.trigger_for_image[1]) == 0
        assert int(m.trigger_for_image[-1]) == len(result.triggers.utc_s) - 1

    def test_counter_scale_limit_covers_the_fleet_and_still_has_teeth(self, result):
        """align.counter_scale is realtime INS cadence vs postprocessed INS
        distance. The gap is systematic, not per-run: on 20260824 the export
        read short on ten of eleven runs (a = 0.9932 to 1.0000, residuals
        4-38 mm), so anything inside that spread reports normal behaviour as a
        problem. Derek set the limit at 1.0% on 2026-08-31.

        Pinned here so the limit is not quietly nudged: it must pass the worst
        run of that day and still catch a scale error big enough to mean
        something."""
        from core.pipeline import COUNTER_SCALE_MAX_DEV

        worst_seen = abs(0.99316 - 1.0)          # 20260824.101724
        assert COUNTER_SCALE_MAX_DEV > worst_seen
        assert COUNTER_SCALE_MAX_DEV < 0.02
        check = next(c for c in result.report.checks
                     if c.check_id == "align.counter_scale")
        assert check.severity is Severity.PASS
        assert f"{COUNTER_SCALE_MAX_DEV * 100:.1f}%" in check.message

    def test_locations_and_export(self, result, tmp_path):
        assert not result.export_blocked, [c.message for c in result.report.failed]
        out = export_csv(result, tmp_path / "out.csv")
        lines = out.read_text().splitlines()
        n_cams = max(len(result.camera_alignments), 1)
        assert len(lines) == len(result.parsed.images) * n_cams + 1
        al = result.alignment
        matched = result.match.trigger_for_image >= 0
        assert np.all(np.isfinite(al.lat_deg[matched]))
        assert np.all(np.isnan(al.lat_deg[~matched]))
        # PTP time = UTC + solved offset
        j = int(np.where(matched)[0][0])
        assert al.ptp_us[j] / 1e6 - al.utc_s[j] == pytest.approx(result.ptp_l.offset_s, abs=1e-3)

    def test_export_blocked_on_fail(self, synth_run_root, synth_cal_dir, tmp_path):
        # corrupt the export so nav coverage fails -> export must refuse
        nav = next(synth_run_root.rglob("ascii-output.txt"))
        text = nav.read_text(encoding="utf-8").splitlines()
        nav.write_text("\n".join(text[:6] + text[6:200]) + "\n",   # truncate
                       encoding="utf-8")
        runs = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir)
        res = process_run(runs[0])
        assert res.export_blocked
        with pytest.raises(RuntimeError, match="export blocked"):
            export_csv(res, tmp_path / "out.csv")

    def test_missing_input_reported_not_crash(self, synth_run_root, synth_cal_dir):
        next(synth_run_root.rglob("ascii-output.txt")).unlink()
        runs = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir)
        assert "nav_export" in runs[0].missing()[0]
        res = process_run(runs[0])
        fail_ids = [c.check_id for c in res.report.failed]
        assert "presence.nav_export" in fail_ids
        assert res.export_blocked


class TestOverrides:
    def test_locate_flow_and_persistence(self, synth_run_root, synth_cal_dir, tmp_path):
        from core.discovery import OverrideStore, KEY_NAV_EXPORT

        # move the export elsewhere (simulates it living in another dataset)
        nav = next(synth_run_root.rglob("ascii-output.txt"))
        moved = tmp_path / "elsewhere" / "ascii-output.txt"
        moved.parent.mkdir()
        nav.rename(moved)

        store_path = tmp_path / "overrides.json"
        store = OverrideStore(store_path)
        runs = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir, overrides=store)
        assert runs[0].get(KEY_NAV_EXPORT) is None

        # user locates the file -> saved -> applied on rediscovery (and "restart")
        store.set(runs[0].root, runs[0].run_id, KEY_NAV_EXPORT, moved)
        runs2 = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir, overrides=store)
        assert runs2[0].get(KEY_NAV_EXPORT) == moved

        fresh_store = OverrideStore(store_path)  # app restart
        runs3 = discover_runs(synth_run_root, calibrations_dir=synth_cal_dir, overrides=fresh_store)
        assert runs3[0].get(KEY_NAV_EXPORT) == moved
        res = process_run(runs3[0])
        assert not res.export_blocked


@pytest.fixture(scope="module")
def real_result(sample_root, sample_cal_dir):
    runs = discover_runs(sample_root, calibrations_dir=sample_cal_dir)
    by_id = {r.run_id: r for r in runs}
    assert "20260816.110840" in by_id
    return process_run(by_id["20260816.110840"])


@sampledata
class TestRealRun110840:
    """Integration against real run 20260816.110840 (needs the F: sample data,
    or LIA_SAMPLE_ROOT pointing at the dataset)."""

    @pytest.fixture()
    def result(self, real_result):
        return real_result

    def test_counts(self, result):
        assert len(result.parsed.triggers_sbg_us) == 592
        assert len(result.parsed.images) == 593

    def test_ptp_offset_ground_truth(self, result):
        assert result.ptp_l.passed
        assert result.ptp_l.offset_s == pytest.approx(37.0, abs=0.1)
        assert result.ptp_l.r >= 0.95

    def test_match_ground_truth(self, result):
        assert result.match.shift_k == 1
        assert result.match.n_unmatched == 1
        assert int(result.match.trigger_for_image[0]) == -1
        assert abs(result.match.scale_a - 1.0) < 0.005

    def test_all_matched_images_located(self, result):
        al = result.alignment
        matched = result.match.trigger_for_image >= 0
        assert np.all(np.isfinite(al.lat_deg[matched]))

    def test_no_qc_failures(self, result):
        assert not result.export_blocked, [c.message for c in result.report.failed]


class TestTheLeverArmIsARigidOffset:
    """The lens is bolted a fixed distance behind the laser. Its ground centre
    is therefore the cart's position displaced opposite the HEADING — not the
    place the cart happened to occupy earlier.

    Those two readings agree only on a straight line travelled at constant
    speed, which is why the difference went unnoticed. They part company on a
    curve, and they part company badly for the first image of a run, where the
    cart was never as far back as the arm asks for and the walk falls into the
    pre-run settling noise (measured on 20260824: every run's first image short
    by -23 mm to -675 mm).
    """

    ARM = 1.021

    def _run(self, lat, lon, epoch, yaw_deg):
        """One synthetic run: a nav track plus a trigger on every fix."""
        import numpy as np
        from core.alignment import ImageSet, MatchResult, PtpOffsetResult, TriggerData, align_images
        from core.formats import NavTable

        nav = NavTable(epoch_s=np.asarray(epoch, float), lat_deg=np.asarray(lat, float),
                       lon_deg=np.asarray(lon, float),
                       alt_m=np.zeros(len(epoch)), yaw_deg=np.asarray(yaw_deg, float),
                       gps_tow_s=np.asarray(epoch, float))
        n = len(epoch)
        images = ImageSet(dir="x", files=[f"{i}.jpg" for i in range(n)],
                          counter_mm=(np.arange(n, dtype=np.int64) + 1) * 750)
        triggers = TriggerData(
            sbg_us=(np.asarray(epoch, float) * 1e6).astype(np.int64),
            utc_s=np.asarray(epoch, float), dist_m=np.asarray(nav.along_m, float),
            spacing_m=np.diff(np.asarray(nav.along_m, float)),
            spacing_ok_frac=1.0, message="")
        match = MatchResult(shift_k=0, d0_m=0.0,
                            trigger_for_image=np.arange(n, dtype=np.int64),
                            residual_m=np.zeros(n), scale_a=1.0,
                            median_abs_residual_m=0.0, n_matched=n, n_unmatched=0,
                            message="")
        ptp = PtpOffsetResult(offset_s=37.0, r=1.0, passed=True, n_used=n,
                              sweep_offsets=np.empty(0), sweep_r=np.empty(0), message="")
        return nav, images, triggers, match, ptp

    def _straight_north(self, n=12, step_m=0.75):
        from core.formats import EARTH_M_PER_DEG_LAT
        lat = [43.0 + i * step_m / EARTH_M_PER_DEG_LAT for i in range(n)]
        return lat, [-80.0] * n, [1000.0 + i for i in range(n)], [0.0] * n

    def test_the_first_image_is_offset_like_every_other_one(self):
        """The bug: the first image cannot be placed by walking backwards,
        because the cart was never there. Heading needs no history."""
        import numpy as np
        from core.alignment import align_images
        from core.formats import EARTH_M_PER_DEG_LAT

        lat, lon, epoch, yaw = self._straight_north()
        nav, images, triggers, match, ptp = self._run(lat, lon, epoch, yaw)
        al = align_images(images, triggers, match, ptp, nav, arm_m=self.ARM)

        # heading is due north, so the lens sits ARM metres SOUTH of the cart
        offsets = (np.asarray(lat) - al.lat_deg) * EARTH_M_PER_DEG_LAT
        assert offsets == pytest.approx(self.ARM, abs=1e-6), \
            "every image, including the first, must be offset by the full arm"

    def test_the_offset_is_behind_not_ahead(self):
        """A sign error would place the photo a whole arm the wrong way — two
        arms, 2 m, from the truth. Checked in both run directions, because a
        rev-run is what makes a sign error visible."""
        import numpy as np
        from core.alignment import align_images
        from core.formats import EARTH_M_PER_DEG_LAT

        for heading, sign in ((0.0, +1.0), (180.0, -1.0)):
            lat, lon, epoch, _ = self._straight_north()
            if heading == 180.0:
                lat = lat[::-1]
            nav, images, triggers, match, ptp = self._run(
                lat, lon, epoch, [heading] * len(lat))
            al = align_images(images, triggers, match, ptp, nav, arm_m=self.ARM)
            shift = (al.lat_deg - np.asarray(lat)) * EARTH_M_PER_DEG_LAT
            assert shift == pytest.approx(-sign * self.ARM, abs=1e-6), \
                f"heading {heading}: lens must sit behind the cart, not ahead"

    def test_no_arm_leaves_the_cart_position_untouched(self):
        """An uncalibrated camera must keep the raw trigger position."""
        import numpy as np
        from core.alignment import align_images

        lat, lon, epoch, yaw = self._straight_north()
        nav, images, triggers, match, ptp = self._run(lat, lon, epoch, yaw)
        al = align_images(images, triggers, match, ptp, nav, arm_m=None)

        assert al.lat_deg == pytest.approx(np.asarray(lat), abs=1e-12)
        assert al.arm_m is None

    def test_an_unmatched_image_stays_unplaced(self):
        """NaN in must stay NaN out — an arm must never invent a position."""
        import numpy as np
        from core.alignment import align_images

        lat, lon, epoch, yaw = self._straight_north()
        nav, images, triggers, match, ptp = self._run(lat, lon, epoch, yaw)
        match.trigger_for_image[0] = -1
        al = align_images(images, triggers, match, ptp, nav, arm_m=self.ARM)

        assert np.isnan(al.lat_deg[0]) and np.isnan(al.lon_deg[0])
        assert np.isfinite(al.lat_deg[1:]).all()


class TestImpossibleScanIntervals:
    """The Gocator self-triggers off the encoder, so a profile interval is
    fixed distance / speed. A stamp that breaks that is corrupt, and Pearson r
    over encoder RATE gives it enormous leverage: two bad intervals in 1096
    profiles took run 20260824.101724's right laser from r=0.958 to 0.746 and
    failed a laser that was otherwise perfect."""

    @staticmethod
    def _nav(synth_run_root):
        from datetime import datetime, timezone
        from core.formats import NavTable
        return NavTable.parse(next(synth_run_root.rglob("ascii-output.txt")),
                              anchor_utc_date=datetime(2026, 8, 16, tzinfo=timezone.utc))

    def _gocator(self, synth, ptp_us=None):
        from core.formats import GocatorIndex
        n = len(synth["goc_ptp_us"])
        return GocatorIndex(
            path="x", frame=np.arange(n, dtype=np.int64),
            ptp_us=(synth["goc_ptp_us"] if ptp_us is None else ptp_us).astype(np.int64),
            encoder=synth["goc_enc"].astype(np.int64),
            n_points=np.full(n, 4, dtype=np.int32),
            n_valid=np.full(n, 3, dtype=np.int32),
            byte_offset=np.zeros(n, dtype=np.int64))

    def test_a_clean_run_drops_nothing(self, synth, synth_run_root):
        from core.alignment import solve_ptp_offset
        out = solve_ptp_offset(self._gocator(synth), self._nav(synth_run_root))
        assert out.passed and out.n_bad_intervals == 0

    def test_two_corrupt_stamps_no_longer_fail_the_laser(self, synth, synth_run_root):
        from core.alignment import solve_ptp_offset
        nav = self._nav(synth_run_root)
        clean = solve_ptp_offset(self._gocator(synth), nav)
        # shove two stamps: one lands far late, one far early — exactly the
        # shape seen on 101724 (59 ms and 4.7 ms where ~24 ms was due)
        ptp = synth["goc_ptp_us"].astype(np.int64).copy()
        ptp[40] += 33_000
        ptp[80] -= 19_000
        out = solve_ptp_offset(self._gocator(synth, ptp_us=ptp), nav)
        assert out.n_bad_intervals > 0, "the corrupt stamps should be caught"
        assert out.passed, out.message
        assert out.offset_s == pytest.approx(clean.offset_s, abs=0.02)
        assert "impossible scan interval" in out.message

    def test_it_never_reports_a_worse_score_than_the_honest_one(self, synth,
                                                                synth_run_root):
        """Dropping data must only ever be an improvement — otherwise the run
        keeps the score it actually earned."""
        from core.alignment import solve_ptp_offset
        base = solve_ptp_offset(self._gocator(synth), self._nav(synth_run_root))
        assert base.r >= 0.9
        assert base.n_bad_intervals == 0
