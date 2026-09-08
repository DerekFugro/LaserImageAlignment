"""ACS's QC_Video.csv, and the cross-check against our own trigger gaps.

Derek pointed at this file on 2026-09-01 after I had proved the 20260819.190301
dropout the hard way - opening all 4,334 JPEGs in the collection to read their
EXIF. ACS had already written the same defect down in a three-line CSV:

    Session Name,Error,View,First File,Last File,Begin Chainage,End Chainage,Image Count
    [20260819.190301],Image missing,ROW,000000327000.JPG,000000331654.JPG,0.327,0.3316544,7
    [20260819.190301],Image missing,Rear,000000327000.JPG,000000331654.JPG,0.327,0.3316544,7

So these tests pin two things: that we can read it, and that we say something
useful when it and we disagree. The disagreement is the whole point - one
witness is not evidence.
"""
from __future__ import annotations

import numpy as np
import pytest

from core import discovery as disc
from core.alignment import TriggerData
from core.daily import find_qc_video_file, read_qc_video, video_gaps_for_run
from core.qc import ParsedRun, QCReport, Severity, check_acs_qc_video, trigger_gaps

T0 = 1787003858.0
RUN = "20260819.190301"

# The real file, verbatim from
# F:\Sidewalk\099_CollectedData\001_RoutedCollected\20260819_routed - Copy\20260819
REAL = (
    "Session Name,Error,View,First File,Last File,Begin Chainage,End Chainage,Image Count\n"
    "[20260819.190301],Image missing,ROW,000000327000.JPG,000000331654.JPG,"
    "0.327,0.331654491299577,7\n"
    "[20260819.190301],Image missing,Rear,000000327000.JPG,000000331654.JPG,"
    "0.327,0.331654491299577,7\n"
)
HEADER_ONLY = REAL.splitlines()[0] + "\n"


def collection(tmp_path, text=REAL, day="20260819"):
    """A collection root with an ACS day folder in it."""
    if text is not None:
        d = tmp_path / day
        d.mkdir(exist_ok=True)
        (d / "QC_Video.csv").write_text(text, encoding="utf-8")
    return tmp_path


def triggers_with_gap(gap_at_m=None, gap_m=4.797, n=600, start_m=1840.0):
    """A 0.75 m trigger train, optionally with one interval widened.

    start_m is non-zero on purpose: dist_m is measured from the start of the
    Qinertia EXPORT, not the run, so anything that forgets to rebase reports a
    gap hundreds of metres from where it really is.
    """
    d = np.arange(n, dtype=float) * 0.75 + start_m
    if gap_at_m is not None:
        d[int(gap_at_m / 0.75):] += gap_m - 0.75
    return TriggerData(
        sbg_us=np.zeros(n, dtype=np.int64), utc_s=T0 + (d - start_m) / 1.4,
        dist_m=d, spacing_m=np.diff(d), spacing_ok_frac=1.0, message="",
    )


def cross_check(root, triggers, run_id=RUN, n_recovered=0):
    run = disc.RunPaths(run_id=run_id, root=str(root))
    pr = ParsedRun(run=run)
    pr.cameras = {}
    report = QCReport(run_id=run_id)
    if n_recovered:
        # process_run() adds this BEFORE check_content, so the cross-check can
        # read how many pre-collection triggers sit at the head of the train.
        report.add("align.pre_collection_triggers", "Pre-collection triggers",
                   Severity.PASS, "", n_recovered=n_recovered)
    check_acs_qc_video(pr, report, triggers)
    return {c.check_id: c for c in report.checks}["content.acs_qc_video"]


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------

