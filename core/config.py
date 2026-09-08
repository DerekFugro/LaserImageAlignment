"""Where this installation keeps its folders — `lia.ini`, beside the app.

WHY THIS EXISTS: the calibrations folder used to be a constant compiled into
`discovery.py` (`F:\\Sidewalk\\002_App\\AllCalibrations`). On any other machine
that path does not exist, and since the lever-arm file lives there and is a
REQUIRED input, every run was refused with `missing lever_arms`. The CLI could
work around it with `--calibrations`; the GUI had no way at all, short of
editing a registry key nobody can see.

So: one plain INI next to `app.py`, which a person can open and read.

    [paths]
    calibrations = F:\\Sidewalk\\002_App\\AllCalibrations
    collections  = F:\\Sidewalk\\099_CollectedData

Resolution order, most explicit first:

    1. what the caller passed  (`--calibrations`, or the GUI's own setting)
    2. the environment - LIA_CALIBRATIONS / LIA_COLLECTIONS
    3. lia.ini
    4. the built-in default, so an installation with no INI at all behaves
       exactly as it did before this module existed

The environment layer is there for the cases where editing a file beside the
app is the wrong shape: a test run that must not depend on what this machine
happens to have in its INI, a scheduled job, a second checkout pointed at a
different calibrations folder. It is deliberately ABOVE the INI - somebody who
sets the variable is being more specific than the file.

Nothing here raises. A missing INI, an unreadable one, a typo'd key, a blank
value - all fall through to the next source. A config file that can stop the
app from starting is worse than no config file.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path

INI_NAME = "lia.ini"
SECTION = "paths"

#: key in [paths]  ->  environment variable that outranks it
ENV_VARS = {"calibrations": "LIA_CALIBRATIONS", "collections": "LIA_COLLECTIONS"}


def app_dir() -> Path:
    """The folder holding app.py / cli.py — this file's parent's parent."""
    return Path(__file__).resolve().parent.parent


def ini_path() -> Path:
    return app_dir() / INI_NAME


def load(ini: Path | None = None) -> dict:
    """`{key: value}` from the INI's [paths], or {} if there isn't a usable one.

    Blank values are dropped here rather than downstream, so a key left empty
    behaves the same as a key that was never written - which is what somebody
    clearing a line out expects.
    """
    path = Path(ini) if ini else ini_path()
    if not path.is_file():
        return {}
    parser = configparser.ConfigParser()
    try:
        # utf-8-sig: Notepad writes a BOM, and configparser treats it as part
        # of the first section name if it is not stripped.
        parser.read(path, encoding="utf-8-sig")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    if not parser.has_section(SECTION):
        return {}
    return {k: v.strip() for k, v in parser.items(SECTION) if v.strip()}


def _resolve(key: str, given, fallback, ini: Path | None = None):
    if given:
        return Path(given), "given"
    name = ENV_VARS.get(key)
    env = os.environ.get(name, "").strip() if name else ""
    if env:
        return Path(env), f"${name}"
    value = load(ini).get(key)
    if value:
        return Path(value), str(ini or ini_path())
    return (Path(fallback) if fallback else None), "built-in default"


def calibrations_dir(given=None, ini: Path | None = None) -> Path:
    """The AllCalibrations folder. See the module docstring for the order."""
    from .discovery import DEFAULT_CALIBRATIONS_DIR
    return _resolve("calibrations", given, DEFAULT_CALIBRATIONS_DIR, ini)[0]


def calibrations_source(given=None, ini: Path | None = None) -> str:
    """Where that answer came from, so a report can say so. 'the arms came
    from the file' and 'the file was not there' must never look the same."""
    return _resolve("calibrations", given, None, ini)[1]


def collections_dir(given=None, ini: Path | None = None):
    """Where the viewer's Open dialog starts. None means 'no opinion' - the
    GUI then uses the last folder opened, and failing that the home folder.
    Unlike the calibrations folder this one is only a convenience, so there
    is deliberately no built-in default to be wrong about."""
    return _resolve("collections", given, None, ini)[0]
