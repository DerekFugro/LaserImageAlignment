"""The lens-height heading, and the silent fallback it used to cause.

THE BUG (2026-09-04): the parser matched the lens-height section with
`low.startswith("rear lens height")`. Derek — told to keep prose out of that
file — trimmed the heading to plain `## Rear`. Nothing matched, the 1.719 under
it was discarded, and `lens_height_m()` went on returning the built-in 1.667.
The file said one thing and the app did another, with no warning anywhere.

Two things are under test: that the heading he actually writes is understood,
and that a number the parser does NOT understand is complained about instead of
vanishing. The second matters more than the first — the next surprise will be a
heading nobody has thought of yet.
"""
from __future__ import annotations

import pytest

from core.calibration import (
    PAVE_LENS_HEIGHT_M, lens_height_m, load_lever_arms_file, lever_arm_warnings,
    parse_lever_arms, parse_lever_arms_checked, LEVER_ARMS_FILENAME,
)

ARMS = (
    "# LaserImageAlignment lever arms\n\n"
    "## Rear camera\n\nX  -0.866\nY   0\nZ   0\n\n"
    "## ROW camera\n\nX  -1.00\nY   0\nZ   0\n\n"
    "## Gocator left\n\nX   0\nY  -0.3575\nZ   0\n\n"
    "## Gocator right\n\nX   0\nY  +0.3575\nZ   0\n\n"
)


def cal_dir(tmp_path, text):
    d = tmp_path / "cal"
    d.mkdir(parents=True, exist_ok=True)
    (d / LEVER_ARMS_FILENAME).write_text(text, encoding="utf-8")
    return d


class TestTheHeadingDerekActuallyWrites:

    @pytest.mark.parametrize("heading", [
        "## Rear",
        "## rear",
        "##   Rear   ",
        "## Rear lens height above the pavement",
        "## Rear lens height",
        "## Lens height",
        "# Rear",
    ])
    def test_it_is_read(self, tmp_path, heading):
        d = cal_dir(tmp_path, ARMS + f"{heading}\n\n1.719\n")
        assert lens_height_m(d) == pytest.approx(1.719)

    def test_the_prose_line_above_the_number_does_not_break_it(self, tmp_path):
        """His file carries one warning line between heading and number."""
        d = cal_dir(tmp_path, ARMS + (
            "## Rear\n\n"
            "Effective value for images taken in motion (mount tilts ~12 deg). "
            "Physical is ~1.65 - do not put that here.\n\n"
            "1.719\n"))
        assert lens_height_m(d) == pytest.approx(1.719)


class TestItCannotEatTheRearArm:
    """`startswith("rear")` would match `Rear camera` too, turn the arm into a
    height, and MOVE EVERY WRITTEN POSITION. The match must be exact."""

    def test_rear_camera_is_still_the_camera(self, tmp_path):
        d = cal_dir(tmp_path, ARMS + "## Rear\n\n1.719\n")
        arms = load_lever_arms_file(d / LEVER_ARMS_FILENAME)
        assert arms["Rear"] == (-0.866, 0.0, 0.0)
        assert arms["ROW"] == (-1.0, 0.0, 0.0)
        assert arms["L"] == (0.0, -0.3575, 0.0)
        assert arms["R"] == (0.0, 0.3575, 0.0)

    def test_the_lens_section_is_not_exported_as_a_camera(self, tmp_path):
        arms = parse_lever_arms(ARMS + "## Rear\n\n1.719\n")
        assert set(arms) == {"Rear", "ROW", "L", "R", "#lens_height"}
        # "#lens_height" starts with '#', which no camera name ever can
        assert not any(k.lower() == "rear" and k != "Rear" for k in arms)


