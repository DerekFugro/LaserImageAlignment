"""QC content-check tests.

These pin the behaviour of two checks that were rewritten on 2026-08-20 after
they misdiagnosed real data (collection 20260817_revrus3):

  * the realtime-vs-export distance check fired at 12-14% because it measured
    over the whole logged window, which is dominated by GNSS noise integrated
    while the cart sits parked. It read like a hardware fault; it was not.
  * "encoder decreases (reverse travel?)" fired on a single -6528-count step on
    the FINAL profile of both sensors at once — a logger-shutdown artifact.
"""
from __future__ import annotations

import numpy as np
import pytest

from core import discovery as disc
from core.alignment import TriggerData
from core.formats import DmiTable, GocatorIndex, NavTable
from core.qc import ParsedRun, QCReport, Severity, check_content

T0 = 1787003858.0
LAT0, LON0 = 43.436, -80.315
M_PER_DEG_LAT = 111320.0


def nav_from_along(epoch_s, along_m):
    """A NavTable whose along-track path length follows `along_m` exactly.

    NavTable recomputes along_m from lat/lon in __post_init__, so the distance
    has to be encoded as real north-south motion.
    """
    step = np.diff(np.asarray(along_m, dtype=float), prepend=0.0)
    lat = LAT0 + np.cumsum(step) / M_PER_DEG_LAT
    n = len(lat)
    return NavTable(
        epoch_s=np.asarray(epoch_s, dtype=float), lat_deg=lat,
        lon_deg=np.full(n, LON0), alt_m=np.full(n, 300.0),
        yaw_deg=np.zeros(n), gps_tow_s=np.arange(n, dtype=float),
    )


def parked_then_moving(park_s=40.0, move_s=50.0, speed=1.2, phantom_m=10.0, hz=10.0):
    """SBG distance holds still while parked; the export integrates noise.

    Returns (dmi, nav, triggers) with triggers spanning only the moving part.
    """
    t = np.arange(0.0, park_s + move_s, 1.0 / hz)
    moving = t >= park_s
    true_d = np.where(moving, (t - park_s) * speed, 0.0)

    # the export's path length: real motion PLUS zig-zag noise while parked
    n_park = int(np.sum(~moving))
    noise_step = phantom_m / max(n_park, 1)
    wobble = np.where(~moving, noise_step, 0.0)   # |step| accumulates either way
    nav_along = np.cumsum(wobble) + true_d

    dmi = DmiTable(epoch_s=T0 + t, dist_m=true_d,
                   speed_ms=np.where(moving, speed, 0.0), path="synthetic")
    nav = nav_from_along(T0 + t, nav_along)

    trig_d = np.arange(0.75, true_d[-1], 0.75)
    trig_utc = T0 + park_s + trig_d / speed
    triggers = TriggerData(
        sbg_us=np.zeros(len(trig_utc), dtype=np.int64), utc_s=trig_utc,
        dist_m=trig_d, spacing_m=np.diff(trig_d), spacing_ok_frac=1.0, message="",
    )
    return dmi, nav, triggers


def park_after_first_trigger(hold_s=156.0, wander_m=1.5, run_m=30.0,
                             speed=1.0, hz=10.0):
    """A run that stands still INSIDE its own trigger window.

    Trigger #1 fires as the cart is pushed into position, the cart then sits
    for `hold_s` while the export integrates `wander_m` of GNSS noise, and
    only then does the run proper begin. Triggers fire every 0.750 m of SBG
    distance, so none of them lands in the hold.
    """
    lead_s = 0.75 / speed                       # just enough to fire trigger #1
    t = np.arange(0.0, lead_s + hold_s + run_m / speed, 1.0 / hz)
    v = np.where((t < lead_s) | (t >= lead_s + hold_s), speed, 0.0)
    sbg_d = np.cumsum(v) / hz

    held = (t >= lead_s) & (t < lead_s + hold_s)
    n_held = max(int(np.sum(held)), 1)
    nav_along = sbg_d + np.cumsum(np.where(held, wander_m / n_held, 0.0))

    dmi = DmiTable(epoch_s=T0 + t, dist_m=sbg_d, speed_ms=v, path="synthetic")
    nav = nav_from_along(T0 + t, nav_along)

    trig_d = np.arange(0.75, sbg_d[-1], 0.75)
    trig_utc = T0 + np.interp(trig_d, sbg_d, t)
    triggers = TriggerData(
        sbg_us=np.zeros(len(trig_utc), dtype=np.int64), utc_s=trig_utc,
        dist_m=trig_d, spacing_m=np.diff(trig_d), spacing_ok_frac=1.0, message="",
    )
    return dmi, nav, triggers


