"""Every camera fires on the same trigger, so every camera must hold the same
number of photographs.

Nothing checked that. On 20260824_revruns_FullProcessingRun, Rear held 38
images where ROW held 43 - five stranded mid-rename - and the entire report
said "image/trigger count difference is -5". That names neither the camera
nor what the number should have been, and the five files were sitting right
there under a name nothing looked for.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.formats import ImageSet
from core.qc import ParsedRun, QCReport, Severity, check_content
from core import discovery as disc


def images(n, start_mm=750, step=750, aside=0):
    """An ImageSet of `n` files on a clean counter, plus `aside` filed away."""
    counter = np.arange(n, dtype=np.int64) * step + start_mm
    im = ImageSet(
        dir="synthetic",
        files=[f"{c:012d}.jpg" for c in counter],
        counter_mm=counter,
    )
    im.n_set_aside = aside
    return im


def run_checks(cameras, primary=None):
    run = disc.RunPaths(run_id="20260824.100258", root="synthetic")
    pr = ParsedRun(run=run)
    pr.cameras = cameras
    pr.images = primary
    report = QCReport(run_id=run.run_id)
    check_content(pr, report)
    return {c.check_id: c for c in report.checks}


class TestCamerasMustAgree:
    def test_matching_counts_pass(self):
        c = run_checks({"Rear": images(43), "ROW": images(43)})
        assert c["content.camera_counts"].severity is Severity.PASS

    def test_a_short_camera_is_named_with_both_numbers(self):
        """The whole point: say WHICH camera and HOW MANY, not a bare -5."""
        c = run_checks({"Rear": images(38), "ROW": images(43)})["content.camera_counts"]
        assert c.severity is Severity.WARN
        assert "Rear" in c.message and "38" in c.message and "43" in c.message
        assert "missing 5" in c.message
        assert c.values["counts"] == {"Rear": 38, "ROW": 43}

    def test_set_aside_images_still_count(self):
        """The regression this check would otherwise fire on EVERY run: ROW's
        arm is 0.76 m longer than Rear's, so its footprint reaches further
        back and it routinely files one more image into BeforeCollection. The
        run is intact; only the split moved."""
        c = run_checks({"Rear": images(41, aside=3), "ROW": images(40, aside=4)})
        assert c["content.camera_counts"].severity is Severity.PASS

    def test_a_real_loss_is_still_caught_when_some_are_set_aside(self):
        c = run_checks({"Rear": images(35, aside=3), "ROW": images(40, aside=4)})
        assert c["content.camera_counts"].severity is Severity.WARN

    def test_one_camera_alone_has_nothing_to_disagree_with(self):
        c = run_checks({"Rear": images(43)})
        assert c["content.camera_counts"].severity is Severity.PASS

    def test_no_cameras_raises_nothing(self):
        assert "content.camera_counts" not in run_checks({})


class TestEveryCameraGetsASequenceCheck:
    """The existing check only ever looked at the PRIMARY camera, so a hole
    in ROW went unexamined. Matching is tail-anchored, so images missing from
    the START line up anyway - a gap in the MIDDLE is the dangerous kind,
    because everything before it shifts against the triggers."""

    def test_a_hole_in_the_second_camera_is_found(self):
        rear = images(43)
        holed = images(43)
        keep = [i for i in range(43) if i not in (10, 11)]
        holed.files = [holed.files[i] for i in keep]
        holed.counter_mm = holed.counter_mm[keep]
        c = run_checks({"Rear": rear, "ROW": holed}, primary=rear)
        assert c["content.image_sequence.ROW"].severity is Severity.WARN
        assert c["content.image_sequence.ROW"].values["missing"]

    def test_a_clean_second_camera_passes(self):
        rear = images(43)
        c = run_checks({"Rear": rear, "ROW": images(43)}, primary=rear)
        assert c["content.image_sequence.ROW"].severity is Severity.PASS

    def test_the_primary_is_not_reported_twice(self):
        rear = images(43)
        c = run_checks({"Rear": rear, "ROW": images(43)}, primary=rear)
        assert "content.image_sequence" in c          # the original id
        assert "content.image_sequence.Rear" not in c

    def test_the_primary_is_not_reported_twice_through_the_real_scan(self, tmp_path):
        """The test above hands check_content the SAME ImageSet object twice,
        so the `is` guard it exercises is one production never reaches.

        parse_all used to scan the primary folder a second time on its way
        through run.cameras, giving two equal-but-distinct ImageSets - and
        every run then reported content.image_sequence AND
        content.image_sequence.Rear with identical contents. Found in the
        pre-ship audit, 2026-09-01. Go through the real scan so the object
        identity is whatever production makes it.
        """
        from core.qc import parse_all

        cam = tmp_path / "Images" / "20260824.100258" / "Rear"
        cam.mkdir(parents=True)
        for i in range(1, 6):
            (cam / f"{i * 750:012d}.jpg").write_bytes(b"")
        run = disc.RunPaths(run_id="20260824.100258", root=str(tmp_path),
                            paths={disc.KEY_IMAGES: cam}, cameras={"Rear": cam})
        report = QCReport(run_id=run.run_id)
        pr = parse_all(run, report)
        assert pr.images is not None and pr.cameras["Rear"] is not None
        assert pr.cameras["Rear"] is pr.images, \
            "the primary folder must be scanned once, not twice"
        check_content(pr, report)
        ids = [c.check_id for c in report.checks]
        assert "content.image_sequence" in ids
        assert "content.image_sequence.Rear" not in ids
