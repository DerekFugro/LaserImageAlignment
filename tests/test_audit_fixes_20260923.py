"""Fixes from the 2026-09-23 code audit.

1. Each run gets the Qinertia export of ITS OWN SBG session, not the first
   one found - a multi-day upload skipped every day after the first.
2. A rename that did not finish is not a clean exit.
3. Every non-zero exit ends with one reason line on stderr (contract rule 4).
4. The files other processes read are written atomically (contract rule 12).
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from core import discovery as disc
from core.atomic import WRITING_SUFFIX, atomic_open, atomic_write_text


# --- 1. which export belongs to which run -----------------------------------

def _export(root: Path, session: str, name: str = "ascii-output.txt") -> Path:
    p = root / "SBGData" / session / "export" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


def test_each_day_gets_its_own_session(tmp_path):
    d1 = _export(tmp_path, "20260821.124517_0001")
    d2 = _export(tmp_path, "20260822.081000_0001")
    files = disc._session_files(tmp_path, "ascii-output.txt")
    assert disc.pick_session_file(files, "20260821.133437") == d1
    assert disc.pick_session_file(files, "20260822.093000") == d2


def test_later_session_same_day_wins_for_a_later_run(tmp_path):
    am = _export(tmp_path, "20260821.080000_0001")
    pm = _export(tmp_path, "20260821.130000_0002")
    files = disc._session_files(tmp_path, "ascii-output.txt")
    assert disc.pick_session_file(files, "20260821.101500") == am
    assert disc.pick_session_file(files, "20260821.140206") == pm


def test_logger_clock_a_few_seconds_late_still_matches(tmp_path):
    """The session folder is named on the SBG clock; the run on the ACS one."""
    s = _export(tmp_path, "20260821.130002_0001")
    files = disc._session_files(tmp_path, "ascii-output.txt")
    assert disc.pick_session_file(files, "20260821.130000") == s


def test_unstamped_layout_falls_back_to_the_old_behaviour(tmp_path):
    a = _export(tmp_path, "processed_a")
    _export(tmp_path, "processed_b")
    files = disc._session_files(tmp_path, "ascii-output.txt")
    assert disc.pick_session_file(files, "20260821.133437") == a


def test_run_before_every_session_falls_back_to_first(tmp_path):
    first = _export(tmp_path, "20260821.124517_0001")
    _export(tmp_path, "20260822.081000_0001")
    files = disc._session_files(tmp_path, "ascii-output.txt")
    assert disc.pick_session_file(files, "20260820.100000") == first


def test_no_exports_at_all(tmp_path):
    assert disc.pick_session_file(disc._session_files(tmp_path, "ascii-output.txt"),
                                  "20260821.133437") is None


def test_discovery_assigns_per_run(tmp_path):
    """End to end through discover_runs: two runs on two days, two sessions,
    and each run's nav AND events export come from its own day."""
    for stamp in ("20260821.133437", "20260822.093000"):
        cam = tmp_path / "Images" / stamp / "Rear"
        cam.mkdir(parents=True)
        (cam / "000000000750.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    n1 = _export(tmp_path, "20260821.124517_0001")
    e1 = _export(tmp_path, "20260821.124517_0001", "Events-output.txt")
    n2 = _export(tmp_path, "20260822.081000_0001")
    e2 = _export(tmp_path, "20260822.081000_0001", "Events-output.txt")
    runs = {r.run_id: r for r in disc.discover_runs(
        tmp_path, calibrations_dir=tmp_path, registered_only=False)}
    assert runs["20260821.133437"].get(disc.KEY_NAV_EXPORT) == n1
    assert runs["20260821.133437"].get(disc.KEY_EVENTS_EXPORT) == e1
    assert runs["20260822.093000"].get(disc.KEY_NAV_EXPORT) == n2
    assert runs["20260822.093000"].get(disc.KEY_EVENTS_EXPORT) == e2


# --- 2 + 3. exit code and the stderr reason line ----------------------------

def _report(**kw):
    base = dict(aborted=False, abort_reason="", needs_attention=[], written=[],
                outcomes=[], rename_error="")
    base.update(kw)
    return SimpleNamespace(**base)


def _outcome(status):
    return SimpleNamespace(status=status)


def test_clean_batch_exits_0_and_says_nothing_on_stderr(capsys):
    ok = [_outcome("written")] * 4
    assert cli._process_exit(_report(outcomes=ok, written=ok), {}) == cli.EXIT_OK
    assert capsys.readouterr().err == ""


def test_unfinished_rename_is_not_a_clean_exit(capsys):
    """GPS written, rename stopped on a locked file. Used to exit 0, so the
    orchestrator marked the stage PASS with images under temporary names."""
    ok = [_outcome("written")] * 4
    rep = _report(outcomes=ok, written=ok,
                  rename_error="[WinError 32] file in use: 000000001500.jpg")
    assert cli._process_exit(rep, {}) == cli.EXIT_NEEDS_ATTENTION
    err = capsys.readouterr().err.strip().splitlines()
    assert err and "rename did not finish" in err[-1]


def test_needs_attention_ends_with_a_one_line_reason(capsys):
    outs = [_outcome("written"), _outcome("written"), _outcome("partial"),
            _outcome("skipped")]
    rep = _report(outcomes=outs, written=outs[:2], needs_attention=outs[2:])
    assert cli._process_exit(rep, {}) == cli.EXIT_NEEDS_ATTENTION
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert "2 of 4 run(s) written" in last
    assert "1 partial" in last and "1 skipped" in last


def test_a_record_that_could_not_be_written_is_not_clean(capsys):
    ok = [_outcome("written")]
    written = {"report": "x", "error_mapping": "could not write run_mapping.csv: locked"}
    assert cli._process_exit(_report(outcomes=ok, written=ok), written) == \
        cli.EXIT_NEEDS_ATTENTION
    assert "run_mapping.csv" in capsys.readouterr().err


def test_aborted_exits_2_with_a_reason(capsys):
    rep = _report(aborted=True, abort_reason="no lever-arm file")
    assert cli._process_exit(rep, {}) == cli.EXIT_CANNOT_RUN
    assert "no lever-arm file" in capsys.readouterr().err


# --- 4. atomic writes --------------------------------------------------------

def test_atomic_write_replaces_the_file(tmp_path):
    p = tmp_path / "run_mapping.csv"
    p.write_text("old", encoding="utf-8")
    atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(f"*{WRITING_SUFFIX}"))


def test_a_failed_write_leaves_the_old_file_whole(tmp_path):
    """The point of it: a batch killed mid-write must not leave half a
    manifest or half a gate file that looks complete."""
    p = tmp_path / "rename_manifest.csv"
    p.write_text("original_name,new_name\n000000000750.jpg,000000000026.jpg\n",
                 encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_open(p) as fh:
            fh.write("original_name,new_name\n")
            raise RuntimeError("killed half way")
    assert p.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob(f"*{WRITING_SUFFIX}"))


def test_the_temp_name_is_nothing_the_rename_recovery_would_touch():
    from core.rename import TEMP_SUFFIX
    assert WRITING_SUFFIX != TEMP_SUFFIX
    assert not WRITING_SUFFIX.endswith(".tmp")