def run_checks(**parsed_kwargs):
    run = disc.RunPaths(run_id="20260817.175738", root="synthetic")
    pr = ParsedRun(run=run)
    pr.cameras = {}
    triggers = parsed_kwargs.pop("triggers", None)
    # the PTP results are arguments to check_content, not fields of ParsedRun
    ptp_l = parsed_kwargs.pop("ptp_l", None)
    ptp_r = parsed_kwargs.pop("ptp_r", None)
    for k, v in parsed_kwargs.items():
        setattr(pr, k, v)
    report = QCReport(run_id=run.run_id)
    check_content(pr, report, triggers=triggers, ptp_l=ptp_l, ptp_r=ptp_r)
    return {c.check_id: c for c in report.checks}


class TestRealtimeVsExport:
    def test_parked_noise_does_not_look_like_a_hardware_fault(self):
        """The regression: 10 m of phantom distance before the run must not
        show up as a double-digit realtime-vs-export disagreement."""
        dmi, nav, triggers = parked_then_moving(phantom_m=10.0)
        checks = run_checks(dmi=dmi, nav=nav, triggers=triggers)
        cross = checks["content.realtime_vs_export"]
        assert cross.severity is Severity.PASS
        assert cross.values["dev_pct"] < 2.0

    def test_the_phantom_distance_is_still_reported_separately(self):
        dmi, nav, triggers = parked_then_moving(phantom_m=10.0)
        checks = run_checks(dmi=dmi, nav=nav, triggers=triggers)
        settling = checks["content.nav_settling"]
        # reported, but not a warning: it is normal GNSS behaviour, entirely
        # before the first trigger, and nothing is placed from it
        assert settling.severity is Severity.PASS
        assert settling.values["phantom_m"] == pytest.approx(10.0, abs=0.5)
        # the cart really does travel one trigger interval before trigger #1
        assert settling.values["moved_m"] == pytest.approx(0.75, abs=0.1)

    def test_a_clean_start_passes_both(self):
        dmi, nav, triggers = parked_then_moving(phantom_m=0.0)
        checks = run_checks(dmi=dmi, nav=nav, triggers=triggers)
        assert checks["content.realtime_vs_export"].severity is Severity.PASS
        assert checks["content.nav_settling"].severity is Severity.PASS

    def test_a_long_park_after_the_first_trigger_is_not_a_disagreement(self):
        """20260824.101313: the SBG fires the camera every 0.750 m of realtime
        travel, so trigger #1 goes off while the cart is being manoeuvred into
        place - and then the cart sat still for 156 s. The SBG advanced 0.01 m
        over that hold and the export accumulated 1.50 m of GNSS wander, which
        the trigger-window version of this check read as a 4.40% hardware
        disagreement. The dead slice belongs to nobody: no trigger fires in
        it, so no image is placed from it."""
        dmi, nav, triggers = park_after_first_trigger(hold_s=156.0, wander_m=1.5)
        cross = run_checks(dmi=dmi, nav=nav,
                           triggers=triggers)["content.realtime_vs_export"]
        assert cross.severity is Severity.PASS
        assert cross.values["dev_pct"] < 1.0
        assert cross.values["parked_s"] == pytest.approx(156.0, abs=2.0)
        assert "parked at the start" in cross.message

    def test_the_same_park_seen_over_the_whole_trigger_window_would_have_failed(self):
        """Proof the fix is load-bearing and not a threshold nudge: measured
        from the first trigger, that identical run is well past the limit."""
        dmi, nav, triggers = park_after_first_trigger(hold_s=156.0, wander_m=1.5)
        a, b = float(triggers.utc_s[0]), float(triggers.utc_s[-1])
        sbg = float(dmi.dist_at(b) - dmi.dist_at(a))
        export = float(nav.dist_at(b) - nav.dist_at(a))
        assert abs(sbg - export) / export * 100 > 2.0

    def test_a_real_disagreement_during_collection_still_warns(self):
        """Squeeze the export 5% during the moving part — that IS worth a flag."""
        dmi, nav, triggers = parked_then_moving(phantom_m=0.0)
        squashed = nav_from_along(nav.epoch_s, nav.along_m * 0.95)
        checks = run_checks(dmi=dmi, nav=squashed, triggers=triggers)
        cross = checks["content.realtime_vs_export"]
        assert cross.severity is Severity.WARN
        assert cross.values["dev_pct"] > 2.0


