"""The ACS day folder: the section register, and ACS's own QC.

ACS writes <root>/<YYYYMMDD>/Daily_ARAN104_<YYYYMMDD>.csv with one row per
run, keyed by the run stamp in [brackets]. It is the authority on which LRS
section each run collected, which direction, the From/To chainage (km), and
the GPS point where the section start sits. That makes it the ANCHOR for
naming images by station: the filename's chainage frame is ACS's own, not one
we invent.

The same folder holds QC_Video.csv - ACS's own record of where the cameras
failed to produce an image. See read_qc_video() below.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

STAMP_RE = re.compile(r"\[(\d{8}\.\d{6})\]")


# Status values in the Daily file that mean DO NOT PROCESS this run.
#
# ACS marks a run 'X' (Derek writes it lower case) when it is not a real
# collection - a false start, a section abandoned and re-driven, an alignment
# roll. 20260821 has two: [20260821.125306] on section 910001 and
# [20260821.125440] on 100130, both about 32 m long against sections of 1331 m
# and 462 m. The good runs that day are all 'C'.
#
# Derek's rule (2026-09-01): any row with an X is not processed. It is his
# call and not a judgement this app makes for itself, which is why this is a
# set of literal statuses and not a heuristic about run length.
EXCLUDED_STATUS = frozenset({"X"})


@dataclass
class DailyEntry:
    """One run's row: the section and where it starts."""

    run_id: str
    section: str          # HEADER column — the LRS section id
    direction: str
    from_km: float
    to_km: float
    lat_beg: float
    lon_beg: float
    status: str = ""      # Status column, verbatim: 'C' collected, 'x' excluded

    @property
    def ascending(self) -> bool:
        """True when chainage increases in the direction of travel."""
        return self.to_km >= self.from_km

    @property
    def excluded(self) -> bool:
        """ACS marked this run not-to-be-processed. Case-insensitive: the
        column holds 'x' in practice and 'X' in the spec."""
        return self.status.strip().upper() in EXCLUDED_STATUS


def find_daily_file(root: Path) -> Path | None:
    """The day folder is named YYYYMMDD; the Daily file sits inside it."""
    root = Path(root)
    hits = sorted(root.glob("*/Daily_ARAN104_*.csv"))
    return hits[0] if hits else None


def parse_daily(path: Path) -> dict[str, DailyEntry]:
    """run_id -> DailyEntry. Rows without a [stamp] or coordinates are skipped
    (the file carries a blank spacer row after the header).

    Rows marked X are KEPT, with .excluded True. Dropping them here would be
    easier and worse: the run_mapping.csv row for such a run would then read
    "in the ACS Daily file but no folders on disk", which is a different and
    untrue statement. Excluding a run is a decision, and the record should say
    the decision was made.
    """
    out: dict[str, DailyEntry] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            m = STAMP_RE.search(row.get("FILENAME") or "")
            if not m:
                continue
            try:
                out[m.group(1)] = DailyEntry(
                    run_id=m.group(1),
                    section=str(row["HEADER"]).strip(),
                    direction=str(row.get("Direction", "")).strip(),
                    from_km=float(row["From"]),
                    to_km=float(row["To"]),
                    lat_beg=float(row["LatBeg"]),
                    lon_beg=float(row["LongBeg"]),
                    status=str(row.get("Status") or "").strip(),
                )
            except (KeyError, ValueError, TypeError):
                continue
    return out


# ---------------------------------------------------------------------------
# QC_Video.csv - ACS's own missing-image report
# ---------------------------------------------------------------------------
#
# ACS counts the triggers it issued and the images it got back, and writes one
# row per contiguous hole per camera view:
#
#   Session Name,Error,View,First File,Last File,Begin Chainage,End Chainage,Image Count
#   [20260819.190301],Image missing,ROW,000000327000.JPG,000000331654.JPG,0.327,0.3316544,7
#   [20260819.190301],Image missing,Rear,000000327000.JPG,000000331654.JPG,0.327,0.3316544,7
#
# This is the authoritative answer to "did we lose images", and it is three
# lines to read instead of 4,334 JPEGs to open. We do not use it to PLACE
# anything - it is a witness, not a ruler.
#
# WHY IT IS ONLY A CROSS-CHECK, not the check itself:
#   - It sees the cameras and nothing else. The 4.65 m hole it reports on
#     20260819.190301 has no laser profiles either, and QC_Video.csv is silent
#     about that.
#   - It cannot see anything downstream of our own rename.
#   - It is written by the same system whose trigger we are auditing, so a
#     fault that stopped ACS counting would also stop it reporting.
# Two independent witnesses agreeing is evidence. One witness is not.
#
# Chainage is in KILOMETRES, measured from the start of the run, the same frame
# as the ORIGINAL image filenames (000000327000.JPG = 327.000 m = 0.327 km).
# It is NOT section chainage, and it is NOT the renamed frame.

QC_VIDEO_NAME = "QC_Video.csv"
ERROR_IMAGE_MISSING = "image missing"


@dataclass
class VideoGap:
    """One contiguous stretch where ACS expected images and got none."""

    run_id: str
    view: str             # camera view as ACS names it: "ROW", "Rear"
    error: str            # verbatim, e.g. "Image missing"
    first_file: str
    last_file: str
    begin_m: float        # metres into the run (converted from the file's km)
    end_m: float
    count: int            # images ACS says are missing

    @property
    def length_m(self) -> float:
        return self.end_m - self.begin_m


def find_qc_video_file(root: Path) -> Path | None:
    """<root>/<YYYYMMDD>/QC_Video.csv, or None."""
    hits = sorted(Path(root).glob(f"*/{QC_VIDEO_NAME}"))
    return hits[0] if hits else None


def read_qc_video(path: Path) -> list[VideoGap]:
    """Every row of QC_Video.csv, oldest first.

    A header-only file is a real and common answer - it means ACS found
    nothing wrong - so it returns [], the same as a file with no rows. Callers
    that need to tell "no problems" from "no file" must check the path first
    with find_qc_video_file(); that distinction matters, because reporting
    'ACS found no missing images' when ACS never ran would be a lie.

    Malformed rows are skipped rather than raised on: this file never blocks a
    run, and one unreadable row must not cost us the others.
    """
    out: list[VideoGap] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                m = STAMP_RE.search(row.get("Session Name") or "")
                if not m:
                    continue
                try:
                    out.append(VideoGap(
                        run_id=m.group(1),
                        view=str(row.get("View", "")).strip(),
                        error=str(row.get("Error", "")).strip(),
                        first_file=str(row.get("First File", "")).strip(),
                        last_file=str(row.get("Last File", "")).strip(),
                        begin_m=float(row["Begin Chainage"]) * 1000.0,
                        end_m=float(row["End Chainage"]) * 1000.0,
                        count=int(float(row["Image Count"])),
                    ))
                except (KeyError, ValueError, TypeError):
                    continue
    except OSError:
        return []
    return out


def video_gaps_for_run(gaps: list[VideoGap], run_id: str) -> list[VideoGap]:
    """Just the missing-image rows for one run, in distance order."""
    return sorted(
        (g for g in gaps
         if g.run_id == run_id and g.error.lower() == ERROR_IMAGE_MISSING),
        key=lambda g: g.begin_m,
    )