class TestReadQcVideo:
    def test_the_real_file(self, tmp_path):
        rows = read_qc_video(find_qc_video_file(collection(tmp_path)))
        assert len(rows) == 2
        row = rows[0]
        assert row.run_id == RUN
        assert row.view == "ROW"
        assert row.error == "Image missing"
        assert row.first_file == "000000327000.JPG"
        # chainage is KILOMETRES in the file and metres in our world. Getting
        # this backwards would put the hole 327 km into a 330 m run.
        assert row.begin_m == pytest.approx(327.0)
        assert row.end_m == pytest.approx(331.654, abs=0.001)
        assert row.length_m == pytest.approx(4.654, abs=0.001)
        assert row.count == 7

    def test_both_camera_views_are_kept_separately(self, tmp_path):
        rows = read_qc_video(find_qc_video_file(collection(tmp_path)))
        assert {r.view for r in rows} == {"ROW", "Rear"}

    def test_header_only_is_not_an_error(self, tmp_path):
        """ACS writes a header and no rows when it found nothing wrong. That
        is a real answer, not a broken file."""
        path = find_qc_video_file(collection(tmp_path, HEADER_ONLY))
        assert path is not None
        assert read_qc_video(path) == []

    def test_no_file_is_distinguishable_from_no_rows(self, tmp_path):
        """The distinction the check depends on: 'ACS found nothing' and 'ACS
        never told us' must never print the same sentence."""
        assert find_qc_video_file(collection(tmp_path, None)) is None

    def test_a_malformed_row_does_not_cost_us_the_good_ones(self, tmp_path):
        bad = REAL + "[20260819.190301],Image missing,ROW,a.JPG,b.JPG,notanumber,x,y\n"
        assert len(read_qc_video(find_qc_video_file(collection(tmp_path, bad)))) == 2

    def test_a_row_with_no_bracketed_stamp_is_skipped(self, tmp_path):
        bad = REAL.replace("[20260819.190301],Image missing,ROW",
                           "20260819,Image missing,ROW", 1)
        rows = read_qc_video(find_qc_video_file(collection(tmp_path, bad)))
        assert [r.view for r in rows] == ["Rear"]

    def test_a_missing_file_returns_no_rows_rather_than_raising(self, tmp_path):
        assert read_qc_video(tmp_path / "nope.csv") == []

    def test_rows_are_filtered_to_one_run_and_to_missing_images(self, tmp_path):
        text = REAL + (
            "[20260819.184113],Image missing,ROW,a.JPG,b.JPG,0.1,0.2,3\n"
            "[20260819.190301],Image too dark,ROW,c.JPG,d.JPG,0.5,0.6,9\n"
        )
        rows = read_qc_video(find_qc_video_file(collection(tmp_path, text)))
        assert len(rows) == 4
        mine = video_gaps_for_run(rows, RUN)
        assert len(mine) == 2                       # the two Image missing rows
        assert all(r.run_id == RUN for r in mine)
        assert all(r.error == "Image missing" for r in mine)


# ---------------------------------------------------------------------------
# rebasing - shared by content.trigger_gap and the cross-check
# ---------------------------------------------------------------------------

class TestTriggerGaps:
    def test_a_clean_train_has_none(self):
        assert trigger_gaps(triggers_with_gap()) == []

    def test_the_gap_is_reported_from_the_start_of_the_RUN(self):
        """The bug this function exists to prevent: dist_m starts at the
        export's origin, so an un-rebased gap at 327 m reads as 2167 m."""
        gaps = trigger_gaps(triggers_with_gap(gap_at_m=327.0, start_m=1840.0))
        assert len(gaps) == 1
        assert gaps[0][0] == pytest.approx(327.0, abs=0.75)
        assert gaps[0][1] - gaps[0][0] == pytest.approx(4.797, abs=0.01)

    def test_the_recovered_pre_collection_prefix_is_not_a_dropout(self):
        """20260824.101313, verified against the real collection on
        2026-09-01. One pre-collection trigger is recovered from the Qinertia
        event export and prepended; the operator then rolled 2.19 m before ACS
        started logging, so the FIRST interval is over the 1.5 m limit. That is
        not a camera fault, and ACS's own QC_Video.csv said nothing was wrong
        with the run - which is how this false positive was caught.
        """
        t = triggers_with_gap(gap_at_m=0.75, gap_m=2.19)   # the first interval
        assert len(trigger_gaps(t)) == 1                    # seen without the skip
        assert trigger_gaps(t, skip_leading=1) == []        # and not with it

    def test_skip_leading_does_not_hide_a_real_dropout_further_in(self):
        t = triggers_with_gap(gap_at_m=327.0)
        assert len(trigger_gaps(t, skip_leading=1)) == 1

    def test_an_empty_train_is_not_a_crash(self):
        empty = TriggerData(sbg_us=np.array([], dtype=np.int64), utc_s=np.array([]),
                            dist_m=np.array([]), spacing_m=np.array([]),
                            spacing_ok_frac=1.0, message="")
        assert trigger_gaps(empty) == []


# ---------------------------------------------------------------------------
# the cross-check
# ---------------------------------------------------------------------------