def gocator(encoder):
    encoder = np.asarray(encoder, dtype=np.int64)
    n = len(encoder)
    return GocatorIndex(
        path="synthetic", frame=np.arange(n, dtype=np.int64),
        ptp_us=(np.arange(n, dtype=np.int64) * 20_000) + 1,
        encoder=encoder, n_points=np.full(n, 400, dtype=np.int32),
        n_valid=np.full(n, 380, dtype=np.int32),
        byte_offset=np.arange(n, dtype=np.int64),
    )


class TestGocatorEncoderMessage:
    def test_shutdown_artifact_is_named_as_such(self):
        enc = np.arange(2512, dtype=np.int64) * 100
        enc[-1] -= 6528                       # the real 20260817.180042 signature
        checks = run_checks(gocator_l=gocator(enc))
        c = checks["content.gocator_L_encoder"]
        assert c.severity is Severity.WARN
        assert c.values["terminal_only"] is True
        assert c.values["n_drops"] == 1
        assert c.values["worst_counts"] == -6528 + 100
        assert "the logger stopping rather than travel" in c.message
        assert "nothing moves" in c.message
        # The encoder is a health signal, never a position source.
        assert c.impact_pct == 0.0

    def test_mid_run_drops_are_not_excused(self):
        """Several decreases scattered through a run really are spread."""
        enc = np.arange(500, dtype=np.int64) * 100
        for i in (120, 200, 310):
            enc[i] -= 5000
        checks = run_checks(gocator_l=gocator(enc))
        c = checks["content.gocator_L_encoder"]
        assert c.values["terminal_only"] is False
        assert c.values["n_drops"] == 3
        # Located in metres into the run, not by profile index: "3 m" is a
        # place you can drive to, "profile 119" is arithmetic homework.
        assert "3 of them, between" in c.message
        assert "m into a" in c.message
        assert "profile 119" not in c.message
        assert "spread through the run" in c.message
        assert "reverse travel or lost counts" in c.message

    def test_one_decrease_is_never_called_spread_through_the_run(self):
        """The contradiction this wording replaced. 20260821.130056 printed
        "1 encoder decrease(s) ... spread through the run" about a single
        -102 count step at profile 18435 of 19479. One event is somewhere; it
        is not spread. And a single 24 mm step back at 95% of a run is a
        different thing to chase than a scattering of them."""
        enc = np.arange(1000, dtype=np.int64) * 100
        enc[946] -= 202                       # one 24 mm step back, 95% through
        c = run_checks(gocator_l=gocator(enc))["content.gocator_L_encoder"]
        assert c.values["n_drops"] == 1
        assert c.values["terminal_only"] is False
        assert "spread through the run" not in c.message
        assert "95% through" in c.message
        assert "profile 945" not in c.message
        assert "a single step back and nothing else" in c.message

    def test_monotonic_encoder_passes(self):
        checks = run_checks(gocator_l=gocator(np.arange(300, dtype=np.int64) * 100))
        assert checks["content.gocator_L_encoder"].severity is Severity.PASS

    def test_one_notch_backwards_is_named_as_a_duplicate_not_a_gap(self):
        """A step back of exactly one trigger interval is the wheel dithering
        on a pulse edge while nearly stopped, and it leaves a DUPLICATE scan
        line. 20260821.130056: -102 counts against a 102-count trigger
        spacing, the profiles either side 1.2 s apart against ~0.1 s
        everywhere else, and encoder 3236154 appearing twice."""
        enc = np.arange(1000, dtype=np.int64) * 102
        enc[946] -= 102 * 2                   # lands exactly one notch back
        c = run_checks(gocator_l=gocator(enc))["content.gocator_L_encoder"]
        assert c.values["one_notch"] is True
        assert "duplicate scan line" in c.message
        assert "dithering on a pulse edge" in c.message


