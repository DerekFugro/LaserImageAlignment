# LaserImageAlignment

Aligns Pave (rear, nadir) camera images with Gocator laser profiles on a shared
PTP-time / distance axis, and exports a per-image table of PTP time and lat/lon
from post-processed Qinertia navigation data.

## Launch

Double-click `launch.bat` (macOS/Linux: `./launch.sh`). Later runs start
immediately.

The first run builds the environment one of two ways:

- **[uv](https://docs.astral.sh/uv/) if it is on PATH** — preferred. uv fetches
  its own Python 3.12, so the machine needs no Python installed at all. This
  removes the biggest setup trap: the bundled `wheels/` are cp312, so on a box
  with 3.11 or 3.13 the offline install fails with an error that looks nothing
  like "wrong Python". Install it with
  `winget install --id=astral-sh.uv` or `pip install uv`.
- **`py -3.12` + a venv + pip** otherwise, exactly as before. uv is never a
  requirement.

Either way dependencies come from the bundled `wheels/` folder when it is
present (no internet needed) and from PyPI when it is not.

`.venv` is built for **one machine** — it stores absolute paths. Copy this
folder to another PC and that venv still *exists* but cannot run, which used to
produce an error naming a drive letter that means nothing there. `launch.bat`
now tests the venv before using it and rebuilds it if it is foreign, so this
fixes itself.

## Where it looks for AllCalibrations

The calibrations folder holds `LaserImageAlignmentLeverArms.md`, which is a
**required** input — without it every run is refused with `missing lever_arms`.
It used to be a constant compiled into `discovery.py`, so on any machine that is
not the original the app simply did not work and the GUI had no way to say so.

`lia.ini`, beside `app.py`, is the setting. Open it, change the path, save:

```ini
[paths]
calibrations = F:\Sidewalk\002_App\SidewalkProfilier\100_AllCalibrations
collections  = F:\Sidewalk\099_CollectedData
```

Order, most explicit first: `--calibrations` on the command line → the
`LIA_CALIBRATIONS` / `LIA_COLLECTIONS` environment variables → `lia.ini` → the
built-in default. Nothing in that chain can raise: a missing, blank, or
malformed INI falls through to the next source rather than stopping the app.
`cli.py check --help` prints the path currently in force and where it came from.

`collections` is only where the viewer's **Open** dialog starts; blank is fine.

### What that folder has to contain

Only two things are ever read out of it. Everything else in a real
`AllCalibrations` — raw intrinsic images, backups, other tools' files — is
ignored, and does not need to be copied to a new machine.

| file | required? | used for |
|---|---|---|
| `LaserImageAlignmentLeverArms.md` | **yes** | the arms of both cameras and both lasers, plus the lens height. In every written position. |
| newest `PAVE_*.yaml`, at any depth, `*Refined*` ignored | no | viewer only — undistort and the profile-window width. Missing just warns. |

## The lever-arm file

The one file the app cannot run without, and the one a person is expected to
edit. It is plain markdown: a heading, then the numbers under it.

**This README does not state the values, deliberately — open the file.** They
have changed repeatedly, a number copied out of a document has caused real
mistakes, and the file is the only thing that decides. The shape, with the
values shown as placeholders so nothing here can be copied by accident:

```markdown
# LaserImageAlignment lever arms

Metres from the SBG cover target (the IMU origin).
X forward, Y right, Z down.

## Rear camera

X  <metres, negative = behind the IMU>
Y  <metres, positive = to the right>
Z  <metres, positive = down>

## ROW camera

X  <...>
Y  <...>
Z  <...>

## Gocator left

X  <...>
Y  <...>
Z  <...>

## Gocator right

X  <...>
Y  <...>
Z  <...>

## Rear

<metres above the pavement>
```

How headings are read:

- **ends in `camera`** → a camera, keyed by its first word. `## Rear camera`
  becomes `Rear`, which must match the image folder name under `Images/<run>/`.
- **starts with `Gocator`** → a laser; the words `left` / `right` pick the side.
- **exactly `Rear`, or containing `lens height`** → the lens height. It is a
  bare number on its own line, not an X/Y/Z triple, because it is a height
  above the **ground**, not an offset from the IMU. Display scale only —
  millimetres-per-pixel and the profile window — it moves no written position.
- **anything else** → ignored.

Rules worth knowing before editing it:

- A missing axis line is **0.0**, which is a real claim: it says that sensor
  sits exactly on the IMU in that direction.
- A unit or a remark after the number is fine — `X <value> m` and
  `X <value> (measured 2026-08-31)` both parse. (They did not until
  2026-09-02; a stray `m` silently turned the arm into 0.0 and stamped the
  camera at the IMU.)
- A camera or laser with **no section** holds its own deliverable back rather
  than being guessed at. A file that parses to nothing at all refuses the run.
- **A number the parser does not understand is reported, not discarded.** Put a
  value under a heading it does not recognise and the batch report says so on a
  `WARNING:` line. This exists because on 2026-09-04 the heading had been
  trimmed to `## Rear` when the parser still demanded `## Rear lens height`:
  the lens height in the file was discarded and every run quietly used the
  built-in fallback instead, with nothing said anywhere.
- The file is re-read **every run, never cached** — edit it and the next batch
  uses the new numbers. Which also means editing it between two batches makes
  those batches disagree.

Keep prose out of it. One warning line is fine; a paragraph of reasoning is
not, because the number is what someone needs to find in a hurry.

## Command line (no GUI)

The same `core/` the viewer drives — not a second implementation that can
drift away from it.

```
uv run cli.py runs    <collection>   what will and will not run
uv run cli.py check   <collection>   preflight; writes nothing
uv run cli.py process <collection>   the batch: GPS, rename, reports
```

Without uv, substitute `.venv\Scripts\python.exe` for `uv run`.

**The exit code is the contract**, because the point of a command line is that
a scheduler or a batch file can branch on the answer without reading English:

| code | meaning |
|---|---|
| 0 | every run was written |
| 1 | it ran, but something needs a person — a run was skipped, flagged, or came out partial |
| 2 | it could not run at all — bad path, no runs, a bad flag, or an unhandled error |

1 and 2 are deliberately different. "Nine runs written, one skipped because its
export is missing" is a normal end to a collection day and must not look the
same as "that folder is not a collection".

Flags:

- `--locate KEY=PATH` — the **Locate…** button, for a shell. Supplies a missing
  input for *every* run that lacks it, because the reason an export is missing
  is almost always that the whole day's Qinertia output lives somewhere else.
  It writes the same override sidecar the viewer reads, so a path located here
  is known there, and the other way round. Repeatable. A bad key, or a path
  that is not there, exits 2 before anything is attempted.
- `--no-images` / `--no-gocator` / `--no-csv` — all three together is a dry run:
  nothing is written into the collection, no rename, no `BeforeCollection/`.
  The batch report is still written, and says plainly which it was.
- `--calibrations DIR`, `--overrides FILE`, `-q` (no per-run progress).

A missing input does not stop the other runs: that run is refused, named, and
the rest carry on.

## Workflow

1. **Open Run Root…** — pick a collection-day folder (e.g.
   `F:\Sidewalk\099_CollectedData\20260816_Routed_IMages0.75_gocatordata_23.98`).
   Runs are discovered from `Images/`, `GoCatorData/`, `SBGData/` by their
   shared `YYYYMMDD.HHMMSS` stamp, then filtered against the ACS **Daily
   file** (`<YYYYMMDD>/Daily_ARAN104_*.csv`): a stamp with no row there was
   never a section, and a row whose `Status` is X was a false start. Both are
   left alone and listed in `Processed/run_mapping.csv` with the reason. With
   no Daily file present, nothing is filtered.
2. **Select a run.** QC + alignment run in the background (a few seconds).
3. **Read the QC panel.** Every check shows PASS / WARN / FAIL:
   - A missing input shows a **Locate…** button — point it at the file (the
     Qinertia `ascii-output.txt` often lives in a different processed folder).
     Your choice is remembered for that run.
   - FAIL blocks export (the viewer still works). WARN is recorded in the
     export's `notes` column.
4. **Browse.** ◀/▶ keys or the slider step through rear images; the Left/Right
   Gocator plots show the profiles covering the same patch of ground
   (the laser scanned it one Rear-arm of travel before the camera passed over
   it — the arm comes from `LaserImageAlignmentLeverArms.md`, read fresh every
   run).
5. **Undistort.** Applies the Pave intrinsic calibration to the displayed
   image. On by default — the ground/pixel model is only exact on the
   undistorted picture — and it drops back to raw, saying so in the badge, for
   a run with no calibration.
6. **Check the alignment.** The green and red lines mark where the laser data
   starts and ends on the picture; the orange line is the scan currently
   highlighted on the plot. Step the scan cursor (`Ctrl/Shift + ←/→`) and the
   orange line and the orange curve move together, so you can see whether the
   laser and the photograph agree about where a feature is. This is what the
   viewer is for.
7. **Export CSV** — one row per image per camera: `image_file` (the name the
   run was processed under), `image_file_final` (what it is called on disk
   now), `set_aside`, camera, trigger index, UTC, PTP (µs),
   `dist_along_track_m`, `section`, `section_distance_mm`, lat/lon/alt,
   residual, offsets, notes. The authoritative list is `EXPORT_COLUMNS` in
   `core/pipeline.py`.

## What this app does NOT do (2026-09-01)

Measurement and the calibration processes were taken out: it is a production
processing app. Gone: the two-point Measure tool, Verify Board (ChArUco),
Ground Truth clicks, and the whole calibration-bar tool (`core/bar.py`, its
panel, `bar_measurements.csv`, `ground_truth_clicks.csv`). About 2,200 lines.

The viewer stays, because seeing the picture next to the laser profiles is
how a run gets checked. Undistort stays with it.

The numbers those tools produced now live in
`AllCalibrations/LaserImageAlignmentLeverArms.md`, which the app READS — edit a
lever arm there and the written positions move. Nothing in the app measures
them any more; they are measured elsewhere and written into that file.

That file is a REQUIRED input (2026-09-02). A run whose calibrations folder
has no readable lever-arm file is listed as "missing lever_arms" by the
pre-check and skipped, exactly like a run with no eventOutA.txt. A camera or
laser that is on disk but has no section in the file holds its own
deliverable back (QC FAIL, scoped). There is no built-in fallback any more:
on 2026-09-02 a batch ran while the file was moved aside and stamped every
position with old constants, and its report said nothing. The batch report
now names the file the arms came from on its `arms:` line.

## What the status numbers mean

- **PTP offset** — solved per run by correlating Gocator encoder rate against
  DMI speed (sweep 30–45 s). Must land within 0.5 s of 37 s (TAI−UTC) with
  r ≥ 0.95. Ground truth on sample runs: 37.00 s, r ≈ 0.98–0.99.
- **match k** — image/trigger count difference, tail-anchored. Triggers that
  fired before ACS started logging are recovered from Qinertia's
  `Events-output.txt` (see `_recover_pre_collection_triggers`), so those images
  ARE placed; without that file they stay unmatched and QC says so.
- **counter scale a** — realtime-INS trigger cadence vs postprocessed-INS
  distance (NOT a wheel measurement). 0.9932–1.0000 across 20260824; the limit
  is `COUNTER_SCALE_MAX_DEV` = 1.0%.
- **Unmatched images** stay in the export flagged `matched = False`, no location.

## Batch: writing corrected positions into the data (the product)

ACS assigns positions its own way and gets them wrong; this app recomputes them
from PTP time + the post-processed export and **writes them into the
deliverables** so downstream processes and the client get correct data.

**Process All (write GPS)** in the toolbar runs every run under the open root:

- **JPEGs — every camera** (Rear/Pave, ROW, and anything else found under
  `Images/<run>/`; cameras are discovered, not hard-coded). GPS EXIF written *in place* (lat/lon/alt/time, plus heading and
  speed). Metadata-only insert: pixel data is byte-identical, and any EXIF
  already on the image is preserved rather than replaced.

  In practice there is none to preserve. Checked 2026-09-04 against an
  untouched collection: **ACS writes no EXIF at all** — zero tags in every IFD,
  no CameraID, no DateTime, no Make. So after the batch a JPEG carries a GPS
  block and nothing else. A downstream tool wanting a capture time must take it
  from the alignment CSV or from `GPSDateStamp`/`GPSTimeStamp`, never from
  `DateTime`. (`tools/Record-CaptureTimes.ps1` exists for the same reason: the
  filesystem timestamps were the only record of capture time, and writing GPS
  destroys those.)
- **Gocator CSVs** — `latitude,longitude,elevation` columns inserted *in place*
  immediately before `x0`, one position per profile. Streaming rewrite
  (~0.2 s per 18 MB file).
- **Exports/** — the per-run alignment CSV (one row per image with a `camera`
  column covering every camera).
- **Processed/** — everything about the batch itself: `batch_report_<ts>.txt`,
  `issues_<ts>.csv` (every WARN and FAIL, one row each), `affected_<ts>.csv`
  (every deliverable NOT written, with the reason), and `run_mapping.csv`
  (one row per run, not timestamped, so other tools can point at it by name).


The viewer still shows the primary (Rear/Pave) camera only; the batch geotags
all of them. Each camera is matched to the triggers independently, so a camera
with a different image count can't corrupt another.

**Pre-check before anything is written.** The batch first inspects every run and
shows you the plan: which runs are ready, and which **cannot** be done — with the
exact count of images and Gocator files that will get **no GPS**. Two reasons a
run is skipped:

- a required input is missing (usually the postprocessed `ascii-output.txt`) —
  you can **Locate…** it and the pre-check re-runs; or
- the export exists but does **not cover** the run's PTP/trigger time window
  (it reports how many seconds short it is at each end).

You then choose: process the ready runs, locate a missing file, or cancel.
Skipped runs are left completely untouched and are listed again in the final
report under `SKIPPED — these runs got NO GPS`.

**Dependencies self-heal.** Geotagging needs `piexif`. If the `.venv` predates
it (created before the batch feature was added), the app installs it
automatically from the bundled `wheels/` folder at the start of a batch — no
manual pip step. If that cannot be done it says so once, clearly, and still
writes the Gocator GPS and tables.

Repeated failures are collapsed in the report: one root cause affecting 166
files shows as one line plus an example, not 166 identical lines.

Safety: every in-place write goes to a temp file, is fsync'd, then atomically
renamed — an interrupted run can never leave a damaged source file. Re-running
the batch is safe: values are replaced, never duplicated.

A QC failure is **scoped to what it actually invalidates**, not to the whole
run. One laser's clock failing no longer costs the images their GPS — that run
comes out `partial`, with the bad sensor held back and the reason recorded in
`Processed/affected_<ts>.csv`. Only a failure that invalidates everything
leaves a run untouched. A missing post-processed export is called out
explicitly so it can be supplied.

Check the report before the next process in the chain — every anomaly the QC
engine finds is listed there.

## The Pave intrinsic calibration

(For the lever-arm file — the other and more important one — see **The
lever-arm file** above.)

The app auto-selects the newest `PAVE_*.yaml` from the calibrations folder but
**ignores any file with `Refined` in the name** — those come from the separate
bar-run tool and have been observed with a 6x-wrong focal length and no `board`
block. Keep the factory ChArUco calibration as the one the app uses.

The calibration is **optional**: it drives only the viewer (undistort, measure,
board verification, the Gocator start/end lines). Writing GPS needs PTP +
triggers + the export and nothing else, so a missing or malformed calibration
warns and disables those viewer features — it never blocks a production write.

## Position & distance: corrected export ONLY

All locations **and** all along-track distances come from the post-processed
Qinertia `ascii-output.txt` via PTP/UTC interpolation. `DmiStationEx` is NEVER
used for placement — it is uncorrected realtime data, kept only as a
cross-check (`content.realtime_vs_export`). No-fix/unconverged rows in the
export are dropped automatically.

**`DmiStationEx` is not raw wheel counts.** It is the SBG's own *estimated*
travelled distance. Both it and the export are estimates, so a disagreement
between them does not by itself mean a hardware fault — see the hardware
section below.

## How the hardware fits together (confirmed with Derek 2026-08-20)

One physical wheel encoder feeds **both** the SBG and the Gocator — but only one
of them uses it to decide when to fire.

- **Gocator** triggers *itself* on the real encoder, stamps every profile with
  the encoder count, and takes PTP time from the SBG.
- **SBG** uses the encoder to aid its Kalman filter, but fires the cameras from
  its **Virtual Odometer** — sync outputs A and B, `Distance 0.750 m`, falling
  edge (confirmed from the SBG config screen, 2026-08-20). A virtual odometer
  is distance derived from the SBG's own nav solution, **not from the wheel.**
  Those pulses are what `eventOutA.txt` records (SBG clock).
- The raw encoder counts are **never logged by the SBG** — post-processing
  outputs velocity only. The count survives solely on the Gocator rows.
- ACS (ARAN Collection Software) also writes into the collection folder.

**So the cameras and the laser are on two different rulers.** The laser is on the
wheel; the cameras are on the INS's estimate of distance. Measured: ~4223.6
Gocator counts/m and ~3170 counts per camera trigger, with a ~1% run-to-run
spread — and that spread *is* the virtual odometer disagreeing with the real
wheel, which makes it the most direct measure of realtime INS distance error in
the data.

**This does not corrupt placement.** The app never uses the nominal 0.75 m for
anything: an image's position comes from the trigger's recorded *time* through
the postprocessed export, and a profile's from its PTP time through the same
export. Why the camera fired doesn't affect where we say it was. What it does
mean is that image spacing on the *ground* is only as regular as the nav
solution was, and that `align.counter_scale` / `content.trigger_spacing` compare
realtime INS against postprocessed INS — a convergence signal, not an
independent check, and nothing to do with wheel calibration.

**Image filenames AS ACS WRITES THEM are not a distance measurement.** They
are `ordinal × 750 mm` — across all 8 camera folders of 20260817_revrus3 every
run starts at exactly 750 and every step is exactly 750, without exception. So
an ACS filename says how many images came before it, not where the picture was
taken. The only real count in the raw data is the Gocator's `encoder` column.

After this app's batch has run, the names DO mean distance — see "The batch
RENAMES every image" above. Which frame a folder is in is decided by whether
`rename_manifest.csv` is sitting beside the images.

ACS's own DMI scale factor is **1063.17 counts/m** (`20260817/ARAN104_Settings_*.xml`,
`<DMICalibration>`, calibrated over a 60 m course, effective 2026-08-15). The
Gocator reads ~4× that — it appears to X4-decode the same quadrature signal ACS
counts as single pulses.

Note what this makes possible but the app does **not** yet do: image↔profile
alignment could be solved largely in encoder counts, removing the GNSS ruler.
See `Spec` Amendment F for what that would and would not fix.

## The batch RENAMES every image (and the manifest is load-bearing)

The last step of every batch renames each placed image to its distance into
the ACS section, in the same 12-digit-millimetre form ACS uses — but the
number now means millimetres from the SECTION START on the corrected
trajectory, not the camera's own drifting odometer. Images whose footprint
falls before the section start get a leading minus and are moved into
`BeforeCollection/` inside their camera folder.

Old and new names are indistinguishable by eye. So `rename_manifest.csv`,
written beside the images, is not a convenience — it maps each new name back
to the original odometer counter, and matching NEEDS that counter to pair
images with triggers. **Delete the manifest and the folder can no longer be
matched or undone.** Renaming goes through a temporary suffix so no file is
ever written over another, and an interrupted batch is repaired automatically
on the next run.

**Downstream tools must not descend into `BeforeCollection/`.** Those images
are before the section start and their names carry a gap that looks like
missing photographs but is not: a trigger fires while the cart is being set
down, the cart then stands still, and the trajectory keeps integrating GNSS
noise. On 20260824.101313 that produced a 2188 mm step with nothing missing.

## Key clock facts (verified against real data 2026-08-18)

- Gocator `ptpTimestamp` is on the PTP/TAI epoch: UTC = ptp/1e6 − 37 s.
  The offset is re-verified per run — never trusted blindly.
- The Gocator `timestamp` column is a free-running sensor clock. Ignored.
- Image → time comes via `eventOutA.txt` (SBG clock) → `utcTime.txt` → UTC.
- Locations interpolate the 50 Hz `ascii-output.txt`; never extrapolated.

## Tests

```
uv run pytest       # unit + synthetic end-to-end (fast, no data needed)
```

or, without uv, `.venv\Scripts\activate` then `pytest`.

`tests/test_cli.py` asserts on the **exit code first** and the printing
second, because that is what another process reads.

Integration tests against real sample data run automatically when
`F:\Sidewalk\099_CollectedData\20260816_Routed_IMages0.75_gocatordata_23.98`
exists (override with `LIA_SAMPLE_ROOT` / `LIA_CAL_DIR`).

## Layout

- `core/` — parsers (`formats.py`), run discovery + Locate overrides
  (`discovery.py`), QC (`qc.py`), alignment solvers (`alignment.py`),
  the intrinsics and the lever-arm file (`calibration.py`), orchestration +
  CSV export (`pipeline.py`), the whole-collection batch and its reports
  (`batch.py`), the ACS Daily register and `QC_Video.csv` (`daily.py`),
  Qinertia's event export (`events.py`), the EXIF and Gocator-CSV writers
  (`geotag.py`), and the section rename and its manifest (`rename.py`).
  No Qt imports in `core/` — which is what makes `cli.py` possible, and
  `tests/test_cli.py` has a test that fails if that ever changes.
- `cli.py` — the command line above. Thin: it parses flags and turns the batch
  report into an exit code.
- `gui/` — PySide6 UI per `Spec/gui-coding-standards.md` (dark mode, 5-method
  widget pattern).
- `tests/` — pytest; synthetic fixtures in `conftest.py`.
- `Spec/` — the specification this app was built from.
- `lia.ini` — the per-installation paths (above). `core/config.py` reads it.
- `pyproject.toml` / `uv.lock` — the dependency set for uv, pinned to Python
  3.12 because the bundled wheels are cp312. `requirements.txt` still exists
  and still works for the pip path.

Out of scope for now (by design): the ROW camera's oblique ground-plane
projection (it is geotagged by the batch, and shown in the viewer above the
Rear image, but no measurement is made on it), PITCH and ROLL position
correction, laser-on-image overlay, .exe packaging. Lever arms themselves ARE
applied — every camera and both lasers, rotated by heading — see
`core/alignment.offset_by_lever_arm` and
`AllCalibrations/LaserImageAlignmentLeverArms.md`. See
`Spec/LaserImageAlignment_Phase.md` "Deferred".
