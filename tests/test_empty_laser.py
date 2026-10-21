"""An almost empty laser file is reported as data missing, in percent
(20261015.165518: 1 and 3 profiles over a 496 m run)."""
from types import SimpleNamespace

import numpy as np

from core.qc import _encoder_step


class _goc:
    """Just enough of a GocatorIndex: an encoder column and a length."""

    def __init__(self, encoder):
        self.encoder = np.asarray(encoder)

    def __len__(self):
        return len(self.encoder)


def test_spacing_is_measured_from_the_files_own_encoder():
    """The new van steps 136 tics per profile, not the old cart's 102."""
    r = _goc([123648344, 123648480, 123648616])
    assert _encoder_step(r) == 136


def test_one_profile_borrows_the_other_lasers_step():
    left, right = _goc([123648344]), _goc([123648344, 123648480, 123648616])
    assert _encoder_step(left, left, right) == 136


def test_no_step_when_no_file_has_two_profiles():
    assert _encoder_step(_goc([1]), _goc([5]), None) is None


def test_batch_summary_says_no_laser_data_instead_of_a_fake_fit():
    from core.alignment import PTP_MIN_PROFILES
    from core.batch import BatchReport, RunOutcome
    p = SimpleNamespace(offset_s=37.0, fitted_offset_s=37.0, r=0.0, n_used=1)
    res = SimpleNamespace(ptp_l=p, match=None, report=None,
                          parsed=SimpleNamespace(images=[]))
    o = RunOutcome(run_id="20261015.165518", ok=True, status="partial", result=res)
    rep = BatchReport(root="r", started_utc="2026-10-21T00:00:00+00:00")
    rep.outcomes.append(o)
    text = rep.to_text()
    assert "no laser data to check it" in text and "r=0.000" not in text
    assert PTP_MIN_PROFILES == 10
