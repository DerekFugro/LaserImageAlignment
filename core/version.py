"""Which version of the app is running - for the batch report.

WHY (Derek, 2026-10-20): a collection processed on the VM came back and
nobody could say which code had done it. The report now says, so a batch
report on its own answers "what processed this?".
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def _package_version() -> str:
    try:
        text = (APP_DIR / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "?"
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "?"


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(APP_DIR), *args], capture_output=True,
                           text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


@lru_cache(maxsize=1)
def app_version() -> str:
    """e.g. '0.3.0, commit 551b8fd (2026-10-20)', with
    ' + UNCOMMITTED CHANGES' when the code differs from that commit, or
    'commit unknown' when this copy is not a git checkout."""
    ver = _package_version()
    commit = _git("log", "-1", "--format=%h (%ad)", "--date=short")
    if not commit:
        return f"{ver}, commit unknown (not a git checkout, or git not found)"
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return f"{ver}, commit {commit}" + (" + UNCOMMITTED CHANGES" if dirty else "")