class TestNoiseSuppression:
    """Derek, 2026-09-13: "no collection is perfect... the warnings are noise
    to humans". A finding has to clear BOTH limits to go quiet."""

    def check(self, **kw):
        from core.qc import QCCheck, Severity as S
        return QCCheck("x", "x", S.WARN, "m", {}, None, **kw)

    def test_a_tiny_measured_finding_goes_quiet(self):
        assert self.check(impact_pct=0.08, worst_defect_m=0.36).is_noise

    def test_a_big_percentage_always_speaks(self):
        assert not self.check(impact_pct=2.0, worst_defect_m=0.1).is_noise

    def test_a_small_percentage_hiding_one_big_hole_still_speaks(self):
        """THE case the second limit exists for. 1 m missing out of 447 m is
        0.22% and would pass a percentage-only rule, but it is 1 m of sidewalk
        with no data that somebody downstream walks into."""
        assert not self.check(impact_pct=0.22, worst_defect_m=1.0).is_noise

    def test_an_unmeasured_finding_is_never_suppressed(self):
        """Silence has to be earned by a measurement, not by an omission —
        otherwise every check that forgets to report impact goes dark."""
        assert not self.check().is_noise
        assert not self.check(worst_defect_m=0.1).is_noise

    def test_zero_impact_needs_no_defect_measurement(self):
        assert self.check(impact_pct=0.0).is_noise

    def test_the_lead_in_is_free_at_any_size(self):
        """Ahead of the section start, so it is trimmed downstream."""
        assert self.check(impact_pct=5.0, worst_defect_m=9.0, lead_in=True).is_noise

    def test_a_failure_is_never_noise(self):
        from core.qc import QCCheck, Severity as S
        c = QCCheck("x", "x", S.FAIL, "m", {}, None, impact_pct=0.0)
        assert not c.is_noise

    def test_a_suppressed_finding_is_never_labelled_a_data_impact(self):
        """If we judged it not to matter, we do not then announce it as
        "DATA IMPACT: 0.24%". The label follows the DECISION, not the raw
        number (Derek, 2026-09-13: "You are calling 0.24 percent as data
        impact you should not"). The figure is still in the message for
        anyone who wants it."""
        c = self.check(impact_pct=0.24, worst_defect_m=1.08, lead_in=True)
        assert c.is_noise
        assert c.impact_text() == "NO DATA IMPACT"
        assert "DATA IMPACT:" not in c.impact_text()

    def test_a_real_finding_still_gets_its_number(self):
        """The two words have to stay worth reading when they do appear."""
        c = self.check(impact_pct=3.10, worst_defect_m=2.0)
        assert not c.is_noise
        assert c.impact_text().startswith("DATA IMPACT: 3.10%")

    def test_the_export_notes_column_carries_only_notable_warnings(self):
        """That column is repeated on EVERY ROW of the alignment CSV, so a
        suppressed finding there costs ~580 bytes per image — 400 KB of the
        same two sentences on 20260821.134240 — in the data file other
        processes read. And a notes column on every row is about as read as
        output gets, which is exactly what a no-impact finding must not be."""
        from core.qc import QCReport, Severity as S
        r = QCReport("run")
        r.add("a", "a", S.WARN, "a real hole", impact_pct=9.0, worst_defect_m=4.0)
        r.add("b", "b", S.WARN, "lead-in startup lag", impact_pct=0.3,
              worst_defect_m=1.2, lead_in=True)
        assert r.notes() == "a: a real hole"
        assert "lead-in" not in r.notes()

    def test_warned_still_holds_everything(self):
        """The two lists must always add up — issues.csv depends on it."""
        from core.qc import QCReport, Severity as S
        r = QCReport("run")
        r.add("a", "a", S.WARN, "loud", impact_pct=9.0, worst_defect_m=4.0)
        r.add("b", "b", S.WARN, "quiet", impact_pct=0.0)
        assert len(r.warned) == 2
        assert [c.check_id for c in r.notable] == ["a"]
        assert [c.check_id for c in r.suppressed] == ["b"]


