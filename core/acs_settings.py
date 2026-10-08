"""The DMI calibration the van actually collected with, from ACS's own file.

ACS writes <root>/<YYYYMMDD>/<van>_Settings_<YYYYMMDD>.xml every day, and its
<DMICalibration> block holds the DMIScaleFactor (encoder pulses per metre) and
the date it took effect. That is the calibration in force for that day's
collection, so it is read from there - never from a number in this code.

Until 2026-10-08 the app had the OLD cart's Gocator scale built in
(0.235116 mm per tick). The new van (ARANSW1) runs 1445.56 pulses/m, so the
encoder message overstated distances by ~36% ("78 m into a 272 m run" for a
200 m run). Placement never used it - everything is placed by PTP time on the
Qinertia trajectory - but the QC wording was wrong for every vehicle but one.

The Gocator counts every edge of the same encoder's pulses (x4 quadrature).
That held on both vehicles: ARAN104 1063.17 pulses/m with the Gocator set to
0.235116 mm/tick, ARANSW1 1445.56 with 0.172943 mm/tick, both exactly 1/4.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

GOCATOR_QUADRATURE = 4


@dataclass(frozen=True)
class AcsDmi:
    pulses_per_m: float      # ACS DMIScaleFactor
    effective: str           # ACS EffectiveDate, verbatim
    path: str

    @property
    def gocator_counts_per_m(self) -> float:
        return self.pulses_per_m * GOCATOR_QUADRATURE

    @property
    def gocator_mm_per_tick(self) -> float:
        return 1000.0 / self.gocator_counts_per_m

    def describe(self) -> str:
        when = f" (effective {self.effective})" if self.effective else ""
        return (f"ACS DMI {self.pulses_per_m:g} pulses/m{when} from "
                f"{Path(self.path).name} -> Gocator "
                f"{self.gocator_mm_per_tick:.6f} mm/tick")


def find_settings_file(root: Path, day: str) -> Path | None:
    """<root>/<day>/<van>_Settings_<day>.xml for any van, or None."""
    folder = Path(root) / day
    if not day or not folder.is_dir():
        return None
    hits = sorted(folder.glob(f"*_Settings_{day}.xml"))
    return hits[0] if hits else None


def read_dmi(path: Path) -> tuple[AcsDmi | None, str]:
    """(the DMI calibration, "") or (None, why not)."""
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        return None, f"{Path(path).name} could not be read ({exc})"
    block = tree.getroot().find(".//DMICalibration")
    if block is None:
        return None, f"{Path(path).name} has no DMICalibration block"
    raw = (block.findtext("DMIScaleFactor") or "").strip()
    try:
        scale = float(raw)
    except ValueError:
        return None, f"{Path(path).name}: DMIScaleFactor is {raw!r}, not a number"
    if scale <= 0:
        return None, f"{Path(path).name}: DMIScaleFactor is {scale:g}"
    effective = " ".join((block.findtext("EffectiveDate") or "").split())
    return AcsDmi(scale, effective, str(path)), ""


def dmi_for_run(root: Path | str, run_id: str) -> tuple[AcsDmi | None, str]:
    """The DMI calibration for the day a run was collected (its stamp's
    YYYYMMDD), or (None, why not)."""
    day = str(run_id).strip("[]")[:8]
    path = find_settings_file(Path(root), day)
    if path is None:
        return None, f"no {day}/*_Settings_{day}.xml in the collection"
    return read_dmi(path)
