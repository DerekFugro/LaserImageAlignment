"""LaserImageAlignment on the command line — the production path, no GUI.

    python cli.py runs    <collection>       what will and will not run
    python cli.py check   <collection>       preflight; writes nothing
    python cli.py process <collection>       the batch: writes GPS, renames,
                                             and writes the reports

`core/` has never imported Qt, so all of this is the same code the viewer
drives — not a second implementation that can drift away from it.

EXIT CODES, because the point of a CLI is that something else can branch on
the answer:

    0   every run was written
    1   it ran, but something needs a person: a run was skipped, flagged, or
        came out partial
    2   it could not run at all: bad path, no runs, or an unhandled error

1 and 2 are deliberately different. "Nine runs written, one skipped because
its export is missing" is a normal end to a collection day and should not look
the same as "that folder is not a collection".

A MISSING INPUT DOES NOT STOP THE OTHER RUNS. The viewer offers a Locate…
button; here the run is refused, named, and the rest carry on — and
`--locate KEY=PATH` supplies the file for every run that is missing it, which
is the same override store the GUI writes, so a path located here is
remembered there and the other way round.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from core import config
from core import discovery as disc
from core.batch import (bracket_stamp, preflight, process_collection,
                        write_reports, write_run_mapping_csv)

EXIT_OK = 0
EXIT_NEEDS_ATTENTION = 1
EXIT_CANNOT_RUN = 2


class UsageError(Exception):
    """The caller asked for something impossible - a bad --locate key, a path
    that is not there. Exits EXIT_CANNOT_RUN, not 1: nothing was attempted, so
    it must not look like "ran, and something needs a person"."""


def _overrides(args) -> disc.OverrideStore:
    """The SAME sidecar the GUI uses, so a path located in one is known to the
    other. --locate adds to it before anything is discovered."""
    store = disc.OverrideStore(Path(args.overrides) if args.overrides else None)
    return store


def _apply_locates(store, root: Path, locates: list[str], cal_dir) -> list[str]:
    """`--locate nav_export=D:\\...\\ascii-output.txt`, applied to every run that
    is missing that input. Returns what it did, for the log.

    Every run, not one: the reason an export is missing is almost always that
    the whole collection's Qinertia output lives somewhere else, and asking for
    it twelve times would be a worse tool than the button it replaces.
    """
    notes = []
    for item in locates:
        key, _, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        if not raw:
            raise UsageError(f"--locate needs KEY=PATH, got {item!r}")
        if key not in disc.REQUIRED_KEYS:
            raise UsageError(f"--locate: unknown input {key!r}. "
                             f"One of: {', '.join(disc.REQUIRED_KEYS)}")
        path = Path(raw)
        exists = path.is_dir() if key == disc.KEY_IMAGES else path.is_file()
        if not exists:
            raise UsageError(f"--locate {key}: no such file or folder: {path}")
        n = 0
        for run in disc.discover_runs(root, calibrations_dir=cal_dir,
                                      registered_only=False):
            if run.get(key) is None:
                store.set(run.root, run.run_id, key, path)
                n += 1
        notes.append(f"located {key} for {n} run(s): {path}")
    return notes


def _progress(quiet: bool):
    """One line per stage, not one per image.

    process_collection calls this about 40,000 times for a collection - once
    per image and once per profile. Printed raw it is a wall of text that
    hides the run boundaries, which are the only part a person watching wants.
    """
    if quiet:
        return None
    state = {"key": None, "t": 0.0}

    def report(stage, run_id, i, n):
        key = (stage, run_id)
        now = time.monotonic()
        if key != state["key"]:
            state["key"], state["t"] = key, now
            print(f"  {run_id}  {stage} ({n})", flush=True)
        elif now - state["t"] >= 5.0:          # a long stage is still alive
            state["t"] = now
            print(f"  {run_id}  {stage} {i}/{n}", flush=True)

    return report


def cmd_runs(args) -> int:
    """What this collection holds, and what the app will do with each run."""
    root = Path(args.collection)
    cal = Path(args.calibrations) if args.calibrations else None
    on_disk = {r.run_id for r in disc.discover_runs(root, calibrations_dir=cal,
                                                    registered_only=False)}
    will_run = [r.run_id for r in disc.discover_runs(root, calibrations_dir=cal)]
    excluded = disc.excluded_stamps(root)
    registered = disc.daily_stamps(root)

    if not on_disk and not registered:
        print(f"no runs found under {root}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    known = sorted(on_disk | (registered or set()) | excluded)
    print(f"{'run':>18}  {'on disk':>7}  verdict")
    for rid in known:
        if rid in will_run:
            verdict = "PROCESS"
        elif rid in excluded:
            verdict = "excluded - Status X in the ACS Daily file"
        elif registered is not None and rid not in registered:
            verdict = "ignored - no row in the ACS Daily file"
        else:
            verdict = "no data on disk for it"
        print(f"{bracket_stamp(rid):>18}  {'yes' if rid in on_disk else 'no':>7}  {verdict}")
    print(f"\n{len(will_run)} run(s) would be processed")
    return EXIT_OK if will_run else EXIT_NEEDS_ATTENTION


def cmd_check(args) -> int:
    """Preflight only. Writes nothing, decides nothing, changes nothing."""
    root = Path(args.collection)
    cal = Path(args.calibrations) if args.calibrations else None
    store = _overrides(args)
    for note in _apply_locates(store, root, args.locate, cal):
        print(note)
    rep = preflight(root, calibrations_dir=cal, overrides=store)
    if not rep.runs:
        print(f"no runs found under {root}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    print(rep.summary_text())
    return EXIT_OK if not rep.not_ready and rep.exif_ok else EXIT_NEEDS_ATTENTION


def cmd_process(args) -> int:
    """The batch. Writes GPS into the images and the Gocator CSVs, renames the
    images to their section distance, and writes the reports."""
    root = Path(args.collection)
    cal = Path(args.calibrations) if args.calibrations else None
    store = _overrides(args)
    for note in _apply_locates(store, root, args.locate, cal):
        print(note)

    report = process_collection(
        root, calibrations_dir=cal, overrides=store,
        write_images=not args.no_images,
        write_gocator=not args.no_gocator,
        write_csv=not args.no_csv,
        progress=_progress(args.quiet),
    )
    if not report.outcomes and not report.excluded and not report.ignored:
        print(f"no runs found under {root}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    print()
    print(report.to_text())

    # The reports are the record of what was written. They are written even for
    # a dry run: "what WOULD have happened" is worth keeping too, and the batch
    # report says plainly which it was.
    for kind, path in write_reports(report).items():
        print(f"{kind}: {path}")

    if args.no_images and args.no_gocator and args.no_csv:
        print("\nDRY RUN - nothing was written into the collection "
              "(--no-images --no-gocator --no-csv)")

    if report.aborted:
        return EXIT_CANNOT_RUN
    return EXIT_OK if not report.needs_attention else EXIT_NEEDS_ATTENTION


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lia", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, with_writes=False):
        sp.add_argument("collection", help="the collection-day folder")
        sp.add_argument("--calibrations", metavar="DIR",
                        help="AllCalibrations folder (holds the lever-arm file). "
                             "Outranks $LIA_CALIBRATIONS and lia.ini. "
                             f"Currently: {config.calibrations_dir()} "
                             f"(from {config.calibrations_source()})")
        sp.add_argument("--overrides", metavar="FILE",
                        help="where located paths are remembered. Default: the "
                             "same sidecar the viewer uses")
        sp.add_argument("--locate", metavar="KEY=PATH", action="append", default=[],
                        help="supply a missing input for every run that lacks it, "
                             "e.g. --locate nav_export=D:\\proc\\ascii-output.txt. "
                             "Repeatable.")
        sp.add_argument("-q", "--quiet", action="store_true",
                        help="no per-run progress")
        if with_writes:
            sp.add_argument("--no-images", action="store_true",
                            help="do not write GPS EXIF into the JPEGs")
            sp.add_argument("--no-gocator", action="store_true",
                            help="do not write GPS columns into the Gocator CSVs")
            sp.add_argument("--no-csv", action="store_true",
                            help="do not write the per-run alignment table")

    common(sub.add_parser("runs", help="what will and will not run"))
    common(sub.add_parser("check", help="preflight; writes nothing"))
    common(sub.add_parser("process", help="the batch"), with_writes=True)
    return p


def main(argv=None) -> int:
    # The reports carry em dashes and arrows. A Windows console is cp1252 by
    # default, which turns them into mojibake - and the first thing anybody
    # reads from a CLI is its output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):      # already wrapped, or a pipe
            pass
    args = build_parser().parse_args(argv)
    root = Path(args.collection)
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    fn = {"runs": cmd_runs, "check": cmd_check, "process": cmd_process}[args.command]
    try:
        return fn(args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN
    except Exception as exc:                       # never a bare traceback at a shell
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