class TestSharedDmiMerge:
    """Both Gocators are fed the same DMI pulses off a Y-split, so one wheel
    event was being reported twice and read like two faults (Derek,
    2026-09-13: "both lasers get the same DMI pulses it is a y connector?")."""

    def both_sides(self, enc_l, enc_r):
        return run_checks(gocator_l=gocator(enc_l), gocator_r=gocator(enc_r))

    def test_the_same_event_on_both_lasers_becomes_one_finding(self):
        enc = np.arange(1000, dtype=np.int64) * 102
        enc[946] -= 204
        # The two sensors start logging a moment apart, which is the ONLY
        # reason the profile index differed — the encoder value is identical.
        checks = self.both_sides(enc, enc.copy())
        assert "content.dmi_encoder" in checks
        assert "content.gocator_L_encoder" not in checks
        assert "content.gocator_R_encoder" not in checks
        assert checks["content.dmi_encoder"].values["sides"] == "L+R"
        assert "one wheel event and not two faults" in checks["content.dmi_encoder"].message

    def test_sides_that_disagree_are_kept_apart(self):
        """One encoder feed misbehaving on its own is a DIFFERENT fault and
        has to stay visible as two rows."""
        enc_l = np.arange(1000, dtype=np.int64) * 102
        enc_r = enc_l.copy()
        enc_l[946] -= 204
        enc_r[500] -= 204                     # different place entirely
        checks = self.both_sides(enc_l, enc_r)
        assert "content.dmi_encoder" not in checks
        assert checks["content.gocator_L_encoder"].severity is Severity.WARN
        assert checks["content.gocator_R_encoder"].severity is Severity.WARN

    def test_one_clean_side_is_not_merged(self):
        enc_l = np.arange(1000, dtype=np.int64) * 102
        enc_r = enc_l.copy()
        enc_l[946] -= 204                     # only the left misbehaves
        checks = self.both_sides(enc_l, enc_r)
        assert "content.dmi_encoder" not in checks
        assert checks["content.gocator_L_encoder"].severity is Severity.WARN
        assert checks["content.gocator_R_encoder"].severity is Severity.PASS


