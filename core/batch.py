"""Batch processing: correct positions for every run in a collection root.

For each run: QC -> solve offsets -> compute corrected positions -> write them
into the deliverables (JPEG GPS EXIF, Gocator GPS columns, export CSV) -> record
the outcome. Nothing halts the batch; failures are listed in the report so they
can be checked before the next process in the chain.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import calibration as cal
from . import discovery as disc
from .alignment import PTP_PASS_MAX_DEV_S, offset_by_lever_arm
from .formats import GOCATOR_META_COLS, TAI_UTC_NOMINAL_S
from .geotag import ensure_piexif, geotag_image, inject_gocator_gps
from .pipeline import RunResult, export_csv, process_run
from .qc import TARGET_CSV, TARGET_IMAGES, TARGETS


@dataclass
class MissingInput:
    """A required input a run cannot be processed without."""
    run_id: str
    key: str
    description: str


@dataclass
class RunPreflight:
    """What can and cannot be done for one run, decided BEFORE anything is written."""
    run_id: str
    ready: bool = True
    missing: list[MissingInput] = field(default_factory=list)
    coverage_ok: bool = True
    reason: str = ""                              # why it is not ready
    cameras: dict = field(default_factory=dict)   # camera -> image count
    gocator_files: list = field(default_factory=list)

    @property
    def n_images(self) -> int:
        return sum(self.cameras.values())

    def files_skipped_text(self) -> str:
        parts = [f"{n} {cam} image(s)" for cam, n in sorted(self.cameras.items()) if n]
        parts += [f"{len(self.gocator_files)} Gocator CSV(s)"] if self.gocator_files else []
        return ", ".join(parts) if parts else "no data files found"


@dataclass
class PreflightReport:
    root: str
    runs: list[RunPreflight] = field(default_factory=list)
    exif_ok: bool = True
    exif_note: str = ""

    @property
    def ready(self) -> list[RunPreflight]:
        return [r for r in self.runs if r.ready]

    @property
    def not_ready(self) -> list[RunPreflight]:
        return [r for r in self.runs if not r.ready]

    def summary_text(self) -> str:
        lines = []
        if not self.exif_ok:
            lines.append(f"!! IMAGES CANNOT BE GEOTAGGED: {self.exif_note}")
            lines.append("")
        if self.ready:
            lines.append(f"WILL BE PROCESSED — {len(self.ready)} run(s):")
            for r in self.ready:
                lines.append(f"  + {r.run_id}: {r.files_skipped_text()}")
        if self.not_ready:
            lines.append("")
            lines.append(f"WILL BE SKIPPED — {len(self.not_ready)} run(s), "
                         "these files get NO GPS:")
            for r in self.not_ready:
                lines.append(f"  - {r.run_id}: {r.reason}")
                lines.append(f"      not written: {r.files_skipped_text()}")
        return "\n".join(lines)


def _gocator_ptp_span(path: Path) -> tuple[float, float] | None:
    """First/last ptpTimestamp (seconds) without indexing the whole file."""
    try:
        with open(path, "rb") as fh:
            fh.readline()                      # header
            first = fh.readline()
            fh.seek(0, 2)
            size = fh.tell()
            back = min(size, 65536)
            fh.seek(size - back)
            tail = fh.read().splitlines()
        last = next((l for l in reversed(tail)
                     if l.count(b",") >= GOCATOR_META_COLS), None)
        if not first or last is None:
            return None
        f = int(first.split(b",", 3)[2]) / 1e6
        l = int(last.split(b",", 3)[2]) / 1e6
        return (min(f, l), max(f, l))
    except (OSError, ValueError, IndexError):
        return None


def preflight(root: Path, calibrations_dir: Path | None = None,
              overrides: disc.OverrideStore | None = None) -> PreflightReport:
    """Check EVERY run BEFORE anything is written: are the required inputs
    present, and does the postprocessed export actually COVER the run's times?

    The batch writes in place, so the user must be told exactly which runs will
    be skipped — and which files therefore get no GPS — before it starts.
    """
    from .formats import NavTable, ParseError, UtcTable, parse_event_triggers

    report = PreflightReport(root=str(root))
    report.exif_ok, report.exif_note = ensure_piexif()
    nav_cache: dict = {}
    for run in disc.discover_runs(root, calibrations_dir=calibrations_dir,
                                  overrides=overrides):
        pf = RunPreflight(run_id=run.run_id)
        for cam, cam_dir in run.cameras.items():
            pf.cameras[cam] = len(list(Path(cam_dir).glob("*.jpg")))
        for key in (disc.KEY_GOCATOR_L, disc.KEY_GOCATOR_R):
            if run.get(key):
                pf.gocator_files.append(run.get(key))

        for key in run.missing():
            if key not in disc.OPTIONAL_KEYS:
                pf.missing.append(MissingInput(run.run_id, key,
                                               disc.KEY_DESCRIPTIONS.get(key, key)))
        if pf.missing:
            pf.ready = False
            pf.reason = "missing " + ", ".join(m.key for m in pf.missing)
            report.runs.append(pf)
            continue

        # coverage: does the export span this run's trigger and profile times?
        try:
            utc = UtcTable.parse(run.get(disc.KEY_UTC_TIME))
            navp = run.get(disc.KEY_NAV_EXPORT)
            if navp not in nav_cache:
                nav_cache[navp] = NavTable.parse(navp, anchor_utc_date=utc.utc_date)
            nav = nav_cache[navp]
            trig = utc.sbg_to_utc(parse_event_triggers(run.get(disc.KEY_EVENT_A)))
            need_lo, need_hi = float(trig.min()), float(trig.max())
            for gp in pf.gocator_files:
                span = _gocator_ptp_span(Path(gp))
                if span:
                    # PTP -> UTC, nominally TAI_UTC_NOMINAL_S with the solver's
                    # own tolerance either side. Spelled from the constants:
                    # written out as 37.5 / 36.5 this preflight would keep the
                    # old offset through a leap second and start skipping runs
                    # the batch could process.
                    need_lo = min(need_lo, span[0] - TAI_UTC_NOMINAL_S - PTP_PASS_MAX_DEV_S)
                    need_hi = max(need_hi, span[1] - TAI_UTC_NOMINAL_S + PTP_PASS_MAX_DEV_S)
            lo, hi = float(nav.epoch_s[0]), float(nav.epoch_s[-1])
            if need_lo < lo or need_hi > hi:
                pf.ready = False
                pf.coverage_ok = False
                short_start = max(0.0, lo - need_lo)
                short_end = max(0.0, need_hi - hi)
                pf.reason = (
                    "postprocessed export does NOT cover this run's times "
                    f"(short by {short_start:.0f} s at the start, {short_end:.0f} s at the end)"
                )
        except (ParseError, ValueError, TypeError) as exc:
            pf.ready = False
            pf.coverage_ok = False
            pf.reason = f"could not verify coverage: {exc}"
        report.runs.append(pf)
    return report


@dataclass
class RunOutcome:
    run_id: str
    ok: bool
    status: str                       # "written" | "flagged" | "error"
    images_tagged: int = 0
    images_skipped: int = 0
    profiles_tagged: int = 0
    cameras: dict = field(default_factory=dict)   # camera name -> images tagged
    # what was actually THERE, counted before anything was attempted. A run that
    # fails QC writes nothing, so every "tagged" number is zero — and a report
    # that only prints those reads as "this run has no images", which is a very
    # different (and alarming) statement than "this run's images were left
    # alone". Keep the found counts so the difference stays visible.
    cameras_found: dict = field(default_factory=dict)   # camera name -> images present
    gocator_found: int = 0
    # deliverable -> why it was NOT written, while others in the same run were.
    # This is the whole point of scoping failures: the run is no longer all-or
    # -nothing, so something has to carry "what is still outstanding".
    held: dict = field(default_factory=dict)
    csv_path: str = ""
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    result: RunResult | None = None
    # camera -> RenameOutcome, filled by the rename step at the end of the
    # batch. The alignment CSV is rewritten from these so the names in it are
    # the names on disk.
    renames: dict = field(default_factory=dict)
    # the resolved input paths for this run, kept so the run mapping can be
    # written for SKIPPED runs too — those are exactly the ones somebody
    # downstream needs to know about
    run: disc.RunPaths | None = None

    @property
    def images_found(self) -> int:
        return sum(self.cameras_found.values())

    def found_text(self) -> str:
        parts = [f"{n} {cam} image(s)" for cam, n in sorted(self.cameras_found.items()) if n]
        if self.gocator_found:
            parts.append(f"{self.gocator_found} Gocator CSV(s)")
        return ", ".join(parts) if parts else "no data files found"


def _collapse(msgs: list[str], limit: int = 3) -> list[str]:
    """One root cause repeated across hundreds of files becomes one line."""
    groups: dict[str, list[str]] = {}
    for m in msgs:
        reason = m.rsplit(": ", 1)[-1] if ": " in m else m
        groups.setdefault(reason, []).append(m)
    out: list[str] = []
    for reason, items in groups.items():
        if len(items) > limit:
            out.append(f"{len(items)} file(s) failed — {reason}")
            out.append(f"    e.g. {items[0]}")
        else:
            out.extend(items)
    return out


@dataclass
class BatchReport:
    root: str
    started_utc: str
    outcomes: list[RunOutcome] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    # Stamps found on disk that the ACS Daily file does not register. Not
    # failures and not skipped runs — they were never sections, so they are
    # nobody's to-do item and get no issue row. One line in the report is
    # still worth it: data exists in those folders, and silence about it
    # would leave someone wondering where it went.
    ignored: list[str] = field(default_factory=list)
    # Stamps the ACS Daily file marks Status X: false starts, abandoned
    # sections, alignment rolls. Derek's rule (2026-09-01) is that these are
    # not processed. Kept apart from `ignored` because the two are different
    # facts - "no row in the Daily file" versus "a row that says do not use
    # this" - and a folder full of data deserves the right explanation.
    excluded: list[str] = field(default_factory=list)
    # Set when the rename step could not finish (a locked file). The GPS is
    # still written and the reports are still produced; the names are simply
    # the ones ACS gave. Recorded rather than raised - see process_collection.
    rename_error: str = ""
    # Images put back after a batch was killed mid-rename. Reported loudly:
    # they were invisible until now, and their run may have been refused.
    recovered: list[str] = field(default_factory=list)
    # What this batch was allowed to write. A dry run (all three off) produces
    # a report that otherwise reads exactly like a successful one, down to
    # "every deliverable was written" - which is the opposite of the truth, and
    # the batch report is the record somebody reads back a week later.
    write_images: bool = True
    write_gocator: bool = True
    write_csv: bool = True

    @property
    def dry_run(self) -> bool:
        return not (self.write_images or self.write_gocator or self.write_csv)

    @property
    def written(self) -> list[RunOutcome]:
        return [o for o in self.outcomes if o.status == "written"]

    @property
    def needs_attention(self) -> list[RunOutcome]:
        return [o for o in self.outcomes if o.status != "written"]

    @property
    def partial(self) -> list[RunOutcome]:
        return [o for o in self.outcomes if o.status == "partial"]

    def issue_rows(self) -> list[dict]:
        """One row per fail, error and warning, across every run.

        Includes SKIPPED runs, which never reach process_run and so have no QC
        report of their own — their preflight reason and each missing input is
        recorded here, because "we could not process it" is exactly the kind of
        thing that has to be worked down and fixed.
        """
        rows: list[dict] = []
        for o in self.outcomes:
            sev = "ERROR" if o.status == "error" else "FAIL"
            for msg in o.failures:
                cid, detail = _split_check(msg)
                rows.append({"run_id": o.run_id, "run_status": o.status,
                             "severity": sev, "check_id": cid, "detail": detail})
            for msg in o.warnings:
                cid, detail = _split_check(msg)
                rows.append({"run_id": o.run_id, "run_status": o.status,
                             "severity": "WARN", "check_id": cid, "detail": detail})
            for target, reason in sorted(o.held.items()):
                rows.append({"run_id": o.run_id, "run_status": o.status,
                             "severity": "HELD", "check_id": target,
                             "detail": reason})
        return rows

    @property
    def n_issues(self) -> dict:
        counts: dict = {}
        for r in self.issue_rows():
            counts[r["severity"]] = counts.get(r["severity"], 0) + 1
        return counts

    def affected_rows(self) -> list[dict]:
        """One row per deliverable that did NOT reach the client.

        Once a failure stops only part of a run, "which runs failed" is no
        longer the useful question — a run can be 90% delivered. What has to be
        chased is the specific sensor or camera still outstanding, which is what
        this lists.
        """
        rows: list[dict] = []
        for o in self.outcomes:
            for target in sorted(o.held):
                rows.append({
                    "run_id": o.run_id,
                    "target": target,
                    "run_status": o.status,
                    "reason": o.held[target],
                    "files_affected": (o.found_text() if target == TARGET_IMAGES
                                       else _target_files(o, target)),
                })
        return rows

    def affected_text(self) -> str:
        rows = self.affected_rows()
        if self.dry_run:
            return ("DRY RUN — nothing was written into the collection. This "
                    "report says what WOULD have happened.\n")
        if not rows:
            return "AFFECTED COLLECTION: none — every deliverable was written.\n"
        lines = ["AFFECTED COLLECTION — these deliverables were NOT written:", ""]
        for r in rows:
            lines.append(f"  {r['run_id']}  [{r['target']}]  ({r['run_status']})")
            lines.append(f"      files:  {r['files_affected']}")
            lines.append(f"      reason: {r['reason']}")
        lines.append("")
        lines.append(f"{len(rows)} deliverable(s) across "
                     f"{len({r['run_id'] for r in rows})} run(s).")
        return "\n".join(lines) + "\n"

    def lever_arm_sources(self) -> list[str]:
        """Every lever-arm file this batch read, so the report says where the
        arms came from. A batch that ran on defaults once said nothing at all
        about it; now there is no default, and the file is named here."""
        out = []
        for o in self.outcomes:
            v = o.run.get(disc.KEY_LEVER_ARMS) if o.run is not None else None
            if v and str(v) not in out:
                out.append(str(v))
        return out

    def to_text(self) -> str:
        arms = self.lever_arm_sources()
        lines = [
            "LaserImageAlignment — batch report"
            + ("  *** DRY RUN ***" if self.dry_run else ""),
            f"root:    {self.root}",
            f"started: {self.started_utc}",
            "arms:    " + ("; ".join(arms) if arms else
                           "NO LEVER-ARM FILE - nothing was written"),
            f"runs:    {len(self.outcomes)}  "
            f"({len(self.written)} written, {len(self.partial)} partial, "
            f"{len(self.needs_attention) - len(self.partial)} need attention)",
            "",
        ]
        # A number in the lever-arm file that the parser did not understand is
        # a number the app is NOT using while the file says otherwise. It has
        # to be visible here: on 2026-09-04 the file said the lens height was
        # 1.719 and every run quietly used the built-in 1.667 instead.
        for src in arms:
            for w in cal.lever_arm_warnings(src):
                lines[-1:] = [f"WARNING: {Path(src).name}: {w}", ""]
        if self.recovered and self.write_images:
            lines[-1:] = [
                f"recovered: {len(self.recovered)} image(s) left mid-rename by an "
                "interrupted batch, put back before processing",
                "",
            ]
        elif self.recovered:
            lines[-1:] = [
                f"STRANDED: {len(self.recovered)} image(s) left mid-rename by an "
                "interrupted batch are still under temporary names. A real run "
                "would put them back; this one wrote no images. Until it does, "
                "those runs scan short and the matcher will refuse them.",
                "",
            ]
        if self.ignored:
            lines[-1:] = [
                f"ignored: {len(self.ignored)} folder(s) with no row in the ACS "
                f"Daily file — {', '.join(self.ignored)}",
                "",
            ]
        if self.rename_error:
            lines[-1:] = [
                f"RENAME DID NOT FINISH: {self.rename_error} - GPS was written and "
                "these reports are complete, but some images still carry the name "
                "ACS gave them. Close anything holding the files and re-run.",
                "",
            ]
        if self.excluded:
            lines[-1:] = [
                f"excluded: {len(self.excluded)} run(s) marked Status X in the ACS "
                f"Daily file, not processed — {', '.join(self.excluded)}",
                "",
            ]
        if self.aborted:
            lines.insert(1, f"*** BATCH ABORTED: {self.abort_reason} ***")
        for o in self.outcomes:
            mark = {"written": "OK  ", "flagged": "FLAG", "error": "ERR ",
                    "partial": "PART", "skipped": "SKIP"}.get(o.status, "????")
            cams = ", ".join(f"{k}:{v}" for k, v in sorted(o.cameras.items())) or "-"
            # "N of M images tagged", never a bare "N images" — the bare form
            # was read as the number of images the run HAS (Derek, 2026-08-26,
            # on a flagged run that showed "0 images" while holding 76 of them).
            lines.append(f"[{mark}] {o.run_id}: {o.images_tagged} of {o.images_found} "
                         f"images tagged ({cams}), {o.profiles_tagged} profiles tagged"
                         + (f", {o.images_skipped} images skipped" if o.images_skipped else ""))
            if o.result is not None and o.result.ptp_l is not None:
                p = o.result.ptp_l
                m = o.result.match
                lines.append(f"         PTP offset {p.offset_s:.3f} s (r={p.r:.3f})"
                             + (f" | match k={m.shift_k:+d}, {m.n_matched}/"
                                f"{len(o.result.parsed.images or [])} images" if m else ""))
            for f in _collapse(o.failures):
                lines.append(f"         FAIL: {f}")
            for w in o.warnings:
                lines.append(f"         warn: {w}")
            for n in o.notes:
                lines.append(f"         note: {n}")
            if o.csv_path:
                lines.append(f"         csv: {o.csv_path}")
            lines.append("")
        skipped = [o for o in self.outcomes if o.status == "skipped"]
        if skipped:
            lines.append("SKIPPED — these runs got NO GPS (files left untouched):")
            for o in skipped:
                lines.append(f"  - {o.run_id}: {'; '.join(o.failures)}")
                for n in o.notes:
                    lines.append(f"      {n}")
            lines.append("")
        if self.needs_attention:
            lines.append("Runs needing attention before the next process:")
            for o in self.needs_attention:
                lines.append(f"  - {o.run_id}: "
                             + ("; ".join(_collapse(o.failures)) or o.status))
            lines.append("")
        lines.append(self.affected_text())
        return "\n".join(lines)


def _target_files(o: RunOutcome, target: str) -> str:
    if target.startswith("gocator_"):
        return f"{target[-1]} profile CSV"
    if target == TARGET_CSV:
        return "alignment export CSV"
    return o.found_text()


AFFECTED_COLUMNS = ["run_id", "target", "run_status", "reason", "files_affected"]

# Everything the app produces about a batch lands here, in the collection
# root. The one thing that does NOT is the rename manifest: that belongs
# beside the images it describes, because it is how those files are read back
# and undone.
PROCESSED_DIR = "Processed"
ISSUE_COLUMNS = ["run_id", "run_status", "severity", "check_id", "detail"]

# One row per ACS run, for OTHER processes.
#
# The problem it solves: the SBG logger and the collection system open their
# per-run folders independently, so the same physical run can be named a
# second or two apart — 20260824.100258 in Images/ and GoCatorData/, but
# "20260824.100259 DataLogger" in SBGData/. Every tool that opens a collection
# has to work that out, and a tool that pairs them differently silently reads
# ANOTHER run's triggers. This app already decides it (discovery.pair_sbg_stamps:
# 3 s tolerance, exact matches claim their logger first, no logger used twice,
# ambiguity left unmatched). Writing the decision down means nobody downstream
# has to re-derive it, and everyone agrees.
#
# Skipped runs get a row too — those are precisely the ones someone has to
# chase, and a mapping that omitted them would read as "this run never existed".
#
# It answers two questions, in this order:
#   "which sections from the Daily report can I actually run?"  -> status
#   "the folder is named a second or two wrong, where IS it?"   -> sbg_logger_dir
#
# Rows come from the Daily file UNION what is on disk, so a section ACS says
# it collected but whose folders never arrived gets a row saying `not_found`
# instead of silently not existing. `note` says in words what is odd about
# the row, so the file can be skimmed without decoding the numeric columns.
#
# run_id and sbg_stamp are written BRACKETED - [20260824.100157] - which is
# exactly how the ACS Daily file writes the same stamp. Bare, they are valid
# decimal numbers: Excel parses 20260824.100157 as a float and General format
# shows "20260824.1", so all twelve rows of a day look identical and the file
# is useless in the tool people actually open it with. The brackets make the
# field text, and they make the mapping and the Daily report agree on how a
# run is spelled. Consumers strip them - see strip_stamp().
MAPPING_COLUMNS = [
    "run_id", "status", "note",
    "sbg_stamp", "sbg_skew_s", "sbg_exact", "sbg_logger_dir",
    "section", "direction", "from_km", "to_km",
    "images_dir", "gocator_l", "gocator_r",
    "event_a", "utc_time", "dmi", "nav_export", "events_export",
    "cameras", "images_found", "images_tagged", "profiles_tagged",
]


def processed_dir(root) -> Path:
    """<root>/Processed, created on demand."""
    d = Path(root) / PROCESSED_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _split_check(msg: str) -> tuple[str, str]:
    """Messages are written 'check.id: detail'; keep them apart in the CSV so
    a whole class of problem can be filtered in one go."""
    head, sep, tail = msg.partition(": ")
    looks_like_id = sep and " " not in head and "." in head
    return (head, tail) if looks_like_id else ("", msg)


def write_affected_csv(report: BatchReport, out_path: Path) -> Path:
    """The machine-readable half of the same record, so a collection can be
    filtered and tracked rather than read by eye."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=AFFECTED_COLUMNS)
        w.writeheader()
        for row in report.affected_rows():
            w.writerow(row)
    return out_path


