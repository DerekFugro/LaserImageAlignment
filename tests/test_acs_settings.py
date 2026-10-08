"""The DMI calibration comes from ACS's Settings file for the day (2026-10-08).

Until then the old cart's Gocator scale (0.235116 mm/tick) was built into the
code, so the new van's encoder messages overstated distances by ~36%.
"""
import numpy as np
import pytest

from core.acs_settings import dmi_for_run, find_settings_file, read_dmi
from core.qc import Severity
from tests.test_qc import acs_settings_file, gocator, run_checks


def test_reads_the_new_van(tmp_path):
    acs_settings_file(tmp_path, "20261007", 1445.56, van="ARANSW1")
    dmi, why = dmi_for_run(tmp_path, "20261007.120812")
    assert why == ""
    assert dmi.pulses_per_m == pytest.approx(1445.56)
    assert dmi.effective == "Oct 2 2026 6:47PM"
    # the Gocator counts every edge: exactly the 0.172943 set on the sensor
    assert dmi.gocator_mm_per_tick == pytest.approx(0.172943, abs=1e-6)
    assert "ARANSW1_Settings_20261007.xml" in dmi.describe()


def test_the_old_van_gives_the_old_scale(tmp_path):
    acs_settings_file(tmp_path, "20260821", 1063.17)
    dmi, _ = dmi_for_run(tmp_path, "[20260821.130056]")
    assert dmi.gocator_mm_per_tick == pytest.approx(0.235146, abs=1e-6)


def test_each_day_uses_its_own_file(tmp_path):
    acs_settings_file(tmp_path, "20261005", 1443.504, van="ARANSW1")
    acs_settings_file(tmp_path, "20261007", 1445.56, van="ARANSW1")
    assert dmi_for_run(tmp_path, "20261005.095340")[0].pulses_per_m == 1443.504
    assert dmi_for_run(tmp_path, "20261007.120812")[0].pulses_per_m == 1445.56


def test_missing_or_broken_file_says_why(tmp_path):
    dmi, why = dmi_for_run(tmp_path, "20261007.120812")
    assert dmi is None and "_Settings_20261007.xml" in why
    folder = tmp_path / "20261007"
    folder.mkdir()
    bad = folder / "ARANSW1_Settings_20261007.xml"
    bad.write_text("<ARANSettings><misc/></ARANSettings>", encoding="utf-8")
    assert find_settings_file(tmp_path, "20261007") == bad
    dmi, why = read_dmi(bad)
    assert dmi is None and "no DMICalibration" in why


def _one_step_back():
    enc = np.arange(1000, dtype=np.int64) * 136
    enc[290] -= 136 * 2                   # one notch back, 29% through
    return enc


def test_encoder_message_uses_the_vans_scale(tmp_path):
    acs_settings_file(tmp_path, "20260817", 1445.56, van="ARANSW1")
    checks = run_checks(gocator_l=gocator(_one_step_back()), root=tmp_path)
    msg = checks["content.gocator_L_encoder"].message
    # 136 counts x 0.172943 mm = 24 mm; the old built-in scale said 32 mm
    assert "(~24 mm)" in msg
    span = 999 * 136 / (1445.56 * 4)
    assert f"into a {span:.0f} m run" in msg
    info = checks["content.acs_dmi"]
    assert info.severity is Severity.INFO
    assert info.values["pulses_per_m"] == pytest.approx(1445.56)


def test_without_a_settings_file_there_is_no_made_up_scale(tmp_path):
    checks = run_checks(gocator_l=gocator(_one_step_back()), root=tmp_path)
    msg = checks["content.gocator_L_encoder"].message
    assert "29% through the run" in msg
    assert " mm)" not in msg and "m run" not in msg
    assert "counts, not mm" in checks["content.acs_dmi"].message


def _laser_with_a_step_back(tmp_path, pulses_per_m):
    """A 30 m laser run logged at 4253.1 counts/m (ARAN104: 1063.17 x 4), with
    one step back mid-run, checked against `pulses_per_m` from ACS."""
    from tests.test_qc import gocator_over, parked_then_moving
    dmi, nav, triggers = parked_then_moving(move_s=35.0, speed=1.0, phantom_m=0.0)
    d0, d1 = float(triggers.dist_m[0]), float(triggers.dist_m[-1])
    goc, ptp = gocator_over(nav, 37.0, d0, d1 + 0.3)
    goc.encoder[len(goc.encoder) // 2] -= 500
    acs_settings_file(tmp_path, "20260817", pulses_per_m)
    checks = run_checks(dmi=dmi, nav=nav, triggers=triggers,
                        gocator_l=goc, ptp_l=ptp, root=tmp_path)
    return checks["content.gocator_L_encoder"].message


def test_matching_encoder_gets_distances(tmp_path):
    msg = _laser_with_a_step_back(tmp_path, 1063.17)
    assert "m into a" in msg and " mm)" in msg
    assert "Distances left out" not in msg


def test_encoder_that_disagrees_with_acs_gets_no_distances(tmp_path):
    """July / early August 2026: the Gocator was NOT set to ACS x 4 (it ran
    0.64-0.66 of it). A wrong scale must not turn into wrong millimetres."""
    msg = _laser_with_a_step_back(tmp_path, 1445.56)
    assert "Distances left out" in msg
    assert "through the run" in msg
    assert " mm)" not in msg