class TestTriggerDropouts:
    """A hole in the trigger train, which the SPACING FRACTION cannot see.

    20260819.190301: the camera stopped firing for 3.456 s while the cart
    drove on at 1.40 m/s - 4.8 m of sidewalk with no photographs and no
    profiles. One bad interval in 435 is 0.23% of them, so spacing_ok_frac
    stayed far above its 95% limit and the run reported clean. Same shape of
    mistake as measuring laser coverage as a percentage: a ratio hides a
    single large defect, and the defect is what matters.
    """

    def triggers_with_gap(self, gap_m=None, n=60, start_m=0.0):
        """A normal trigger train, optionally with one interval widened.

        `start_m` offsets the whole train, because dist_m is measured from
        the start of the EXPORT, not the run - a real run starts hundreds or
        thousands of metres in."""
        d = np.arange(n, dtype=float) * 0.75 + start_m
        if gap_m is not None:
            d[n // 2:] += gap_m - 0.75
        return TriggerData(
            sbg_us=np.zeros(n, dtype=np.int64), utc_s=T0 + d / 1.4,
            dist_m=d, spacing_m=np.diff(d), spacing_ok_frac=1.0, message="",
        )

    def check(self, triggers):
        dmi, nav, _ = parked_then_moving(phantom_m=0.0, move_s=60.0)
        return run_checks(dmi=dmi, nav=nav,
                          triggers=triggers)["content.trigger_gap"]

    def test_a_clean_train_passes(self):
        c = self.check(self.triggers_with_gap())
        assert c.severity is Severity.PASS
        assert c.values["n_gaps"] == 0

    def test_the_real_dropout_is_caught_and_located(self):
        c = self.check(self.triggers_with_gap(gap_m=4.797))
        assert c.severity is Severity.WARN
        assert c.values["n_gaps"] == 1
        assert c.values["worst_gap_m"] == pytest.approx(4.797, abs=0.01)
        assert "5 trigger(s) never fired" in c.message
        assert "that stretch has no images" in c.message
        assert "into the run" in c.message

    def test_it_does_not_claim_the_lasers_lost_anything(self):
        """The Gocators are triggered off the wheel encoder, not the camera
        sync line, so a camera dropout costs images only. This check said
        "no images and no profiles" until 2026-09-01, when the profiles over
        that exact window on 20260819.190301 were counted: 205 per side
        between 75.8 and 80.7 m at full 24 mm density, no holes. The laser's
        coverage is content.gocator_*_coverage and this check must not
        pronounce on it."""
        c = self.check(self.triggers_with_gap(gap_m=4.797))
        assert "no profiles" not in c.message
        assert "unaffected" in c.message

    def test_ordinary_jitter_does_not_fire(self):
        """Measured spacing on real runs runs 0.566 to 0.860 m. None of that
        may raise a warning, or the check is noise."""
        for g in (0.566, 0.75, 0.86, 1.4):
            assert self.check(self.triggers_with_gap(gap_m=g)).severity is Severity.PASS

    def test_one_missed_trigger_is_enough(self):
        """1.5 m is two intervals: exactly one trigger skipped."""
        c = self.check(self.triggers_with_gap(gap_m=1.51))
        assert c.severity is Severity.WARN
        assert "1 trigger(s) never fired" in c.message

    def test_the_position_is_measured_from_the_START_OF_THE_RUN(self):
        """The bug this caught on 20260819.190301: dist_m counts from the
        start of the EXPORT, so an unrebased number read "2169 m into the
        run" for a 330 m run. A location that cannot be true is worse than
        no location at all."""
        c = self.check(self.triggers_with_gap(gap_m=4.797, n=60, start_m=1840.0))
        assert c.severity is Severity.WARN
        at = float(c.message.split(" at ")[1].split(" m")[0])
        assert 0 <= at <= 60 * 0.75 + 5, f"reported {at} m into a ~45 m run"

    def test_the_fraction_check_would_have_missed_it(self):
        """The regression, stated as the reason this check exists: with one
        bad interval in sixty, spacing_ok_frac is still 98%."""
        n = 60
        assert (n - 2) / (n - 1) > 0.95
        c = self.check(self.triggers_with_gap(gap_m=4.797, n=n))
        assert c.severity is Severity.WARN, \
            "the gap check must fire where the fraction check cannot"


def gocator_over(nav, ptp_offset, d_from, d_to, hz=40.0):
    """A GocatorIndex whose profiles span [d_from, d_to] metres of `nav`."""
    from core.alignment import PtpOffsetResult
    along = nav.along_m
    t_lo = float(np.interp(d_from, along, nav.epoch_s))
    t_hi = float(np.interp(d_to, along, nav.epoch_s))
    t = np.arange(t_lo, t_hi, 1.0 / hz)
    n = len(t)
    enc = (np.interp(t, nav.epoch_s, along) * 4253.1).astype(np.int64)
    goc = GocatorIndex(
        path="g", frame=np.arange(n, dtype=np.int64),
        ptp_us=((t + ptp_offset) * 1e6).astype(np.int64), encoder=enc,
        n_points=np.full(n, 4, dtype=np.int32),
        n_valid=np.full(n, 4, dtype=np.int32),
        byte_offset=np.zeros(n, dtype=np.int64))
    ptp = PtpOffsetResult(offset_s=ptp_offset, r=0.99, passed=True, n_used=n,
                          sweep_offsets=np.empty(0), sweep_r=np.empty(0),
                          message="synthetic")
    return goc, ptp


class TestGocatorSectionCoverage:
    """The lasers start recording after the run has begun, so a section can
    have images with no profiles under them. Measured in METRES missing, not
    as a percentage of the run: it is a startup delay, so the same physical
    gap reads 8% on a 30 m section and 0.8% on a 300 m one, which would hide
    it on exactly the long sections where it is hardest to notice."""

    def _run(self, lead_m, run_m=30.0):
        dmi, nav, triggers = parked_then_moving(move_s=run_m + 5.0, speed=1.0,
                                                phantom_m=0.0)
        d0, d1 = float(triggers.dist_m[0]), float(triggers.dist_m[-1])
        goc, ptp = gocator_over(nav, 37.0, d0 + lead_m, d1 + 0.3)
        checks = run_checks(dmi=dmi, nav=nav, triggers=triggers,
                            gocator_l=goc, ptp_l=ptp)
        return checks["content.gocator_L_coverage"]

    def test_a_full_run_passes_and_says_so(self):
        c = self._run(lead_m=0.0)
        assert c.severity is Severity.PASS
        assert c.values["lead_m"] == pytest.approx(0.0, abs=0.2)

    def test_a_small_late_start_is_measured_but_allowed(self):
        c = self._run(lead_m=0.6)          # the left laser's typical miss
        assert c.severity is Severity.PASS
        assert c.values["lead_m"] == pytest.approx(0.6, abs=0.2)

    def test_a_late_start_past_the_limit_warns_and_says_where(self):
        c = self._run(lead_m=2.4)          # 101313's right laser
        assert c.severity is Severity.WARN
        assert c.values["lead_m"] == pytest.approx(2.4, abs=0.2)
        assert "at the start" in c.message

    def test_the_same_gap_is_caught_on_a_long_section_too(self):
        """The regression this replaces: as a ratio, 2.4 m of a 300 m section
        is 0.8% and passed silently, while the identical loss on a 30 m
        section failed. The limit is a distance, so both are caught."""
        short = self._run(lead_m=2.4, run_m=30.0)
        long = self._run(lead_m=2.4, run_m=200.0)
        assert short.severity is Severity.WARN
        assert long.severity is Severity.WARN
        assert long.values["lead_m"] == pytest.approx(short.values["lead_m"], abs=0.3)


class TestNavCoverageOfALaserIsScopedToThatLaser:
    """A laser whose profiles run past the export window loses ITS columns,
    not the run's images. This was the one laser check with no `blocks`, so
    it held everything - the images never touch the Gocator."""

    def test_the_fail_blocks_only_that_side(self):
        from core.qc import TARGET_IMAGES, TARGET_CSV, TARGET_GOCATOR_L, TARGET_GOCATOR_R
        dmi, nav, triggers = parked_then_moving(move_s=35.0, speed=1.0, phantom_m=0.0)
        d0, d1 = float(triggers.dist_m[0]), float(triggers.dist_m[-1])
        goc, ptp = gocator_over(nav, 37.0, d0, d1)
        # push the right laser's stamps 1000 s past the end of the export
        goc.ptp_us = goc.ptp_us + int(1000e6)
        run = disc.RunPaths(run_id="x", root="synthetic")
        pr = ParsedRun(run=run); pr.cameras = {}
        pr.dmi, pr.nav, pr.gocator_r = dmi, nav, goc
        report = QCReport(run_id="x")
        check_content(pr, report, triggers=triggers, ptp_r=ptp)
        c = {k.check_id: k for k in report.checks}["content.nav_covers_gocator_R"]
        assert c.severity is Severity.FAIL
        blocked = report.blocked_targets()
        assert TARGET_GOCATOR_R in blocked
        assert TARGET_IMAGES not in blocked and TARGET_CSV not in blocked
        assert TARGET_GOCATOR_L not in blocked