def write_issues_csv(report: "BatchReport", out_path: Path) -> Path:
    """EVERY fail, error and warning in the batch, one row each.

    The text report collapses repeated failures so it stays readable; this
    does not collapse anything. It is the list to work down when fixing a
    collection, so a problem that appears 200 times appears 200 times.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ISSUE_COLUMNS)
        w.writeheader()
        for row in report.issue_rows():
            w.writerow(row)
    return out_path


def bracket_stamp(stamp: str) -> str:
    """[20260824.100157] — how the ACS Daily file writes a run stamp, and how
    this app writes it into run_mapping.csv. See MAPPING_COLUMNS for why."""
    return f"[{stamp}]" if stamp else ""


def strip_stamp(value: str) -> str:
    """The inverse, for anything reading run_mapping.csv back. Tolerates an
    unbracketed value so a hand-edited file still works."""
    return str(value).strip().strip("[]")


def write_run_mapping_csv(report: "BatchReport", out_path: Path) -> Path:
    """Which SBG DataLogger folder belongs to which ACS run (see
    MAPPING_COLUMNS), plus the inputs each run resolved to."""
    from .daily import find_daily_file, parse_daily

    daily_path = find_daily_file(Path(report.root))
    daily = parse_daily(daily_path) if daily_path else {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def p(run, key):
        v = run.get(key) if run is not None else None
        return str(v) if v else ""

    def logger_dir(run) -> str:
        """The DataLogger FOLDER, which is the thing somebody has to go and
        find when its name is a second or two off. The individual file paths
        are already columns, but a person hunting a misnamed folder wants the
        folder."""
        for key in (disc.KEY_EVENT_A, disc.KEY_UTC_TIME, disc.KEY_DMI):
            v = run.get(key) if run is not None else None
            if v:
                return str(Path(v).parent)
        return ""

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MAPPING_COLUMNS)
        w.writeheader()
        for o in report.outcomes:
            run, e = o.run, daily.get(o.run_id)
            # sbg_stamp falls back to run_id: an exact-match run has nothing
            # to record, but a consumer should still find a folder name here
            # rather than a blank it has to interpret
            stamp = (getattr(run, "sbg_stamp", "") or o.run_id) if run else o.run_id
            skew = int(getattr(run, "sbg_skew_s", 0) or 0) if run else 0
            notes = []
            if skew:
                notes.append(f"logger folder is {skew:+d} s from the run stamp: "
                             f"'{stamp} DataLogger'")
            if o.status == "skipped":
                notes.append("cannot run - " + "; ".join(o.failures[:1]))
            elif o.status in ("flagged", "error"):
                notes.append("ran but something failed - see issues_*.csv")
            if e is None:
                notes.append("no row in the ACS Daily file")
            w.writerow({
                "run_id": bracket_stamp(o.run_id),
                "sbg_stamp": bracket_stamp(stamp), "sbg_skew_s": skew,
                "sbg_exact": skew == 0, "status": o.status,
                "sbg_logger_dir": logger_dir(run),
                "note": "; ".join(notes),
                "section": e.section if e else "",
                "direction": e.direction if e else "",
                "from_km": e.from_km if e else "",
                "to_km": e.to_km if e else "",
                "images_dir": p(run, disc.KEY_IMAGES),
                "gocator_l": p(run, disc.KEY_GOCATOR_L),
                "gocator_r": p(run, disc.KEY_GOCATOR_R),
                "event_a": p(run, disc.KEY_EVENT_A),
                "utc_time": p(run, disc.KEY_UTC_TIME),
                "dmi": p(run, disc.KEY_DMI),
                "nav_export": p(run, disc.KEY_NAV_EXPORT),
                "events_export": p(run, disc.KEY_EVENTS_EXPORT),
                "cameras": " ".join(sorted(o.cameras_found)),
                "images_found": o.images_found,
                "images_tagged": o.images_tagged,
                "profiles_tagged": o.profiles_tagged,
            })
        # Sections ACS says it collected that have NOTHING on disk. Without
        # this they simply would not appear: every other row starts from a
        # folder that exists, so a run whose data never arrived would vanish
        # from the very file somebody opens to ask what they can run.
        seen = {o.run_id for o in report.outcomes}
        for run_id in sorted(set(daily) - seen):
            e = daily[run_id]
            # An X run is not missing data - it is data we were told to leave
            # alone. Saying "no folders on disk" about a folder that is right
            # there would send somebody looking for a problem that is not one.
            if e.excluded:
                status = "not_processed"
                note = (f"Status '{e.status}' in the ACS Daily file - "
                        "marked not to be processed")
            else:
                status = "not_found"
                note = "in the ACS Daily file but no folders on disk for it"
            w.writerow({
                "run_id": bracket_stamp(run_id), "status": status,
                "note": note,
                "sbg_stamp": "", "sbg_skew_s": "", "sbg_exact": "",
                "sbg_logger_dir": "",
                "section": e.section, "direction": e.direction,
                "from_km": e.from_km, "to_km": e.to_km,
                "images_dir": "", "gocator_l": "", "gocator_r": "",
                "event_a": "", "utc_time": "", "dmi": "",
                "nav_export": "", "events_export": "",
                "cameras": "", "images_found": 0,
                "images_tagged": 0, "profiles_tagged": 0,
            })
    return out_path


def write_reports(report: "BatchReport") -> dict:
    """Write every record of this batch into <root>/Processed.

    Returns {kind: path}. Nothing here raises: a batch that produced results
    must not lose them because one file could not be written, so each failure
    is reported back as an 'error:' entry instead.
    """
    stamp = report.started_utc.replace(":", "").replace("-", "")
    out: dict[str, str] = {}
    try:
        d = processed_dir(report.root)
    except OSError as exc:
        return {"error": f"could not create {PROCESSED_DIR}/: {exc}"}
    jobs = [("report", f"batch_report_{stamp}.txt",
             lambda p: p.write_text(report.to_text(), encoding="utf-8")),
            ("issues", f"issues_{stamp}.csv",
             lambda p: write_issues_csv(report, p)),
            # NOT stamped: other processes read this one, and a name that
            # changes every batch is a name nothing can be pointed at. It is
            # a statement of the collection's current shape, so the latest
            # batch's answer is the only one that is true.
            ("mapping", "run_mapping.csv",
             lambda p: write_run_mapping_csv(report, p))]
    if report.affected_rows():
        jobs.append(("affected", f"affected_{stamp}.csv",
                     lambda p: write_affected_csv(report, p)))
    for kind, name, write in jobs:
        try:
            write(d / name)
            out[kind] = str(d / name)
        except OSError as exc:
            out[f"error_{kind}"] = f"could not write {name}: {exc}"
    return out


def process_collection(root: Path, calibrations_dir: Path | None = None,
                       overrides: disc.OverrideStore | None = None,
                       write_images: bool = True, write_gocator: bool = True,
                       write_csv: bool = True,
                       progress=None) -> BatchReport:
    """Process every run under `root`. `progress(stage, run_id, i, n)` is
    called for UI updates; it must be cheap and thread-safe.

    Runs without required inputs or without export coverage are SKIPPED and
    listed (see preflight()); complete runs are still processed."""
    root = Path(root)
    report = BatchReport(
        root=str(root),
        started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        write_images=write_images, write_gocator=write_gocator,
        write_csv=write_csv,
    )
    # BEFORE anything reads the folders: put back any image a killed batch
    # left under a temporary name. A temp file does not match *.jpg, so the
    # scan would see fewer images than the run has triggers and the matcher
    # would refuse the whole run — for ever, since nothing else restores them.
    #
    # Not into a Status X run, and not on a dry run. This walk happens before
    # any filtering, so it reached into both until 2026-09-02.
    report.recovered = _recover_interrupted_renames(
        root, skip=disc.excluded_stamps(root), apply=write_images)
    rep_pf = preflight(root, calibrations_dir=calibrations_dir, overrides=overrides)
    skip_ids = {r.run_id for r in rep_pf.not_ready}
    runs = disc.discover_runs(root, calibrations_dir=calibrations_dir, overrides=overrides)
    # what was on disk but not in the Daily file, so the report can say so once
    registered = {r.run_id for r in runs}
    # A run marked Status X in the Daily file has a row; it is just not ours
    # to process. Subtract it from `ignored` or it would be reported as having
    # no row at all, which is a different and untrue statement.
    on_disk = {r.run_id for r in disc.discover_runs(
        root, calibrations_dir=calibrations_dir, registered_only=False)}
    excluded = disc.excluded_stamps(root)
    report.excluded = sorted(on_disk & excluded)
    report.ignored = sorted(on_disk - registered - excluded)
    by_id = {r.run_id: r for r in rep_pf.runs}
    for i, run in enumerate(runs):
        if progress:
            progress("run", run.run_id, i, len(runs))
        if run.run_id in skip_ids:
            pf = by_id[run.run_id]
            report.outcomes.append(RunOutcome(
                run_id=run.run_id, ok=False, status="skipped", run=run,
                # every missing input by name, not just the summary line —
                # these rows are the to-do list for fixing the collection
                failures=[pf.reason] + [f"presence.{m.key}: missing {m.description}"
                                        for m in pf.missing]
                + ([] if pf.coverage_ok else
                   ["coverage.nav_export: the export does not span this run"]),
                cameras_found=dict(pf.cameras),
                gocator_found=len(pf.gocator_files),
                notes=[f"NOT WRITTEN (no GPS): {pf.files_skipped_text()}",
                       "nothing in this run was modified"],
            ))
            continue
        report.outcomes.append(
            _process_one(run, write_images, write_gocator, write_csv, progress,
                         exif_note="" if rep_pf.exif_ok else rep_pf.exif_note)
        )
    # FINAL step, after GPS is in the files: rename every image to its
    # corrected distance into the section (core.rename). Always runs — the
    # filename is a deliverable, and a name that means the odometer when the
    # GPS is right is a name that lies. It is last because it needs the
    # alignments the loop above just built, and reversible from the
    # rename_manifest.csv it writes beside the images.
    # Guarded, unlike everything above it. A locked JPEG - an open Explorer
    # preview, an antivirus scan, the viewer itself - raises OSError out of
    # os.rename, and this runs AFTER the GPS is already in the files. Left
    # unguarded it took process_collection down with it, so write_reports()
    # never ran and a batch that had just written positions into hundreds of
    # images left no report, no issues CSV and no run mapping. The rename is
    # a deliverable; the record of what was written is a bigger one.
    #
    # ONLY WHEN THE IMAGES ARE BEING WRITTEN. The rename IS an image
    # deliverable, so write_images=False has to stop it too. It did not: a
    # caller asking for --no-images --no-gocator --no-csv got every filename
    # changed and the pre-section frames moved into BeforeCollection anyway.
    # A dry run that renames is worse than no dry run at all, because the
    # person who asked for it believes nothing moved.
    if write_images:
        try:
            _rename_to_section_distance(root, report)
        except OSError as exc:
            report.rename_error = str(exc)
    else:
        for o in report.outcomes:
            o.notes.append("RENAME skipped: images were not written this run")
    # The CSV written during the loop names files by what they were called
    # THEN. The rename above has since moved them, so rewrite it with both
    # names. Doing it here rather than deferring the first write keeps an
    # interrupted batch from leaving no table at all. Nothing to refresh when
    # neither the table nor the rename happened.
    if write_csv and write_images:
        _refresh_alignment_csvs(report)
    return report


def _recover_interrupted_renames(root: Path, skip: set[str] | None = None,
                                 apply: bool = True) -> list[str]:
    """Walk every camera folder and restore images stranded mid-rename.

    Returns "<run>/<camera>/<file>" for each one put back. Problems are
    returned in the same list, prefixed, rather than raised: a batch must
    not refuse to start because one folder is odd.

    `skip` is the set of run stamps this batch must not touch - the ones the
    ACS Daily file marks Status X. Derek's rule is that they are not
    processed, and "not processed" means not touched: no rename, not even a
    repair. This walk runs before any filtering, so without `skip` it reached
    straight into them (found 2026-09-02).

    `apply=False` FINDS the stranded files and reports them without moving
    anything. That is what a dry run needs: it still has to say "there are 5
    images here a real run would put back", because that is exactly the
    condition that would otherwise make the run look short and get it
    refused - but a dry run that renames files is not a dry run.
    """
    from .formats import BEFORE_DIR
    from .rename import TEMP_SUFFIX, recover_interrupted

    skip = skip or set()
    out: list[str] = []
    images = Path(root) / "Images"
    if not images.is_dir():
        return out
    for cam_dir in sorted(p for p in images.glob("*/*") if p.is_dir()):
        if cam_dir.name == BEFORE_DIR or cam_dir.parent.name in skip:
            continue
        where = f"{cam_dir.parent.name}/{cam_dir.name}"
        if not apply:
            out += [f"{where}/{p.name[: -len(TEMP_SUFFIX)]} (not put back: "
                    "images were not written this run)"
                    for p in sorted(cam_dir.glob(f"*{TEMP_SUFFIX}"))]
            continue
        got, problems = recover_interrupted(cam_dir)
        out += [f"{where}/{n}" for n in got]
        out += [f"{where}: {p}" for p in problems]
    return out


def _refresh_alignment_csvs(report: BatchReport) -> None:
    """Rewrite each run's alignment CSV now that the images have their final
    names, so every `image_file_final` in it resolves on disk."""
    for o in report.outcomes:
        if o.result is None or not o.csv_path or not o.renames:
            continue
        try:
            export_csv(o.result, Path(o.csv_path), rename_info=o.renames)
        except Exception as exc:
            # the table from before the rename is still there and still
            # correct about positions; say plainly which half is stale
            o.warnings.append(
                f"csv refresh: {exc} — {Path(o.csv_path).name} still names the "
                "images as they were before the rename")


def _rename_to_section_distance(root: Path, report: BatchReport) -> None:
    """Give every placed image a name that says where it is.

    Runs on every batch, after the GPS is in the files. A run with no row in
    the ACS Daily file has no section start to measure from, so it keeps the
    names it has and says so in its notes.
    """
    from .daily import find_daily_file, parse_daily
    from .rename import (move_before_collection, rename_camera_folder,
                         section_distance_mm)

    daily_path = find_daily_file(root)
    daily = parse_daily(daily_path) if daily_path else {}
    if not daily:
        where = "no Daily_ARAN104 file in this collection"
        for o in report.outcomes:
            if o.status in ("written", "partial"):
                o.notes.append(f"RENAME skipped: {where} — names left as they are")
        return
    for o in report.outcomes:
        res = o.result
        if res is None or o.status not in ("written", "partial"):
            continue
        entry = daily.get(o.run_id)
        if entry is None:
            o.notes.append("RENAME skipped: no Daily-file row for this run — "
                           "names left as they are")
            continue
        if TARGET_IMAGES in o.held:
            # no GPS went into these images, so no name goes on them either:
            # the name is a position, and the position was not written
            o.notes.append("RENAME skipped: images were held — names left as they are")
            continue
        nav = res.parsed.nav
        arms = res.parsed.lever_arms or {}
        for cam_name, al in sorted(res.camera_alignments.items()):
            # The arm from the RUN'S lever-arm file - the same numbers the
            # EXIF was just written from (parsed once, in parse_all). Until
            # 2026-09-02 this re-read the arm with no calibrations_dir and
            # fell back to the built-in constant, so an edit to the file
            # moved every stamped position but not one filename.
            arm_xyz = arms.get(cam_name)
            mm = section_distance_mm(
                al, nav, entry, None if arm_xyz is None else -float(arm_xyz[0]))
            r = rename_camera_folder(Path(al.images.dir), al.images.files,
                                     mm, entry, o.run_id, cam_name)
            o.renames[cam_name] = r
            o.notes.append(f"RENAME {cam_name}: {r.renamed} named by distance "
                           f"into section {entry.section}, {r.skipped} kept; "
                           f"manifest {Path(r.manifest_path).name}")
            for e in r.errors:
                o.warnings.append(f"rename {cam_name}: {e}")
            # Immediately after: set aside anything whose footprint is before
            # the section start. Those are real photos of ground outside the
            # section, and leaving them in the camera folder invites someone
            # to stitch or measure them by mistake.
            n_before, probs = move_before_collection(Path(al.images.dir))
            if n_before:
                o.notes.append(f"BEFORE {cam_name}: {n_before} image(s) start "
                               f"before the section — moved to BeforeCollection/")
            for e in probs:
                o.warnings.append(f"before-collection {cam_name}: {e}")


def _process_one(run: disc.RunPaths, write_images: bool, write_gocator: bool,
                 write_csv: bool, progress=None, exif_note: str = "") -> RunOutcome:
    """`exif_note` is non-empty when the EXIF writer is unavailable
    (preflight's ensure_piexif). The images are then HELD, once, with that
    reason - not attempted one by one, which produced one ImportError line
    per photograph and marked every run 'flagged' while its Gocator CSVs and
    table were in fact written."""
    # counted from disk BEFORE anything is attempted, so the report can still
    # say what this run holds even when processing fails outright
    out = RunOutcome(
        run_id=run.run_id, ok=True, status="written", run=run,
        cameras_found={name: len(list(Path(d).glob("*.jpg")))
                       for name, d in run.cameras.items()},
        gocator_found=sum(1 for k in (disc.KEY_GOCATOR_L, disc.KEY_GOCATOR_R)
                          if run.get(k)),
    )
    try:
        result = process_run(run)
    except Exception as exc:  # unexpected — never kill the batch
        out.ok, out.status = False, "error"
        out.failures.append(f"unhandled error: {exc}")
        out.notes.append(f"NOT WRITTEN (no GPS): {out.found_text()}")
        return out
    out.result = result
    out.failures = [f"{c.check_id}: {c.message}" for c in result.report.failed]
    out.warnings = [f"{c.check_id}: {c.message}" for c in result.report.warned]

    # A failure now stops only what it actually invalidates. A run where one
    # laser's clock failed still gets its images, its export CSV and its good
    # sensor; only the bad sensor waits.
    blocked = result.report.blocked_targets()

    def why(target: str) -> str:
        return "; ".join(c.message for c in result.report.blocked_reasons(target)) \
            or "not written"

    if set(TARGETS) <= blocked:      # nothing survives — the old behaviour
        out.ok = False
        out.status = "flagged"
        for target in TARGETS:
            out.held[target] = why(target)
        if any(c.check_id == "presence.nav_export" for c in result.report.failed):
            out.notes.append("POSTPROCESSED EXPORT MISSING — provide ascii-output.txt "
                             "for this run (Locate… in the viewer), then re-run the batch")
        out.notes.append(f"NOT WRITTEN (no GPS): {out.found_text()}")
        out.notes.append("no positions written (QC failed) — nothing was modified")
        return out

    al = result.alignment
    if al is None:
        out.ok = False
        out.status = "flagged"
        out.notes.append(f"NOT WRITTEN (no GPS): {out.found_text()}")
        out.notes.append("alignment did not run — nothing written")
        return out

    nav = result.parsed.nav

    # 1) JPEG GPS EXIF, in place — EVERY camera found (Rear, ROW, ...)
    if write_images and TARGET_IMAGES in blocked:
        out.held[TARGET_IMAGES] = why(TARGET_IMAGES)
    elif write_images and exif_note:
        out.held[TARGET_IMAGES] = f"images cannot be geotagged: {exif_note}"
    elif write_images:
        alignments = result.camera_alignments or ({"": al} if al else {})
        for cam_name, cam_al in sorted(alignments.items()):
            # heading_at() interpolates across the 0/360 wrap (np.interp on raw
            # yaw degrees points the wrong way there)
            heading = nav.heading_at(cam_al.utc_s) if nav is not None else None
            speed = nav.speed_at(cam_al.utc_s) if nav is not None else None
            tagged = skipped = 0
            for j in range(len(cam_al.images)):
                if not (np.isfinite(cam_al.lat_deg[j]) and np.isfinite(cam_al.lon_deg[j])):
                    skipped += 1
                    continue
                try:
                    geotag_image(
                        cam_al.images.path(j), float(cam_al.lat_deg[j]),
                        float(cam_al.lon_deg[j]), float(cam_al.alt_m[j]),
                        float(cam_al.utc_s[j]),
                        heading_deg=float(heading[j]) if heading is not None else None,
                        speed_ms=float(speed[j]) if speed is not None else None,
                    )
                    tagged += 1
                except Exception as exc:
                    out.ok = False
                    out.status = "flagged"
                    out.failures.append(
                        f"geotag {cam_name}/{cam_al.images.files[j]}: {exc}")
                if progress and tagged % 50 == 0:
                    progress("images", f"{run.run_id} {cam_name}", tagged,
                             len(cam_al.images))
            out.images_tagged += tagged
            out.images_skipped += skipped
            out.cameras[cam_name] = tagged
            if skipped:
                out.notes.append(
                    f"{cam_name}: {skipped} image(s) had no position "
                    "(unmatched trigger or outside the export window) — left untagged")

    # 2) Gocator per-profile GPS columns, in place
    if write_gocator and nav is not None:
        for side, goc, ptp in (("L", result.parsed.gocator_l, result.ptp_l),
                               ("R", result.parsed.gocator_r, result.ptp_r)):
            if goc is None:
                continue
            target = f"gocator_{side}"
            if target in blocked or ptp is None or not ptp.passed:
                # named, not silently skipped — this is the sensor the operator
                # has to go and look at
                out.held[target] = why(target) if target in blocked else \
                    f"Gocator {side} PTP offset did not solve"
                continue
            utc = goc.ptp_us.astype(np.float64) / 1e6 - ptp.offset_s
            loc = nav.locate(utc)
            # Out to where the sensor actually is. The lasers straddle the
            # cart 0.715 m apart; without this both were stamped at the IMU,
            # so the two wheelpaths landed on top of each other. The arm is
            # the one parsed for this run; a side the file does not list was
            # held by content.gocator_*_arm above, so this is never None here.
            arm = (result.parsed.lever_arms or {}).get(side)
            if arm is None:
                out.held[target] = f"Gocator {side} has no lever arm in the file"
                continue
            lat, lon, alt = offset_by_lever_arm(
                loc["lat_deg"], loc["lon_deg"], loc["alt_m"],
                nav.heading_at(utc), *arm)
            try:
                n = inject_gocator_gps(
                    Path(goc.path), lat, lon, alt,
                    progress=(lambda w, t, s=side: progress("gocator", f"{run.run_id} {s}", w, t))
                    if progress else None,
                )
                out.profiles_tagged += n
            except Exception as exc:
                out.ok = False
                out.status = "flagged"
                out.failures.append(f"gocator {side} GPS: {exc}")

    # 3) the image->position table
    if write_csv and TARGET_CSV in blocked:
        out.held[TARGET_CSV] = why(TARGET_CSV)
    elif write_csv:
        try:
            dest = Path(run.root) / "Exports" / f"{run.run_id}_alignment.csv"
            out.csv_path = str(export_csv(result, dest))
        except Exception as exc:
            out.ok = False
            out.status = "flagged"
            out.failures.append(f"csv export: {exc}")
            out.held[TARGET_CSV] = str(exc)

    # "partial" is its own status: some of this run reached the client and some
    # did not, and calling that either "written" or "flagged" would be a lie.
    if out.held and out.status == "written":
        out.ok = False
        out.status = "partial"
        out.notes.append("PARTIAL — written: "
                         + (", ".join(sorted(set(TARGETS) - set(out.held))) or "nothing")
                         + " | held: " + ", ".join(sorted(out.held)))
    return out