class TestADiscardedNumberIsComplainedAbout:
    """The regression that matters. A number under a heading the parser does
    not know is a number the app is not using while the file claims it is."""

    def test_an_unknown_heading_with_a_number_warns(self):
        arms, warns = parse_lever_arms_checked(
            ARMS + "## Front lens height of the thing\n\n1.234\n"
                   "## Mystery\n\n9.99\n")
        assert len(warns) == 1
        assert "9.99" in warns[0]
        assert "Mystery" in warns[0]

    def test_a_healthy_file_warns_about_nothing(self):
        _, warns = parse_lever_arms_checked(ARMS + "## Rear\n\n1.719\n")
        assert warns == []

    def test_the_axis_lines_are_not_mistaken_for_stray_numbers(self):
        _, warns = parse_lever_arms_checked(ARMS)
        assert warns == []

    def test_warnings_from_a_file_on_disk(self, tmp_path):
        d = cal_dir(tmp_path, ARMS + "## Nonsense\n\n4.2\n")
        w = lever_arm_warnings(d / LEVER_ARMS_FILENAME)
        assert len(w) == 1 and "4.2" in w[0]

    def test_a_missing_file_is_not_an_error_here(self, tmp_path):
        assert lever_arm_warnings(tmp_path / "nope.md") == []


class TestNoFolderGivenUsesTheInstallationsOwn:
    """Called with no argument it used to skip straight to the constant, even
    with a perfectly good file configured (closed 2026-09-08). Same shape as
    the two bugs this file has already had: a number sitting right there,
    ignored, silently."""

    def test_it_reads_the_configured_folder(self, tmp_path, monkeypatch):
        d = cal_dir(tmp_path, ARMS + "## Rear\n\n1.719\n")
        monkeypatch.setenv("LIA_CALIBRATIONS", str(d))
        assert lens_height_m() == pytest.approx(1.719)

    def test_an_explicit_folder_still_wins(self, tmp_path, monkeypatch):
        env = cal_dir(tmp_path / "a", ARMS + "## Rear\n\n1.719\n")
        given = cal_dir(tmp_path / "b", ARMS + "## Rear\n\n1.500\n")
        monkeypatch.setenv("LIA_CALIBRATIONS", str(env))
        assert lens_height_m(given) == pytest.approx(1.500)

    def test_a_configured_folder_with_no_file_still_falls_back(
            self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("LIA_CALIBRATIONS", str(empty))
        assert lens_height_m() == pytest.approx(PAVE_LENS_HEIGHT_M)


class TestTheWarningReachesTheBatchReport:
    """The parser can complain all it likes; if the complaint never reaches
    the report Derek reads, the bug is still silent. This is the end-to-end
    half — a real batch, a real report, the warning in it."""

    def test_a_stray_number_is_named_in_the_report(
            self, synth_run_root, synth_cal_dir, tmp_path):
        from core.batch import process_collection
        from core.discovery import OverrideStore

        f = synth_cal_dir / LEVER_ARMS_FILENAME
        f.write_text(f.read_text(encoding="utf-8")
                     + "\n## Rear lens hieght\n\n1.719\n", encoding="utf-8")

        report = process_collection(
            synth_run_root, calibrations_dir=synth_cal_dir,
            overrides=OverrideStore(tmp_path / "ov.json"),
            write_images=False, write_gocator=False, write_csv=False)
        text = report.to_text()
        assert "WARNING" in text, text[:2000]
        assert "1.719" in text
        assert LEVER_ARMS_FILENAME in text

    def test_a_clean_file_puts_no_warning_in_the_report(
            self, synth_run_root, synth_cal_dir, tmp_path):
        from core.batch import process_collection
        from core.discovery import OverrideStore

        report = process_collection(
            synth_run_root, calibrations_dir=synth_cal_dir,
            overrides=OverrideStore(tmp_path / "ov.json"),
            write_images=False, write_gocator=False, write_csv=False)
        assert "WARNING" not in report.to_text()


class TestTheFallbackStillExists:
    """Falling back is correct for this one reader — the viewer must draw
    something. What was wrong was falling back in silence."""

    def test_no_lens_section_gives_the_built_in(self, tmp_path):
        d = cal_dir(tmp_path, ARMS)
        assert lens_height_m(d) == pytest.approx(PAVE_LENS_HEIGHT_M)

    def test_and_says_nothing_because_nothing_was_discarded(self, tmp_path):
        d = cal_dir(tmp_path, ARMS)
        assert lever_arm_warnings(d / LEVER_ARMS_FILENAME) == []