class TestCrossCheck:
    def test_the_real_case_the_amounts_agree(self, tmp_path):
        """20260819.190301, as collected. ACS is 4.654 m short; we measure a
        4.798 m trigger gap. Same defect to within a fifth of one image, so
        this is agreement and must PASS - the defect itself is reported once,
        by content.trigger_gap, not twice."""
        c = cross_check(collection(tmp_path), triggers_with_gap(gap_at_m=327.0))
        assert c.severity is Severity.PASS
        assert "agrees with ACS" in c.message
        assert c.values["acs_len_m"] == pytest.approx(4.654, abs=0.01)
        assert c.values["our_len_m"] == pytest.approx(4.797, abs=0.01)

    def test_the_place_no_longer_decides_agreement(self, tmp_path):
        """The change this file exists to pin. ACS's chainage is a count, not
        a place: its row for 190301 reads 327-332 m while the hole is at 76 m.
        Comparing positions turned the one run where the two systems agreed
        into a WARN. Wherever we put the gap, the amounts still agree."""
        for at in (5.0, 76.0, 150.0, 300.0):
            c = cross_check(collection(tmp_path), triggers_with_gap(gap_at_m=at))
            assert c.severity is Severity.PASS, f"gap at {at} m"

    def test_the_two_camera_rows_are_one_shortfall_not_two(self, tmp_path):
        """ACS writes an identical row per view - ROW and Rear. Summing them
        raw would call it 9.3 m and disagree with our 4.8 m."""
        c = cross_check(collection(tmp_path), triggers_with_gap(gap_at_m=76.0))
        assert c.values["n_acs"] == 2                      # two rows read
        assert c.values["acs_len_m"] == pytest.approx(4.654, abs=0.01)   # one loss

    def test_acs_is_short_and_we_saw_nothing(self, tmp_path):
        """The case that matters most: ACS asked for the pictures, so if it
        counts them missing and we see a clean trigger train, we are the ones
        who are blind."""
        c = cross_check(collection(tmp_path), triggers_with_gap())
        assert c.severity is Severity.WARN
        assert "found no trigger gap at all" in c.message
        assert "7 image(s) per camera" in c.message
        assert "000000327000.JPG" in c.message
        # and it says plainly that ACS cannot supply the location
        assert "cannot say WHERE" in c.message

    def test_we_saw_a_gap_and_acs_is_not_short(self, tmp_path):
        c = cross_check(collection(tmp_path, HEADER_ONLY),
                        triggers_with_gap(gap_at_m=327.0))
        assert c.severity is Severity.WARN
        assert "ACS reports nothing missing" in c.message
        assert c.values["our_len_m"] == pytest.approx(4.797, abs=0.01)
        assert "326 m into the run" in c.message           # we DO have the place

    def test_amounts_that_really_differ_still_warn(self, tmp_path):
        """A 30 m hole where ACS is 4.65 m short is not one defect described
        twice. More than one image apart, so it is a real disagreement."""
        c = cross_check(collection(tmp_path),
                        triggers_with_gap(gap_at_m=40.0, gap_m=30.0))
        assert c.severity is Severity.WARN
        assert "disagree on how much is missing" in c.message
        assert c.values["our_len_m"] == pytest.approx(30.0, abs=0.01)

    def test_the_tolerance_is_one_image(self, tmp_path):
        """Just inside and just outside 0.750 m, so the limit is pinned rather
        than implied."""
        assert cross_check(collection(tmp_path),
                           triggers_with_gap(gap_at_m=76.0, gap_m=5.30)
                           ).severity is Severity.PASS    # 0.65 m apart
        assert cross_check(collection(tmp_path),
                           triggers_with_gap(gap_at_m=76.0, gap_m=5.50)
                           ).severity is Severity.WARN    # 0.85 m apart

    def test_a_run_with_no_rows_of_its_own(self, tmp_path):
        """The other three runs of 20260819: the file exists and names another
        run, which for them means ACS found nothing."""
        c = cross_check(collection(tmp_path), triggers_with_gap(),
                        run_id="20260819.184113")
        assert c.severity is Severity.PASS
        assert c.values["n_acs"] == 0

    def test_no_qc_video_file_is_INFO_not_PASS(self, tmp_path):
        """Every collection before ACS wrote this file lands here. Silence
        from a system that was never asked is not a clean bill of health."""
        c = cross_check(collection(tmp_path, None), triggers_with_gap())
        assert c.severity is Severity.INFO
        assert "no QC_Video.csv" in c.message

    def test_no_triggers_still_reports_what_acs_said(self, tmp_path):
        """A run whose triggers failed to parse is exactly when ACS's own
        count is most worth showing."""
        c = cross_check(collection(tmp_path), None)
        assert c.severity is Severity.INFO
        assert c.values["n_acs"] == 2

    def test_the_head_of_run_false_positive_no_longer_disagrees_with_acs(self, tmp_path):
        """The whole cross-check justifying itself. 20260824.101313: ACS said
        the run was clean, we called the 2.19 m pre-collection roll-up a
        dropout, and the disagreement was the bug - ours, not ACS's."""
        t = triggers_with_gap(gap_at_m=0.75, gap_m=2.19)
        assert cross_check(collection(tmp_path, HEADER_ONLY), t).severity is Severity.WARN
        c = cross_check(collection(tmp_path, HEADER_ONLY), t, n_recovered=1)
        assert c.severity is Severity.PASS
        assert c.values["n_ours"] == 0

    def test_the_check_never_blocks_anything(self, tmp_path):
        for triggers in (triggers_with_gap(), triggers_with_gap(gap_at_m=327.0)):
            c = cross_check(collection(tmp_path), triggers)
            assert c.severity is not Severity.FAIL
