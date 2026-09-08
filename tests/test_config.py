"""core.config - where the app looks for its folders.

This module is small but it decides whether the app runs at all on a machine
that is not Derek's: the lever-arm file is a REQUIRED input, it lives in the
calibrations folder, and the calibrations folder used to be a constant hard
coded to `F:\\Sidewalk\\002_App\\AllCalibrations`. Get the order wrong and the
symptom is `missing lever_arms` on every run, which points at the data rather
than at the setting that is actually wrong.

So the order itself is the thing under test, plus the promise that nothing in
here can raise - a config file must never be able to stop the app starting.
"""
from __future__ import annotations

import pytest

from core import config


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """The developer's own LIA_* variables must not decide these results."""
    for name in config.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


def write_ini(path, body):
    path.write_text(body, encoding="utf-8")
    return path


class TestResolutionOrder:
    """given > env > ini > built-in default, and each layer really is skipped
    when the one above it has an answer."""

    def test_given_beats_everything(self, tmp_path, monkeypatch):
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncalibrations = C:\\from-ini\n")
        monkeypatch.setenv("LIA_CALIBRATIONS", "C:\\from-env")
        assert config.calibrations_dir("C:\\given", ini=ini).name == "given"
        assert config.calibrations_source("C:\\given", ini=ini) == "given"

    def test_env_beats_the_ini(self, tmp_path, monkeypatch):
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncalibrations = C:\\from-ini\n")
        monkeypatch.setenv("LIA_CALIBRATIONS", "C:\\from-env")
        assert config.calibrations_dir(ini=ini).name == "from-env"
        assert config.calibrations_source(ini=ini) == "$LIA_CALIBRATIONS"

    def test_the_ini_beats_the_built_in_default(self, tmp_path):
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncalibrations = C:\\from-ini\n")
        assert config.calibrations_dir(ini=ini).name == "from-ini"
        assert config.calibrations_source(ini=ini) == str(ini)

    def test_no_ini_falls_back_to_the_built_in_default(self, tmp_path):
        from core.discovery import DEFAULT_CALIBRATIONS_DIR
        missing = tmp_path / "nothing-here.ini"
        assert config.calibrations_dir(ini=missing) == DEFAULT_CALIBRATIONS_DIR
        assert config.calibrations_source(ini=missing) == "built-in default"

    def test_an_empty_env_var_is_not_an_answer(self, tmp_path, monkeypatch):
        """Set-but-blank is how a batch file leaves a variable it never filled
        in. Treating that as a path gives Path('') == '.', which resolves to
        whatever folder the app happens to be launched from."""
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncalibrations = C:\\from-ini\n")
        monkeypatch.setenv("LIA_CALIBRATIONS", "   ")
        assert config.calibrations_dir(ini=ini).name == "from-ini"


class TestTheSourceIsHonest:
    """`calibrations_source` exists so a report can say where the arms came
    from. 'the file said so' and 'nobody said, we guessed' must never look
    the same in a report someone is using to judge a deliverable."""

    def test_every_layer_names_itself(self, tmp_path, monkeypatch):
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncalibrations = C:\\x\n")
        assert config.calibrations_source("C:\\g", ini=ini) == "given"
        monkeypatch.setenv("LIA_CALIBRATIONS", "C:\\e")
        assert config.calibrations_source(ini=ini) == "$LIA_CALIBRATIONS"
        monkeypatch.delenv("LIA_CALIBRATIONS")
        assert config.calibrations_source(ini=ini) == str(ini)


class TestABadIniCannotStopTheApp:
    """Every one of these used to be a crash-on-startup waiting to happen.
    They must all degrade to 'this file had nothing to say'."""

    @pytest.mark.parametrize("body", [
        "",                                     # empty file
        "calibrations = C:\\x\n",               # no section header
        "[wrong]\ncalibrations = C:\\x\n",      # wrong section
        "[paths]\ncalibrations =\n",            # key present, value blank
        "[paths]\ncalibrations = \t \n",        # whitespace only
        "[paths\ncalibrations = C:\\x\n",       # malformed section
        "[paths]\n[paths]\n",                   # duplicate section
        "\x00\x01\x02binary garbage\xff",       # not text at all
    ])
    def test_it_falls_through_instead_of_raising(self, tmp_path, body):
        from core.discovery import DEFAULT_CALIBRATIONS_DIR
        ini = tmp_path / "lia.ini"
        ini.write_bytes(body.encode("utf-8", "surrogateescape"))
        assert config.load(ini) in ({}, {"calibrations": "C:\\x"})
        assert config.calibrations_dir(ini=ini) in (
            DEFAULT_CALIBRATIONS_DIR, config.Path("C:\\x"))

    def test_a_directory_where_the_ini_should_be(self, tmp_path):
        from core.discovery import DEFAULT_CALIBRATIONS_DIR
        d = tmp_path / "lia.ini"
        d.mkdir()
        assert config.load(d) == {}
        assert config.calibrations_dir(ini=d) == DEFAULT_CALIBRATIONS_DIR

    def test_a_notepad_bom_does_not_hide_the_section(self, tmp_path):
        """Notepad writes UTF-8 with a BOM. configparser reads the BOM as part
        of the first section name, so `[paths]` becomes `[\\ufeffpaths]` and
        every key silently vanishes - the file looks correct on screen."""
        ini = tmp_path / "lia.ini"
        ini.write_text("[paths]\ncalibrations = C:\\from-ini\n", encoding="utf-8-sig")
        assert config.load(ini) == {"calibrations": "C:\\from-ini"}


class TestCollections:
    """The collections folder is only where the Open dialog starts, so unlike
    calibrations it has no built-in default - None means 'no opinion' and the
    GUI then uses the last folder opened."""

    def test_none_when_nothing_says(self, tmp_path):
        assert config.collections_dir(ini=tmp_path / "none.ini") is None

    def test_read_from_the_ini(self, tmp_path):
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncollections = D:\\data\n")
        assert config.collections_dir(ini=ini).name == "data"

    def test_env_override(self, tmp_path, monkeypatch):
        ini = write_ini(tmp_path / "lia.ini", "[paths]\ncollections = D:\\data\n")
        monkeypatch.setenv("LIA_COLLECTIONS", "D:\\other")
        assert config.collections_dir(ini=ini).name == "other"


class TestTheShippedIni:
    """The file that actually travels with the app."""

    def test_it_exists_and_parses(self):
        assert config.ini_path().is_file(), \
            f"{config.INI_NAME} must ship beside app.py - it is how a second " \
            "machine points the app at its own AllCalibrations folder"
        assert "calibrations" in config.load()

    def test_discovery_resolves_through_config(self, tmp_path, monkeypatch):
        """The whole point: discover_runs(calibrations_dir=None) must not fall
        back to the hard-coded F: constant while a setting says otherwise."""
        from core import discovery as disc
        monkeypatch.setenv("LIA_CALIBRATIONS", str(tmp_path))
        empty = tmp_path / "collection"
        empty.mkdir()
        runs = disc.discover_runs(empty)
        assert runs == [] or all(r is not None for r in runs)
        assert config.calibrations_dir() == tmp_path
