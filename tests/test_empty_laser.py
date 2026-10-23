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


def test_no_laser_profiles_at_all_still_places_the_photos(synth_run_root, synth_cal_dir):
    """20261016.172652: both laser CSVs had no profile rows, there was no PTP
    result, and the alignment never ran - 92 photos got no GPS. Photos are
    placed by trigger time; Derek (2026-10-23): process what is good and
    report the rest."""
    from core import discovery as disc
    from core.pipeline import process_run
    from core.qc import TARGET_CSV, TARGET_GOCATOR_L, TARGET_GOCATOR_R, TARGET_IMAGES
    run = disc.discover_runs(synth_run_root, calibrations_dir=synth_cal_dir)[0]
    for key in (disc.KEY_GOCATOR_L, disc.KEY_GOCATOR_R):
        p = run.get(key)
        header = p.read_text(encoding="utf-8").splitlines()[0]
        p.write_text(header + "\n", encoding="utf-8")
    r = process_run(run)
    blocked = r.report.blocked_targets()
    assert {TARGET_GOCATOR_L, TARGET_GOCATOR_R} <= blocked
    assert TARGET_IMAGES not in blocked and TARGET_CSV not in blocked
    assert r.alignment is not None
    assert np.isfinite(r.alignment.lat_deg).sum() > 0


def test_batch_writes_the_photos_and_holds_the_empty_lasers(synth_run_root,
                                                             synth_cal_dir, tmp_path):
    """The batch side of the same run: photos written, both lasers HELD and
    named, status partial - not 'written' (a laser that would not parse used
    to be skipped without being listed as held)."""
    from core import discovery as disc
    from core.batch import process_collection
    run = disc.discover_runs(synth_run_root, calibrations_dir=synth_cal_dir)[0]
    for key in (disc.KEY_GOCATOR_L, disc.KEY_GOCATOR_R):
        p = run.get(key)
        p.write_text(p.read_text(encoding="utf-8").splitlines()[0] + "\n",
                     encoding="utf-8")
    rep = process_collection(synth_run_root, calibrations_dir=synth_cal_dir,
                             overrides=disc.OverrideStore(tmp_path / "ov.json"))
    o = rep.outcomes[0]
    assert o.status == "partial"
    assert o.images_tagged > 0
    assert set(o.held) == {"gocator_L", "gocator_R"}
