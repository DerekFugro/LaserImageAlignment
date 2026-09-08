---
# Instructions for LaserImageAlignment

> This spec is self-contained. The coding agent may have no prior context — every
> rule here is explicit. Default to the strongest model tier (currently Claude
> Opus 5) unless a task is explicitly routed lower.
>
> ⚠️ **THIS SPEC HAS BEEN AMENDED (v0.3, last on 2026-09-02).** v0.1 shipped,
> then the app gained its production feature — it WRITES corrected positions
> into the collected data (Amendment A) — and then, before hand-off, everything
> that was not production processing was taken back out (Amendment J). Many
> statements below are out of date. **Read the Amendments at the end of this
> file before treating any statement here as current** — the amendment wins
> wherever the two disagree, and the newest amendment wins over an older one.
> Superseded lines are struck through and marked inline, and
> "Known-stale statements elsewhere in this file" lists them all in one table.
>
> If this file and the code disagree, **the code is right** and this file is
> missing an amendment.

## ⭐ Project name:
LaserImageAlignment

## ⭐ Purpose:
A Windows desktop app that aligns Pave (rear, nadir) camera images with Gocator
laser profiles on a common PTP-time / distance axis, and exports a table linking
each image to its PTP time and lat/lon location.

## ⭐ Problem it solves:
Derek's sidewalk-survey cart collects rear images (triggered every 0.75 m),
Gocator laser profiles (encoder-triggered, PTP-timestamped), and SBG
inertial/GNSS data — but nothing ties them together. The camera trigger sequence
starts at an arbitrary point (the first recorded image is often from a
pre-collection trigger), so image-to-trigger pairing has an unknown offset. This
app solves that offset automatically, places images and laser data on one
distance axis, and produces per-image geolocations from post-processed
navigation data.

## ⭐ MVP definition:
1. Open a "run root" folder (e.g. `20260818_RoutedRuns_075Images`), auto-discover
   the runs inside `Images/`, `GoCatorData/`, and `SBGData/`.
2. Parse all required formats (see **Data formats** below): Gocator CSV,
   eventOutA.txt, utcTime.txt, DmiStationEx CSV, Qinertia ascii-output.txt,
   image filename distance counters.
3. Run a **data quality check (QC) pass** on every discovered run (see **Data
   quality checks** below): presence, format, and content checks with a
   per-check pass/warn/fail report. If a required input is not found where
   expected, **prompt the user to browse to its location** (never fail
   silently); remember the chosen path per run.
4. Automatically solve (a) the PTP→UTC offset with per-run verification and
   (b) the image↔trigger offset by distance matching. Show both with a
   confidence readout.
5. Viewer: rear image + Gocator Left/Right profile plots for the ground distance
   covered by that image, with next/prev navigation and a distance slider.
6. Camera calibration: load the Pave intrinsic YAML, undistort images (toggle),
   show mm/px scale, ~~click-two-points measure tool, automatic ChArUco board
   verification (measured vs 76.2 mm cell size)~~. **Undistort and mm/px stay.
   Measure and board verification were REMOVED — see Amendment J.**
7. Export a CSV: one row per rear image with trigger index, UTC time, PTP time,
   latitude, longitude, altitude, DMI distance, and match confidence.

## ⭐ Success criteria:
- On sample run `20260818.142721`: PTP offset solver reports 37.0 ± 0.1 s with
  correlation r ≥ 0.95 (verified ground truth: peak at exactly 37.0 s, r = 0.993).
- The 83 rear images vs 82 triggers off-by-one is resolved automatically; the
  unmatched image is flagged, not silently paired.
- Every matched image gets a lat/lon whose UTC lookup falls inside the
  ascii-output.txt time window (no extrapolation).
- ~~ChArUco verification on a board image reports cell size within 2% of
  76.2 mm.~~ **SUPERSEDED — Amendment J.** The app verifies no board.
- The viewer stays responsive with 130 MB Gocator CSVs (no full-file load into
  the GUI thread; indexed/lazy access).

## ⭐ Explicit non-goals / out of scope:
- ~~ROW camera (oblique 55.5°) — everything: display, calibration, projection.
  Design the data model so it can be added later, but write no ROW code.~~
  **SUPERSEDED — Amendments D and J.** ROW is geotagged with its own lever arm
  and is DISPLAYED in the viewer above the Rear image. Only *measuring* on it
  (the oblique ground-plane projection) is still out.
- Realtime `nav.txt` / `gnss1Pos.txt` positions — location comes ONLY from the
  post-processed Qinertia `ascii-output.txt`.
- ~~Lever-arm correction of positions to the camera footprint (camera is 1.27 m
  behind the laser origin) — record the geometry, defer the correction.~~
  **SUPERSEDED — Amendment J.** It is applied, to every camera and both lasers,
  and 1.27 m was the mounting-table nominal, not the measured arm.
- Overlaying laser data drawn onto the image pixels — MVP shows profiles in
  separate plots aligned by distance.
- Packaging as an .exe — run from source with a `launch.bat` only.
- ~~Editing/writing any collected data — the app is strictly read-only on inputs.~~
  **SUPERSEDED — see Amendment A.** Writing corrected positions into the
  collected data is now the app's primary purpose.

## ⭐ Primary user / persona:
Technical (Derek — builds and operates the collection cart, comfortable with
Python and the data, wants engineering-grade numbers he can trust).

## ⭐ Top user stories:
1. As the operator, I open a run root and pick a run; the app shows me the
   solved alignment offsets and their confidence so I know the run is good.
   *Accept:* both offsets shown with pass/fail state before any browsing.
2. As the operator, I step through rear images and see the Gocator L/R profiles
   for the same patch of ground.
   *Accept:* profile window tracks the image's ground-distance span
   (image center distance ± half the along-track footprint).
3. As the operator, I export the image→location table for a run.
   *Accept:* CSV with one row per image; unmatched images present but flagged.
4. ~~As the operator, I verify pixel-to-mm is correct using the calibration
   board lying in the scene.~~ **DROPPED — Amendment J.** That is a separate
   calibration workflow now.
5. ~~As the operator, I measure any feature in an undistorted image by clicking
   two points and reading mm.~~ **DROPPED — Amendment J.**
5b. As the operator, I process a whole collection day **without opening the
   GUI**, and something else branches on whether it worked.
   *Accept:* `cli.py process <collection>` exits 0 / 1 / 2 per Amendment J5.
6. As the operator, I open a run with a missing or misplaced input (e.g. the
   Qinertia export lives elsewhere) and the app tells me exactly what's missing
   and lets me browse to it; my choice is remembered next time.
   *Accept:* QC panel shows the missing item + "Locate…"; after locating, QC
   re-runs automatically and the path persists across restarts.

## ⭐ App type:
GUI desktop (Windows, PySide6/Qt) — **and, since 2026-09-02, a command line
over the same `core/`. See Amendment J.**

## ⭐ Platform standards to follow:
`gui-coding-standards.md` (bundled in this Spec folder) — read it before writing
any widget code. Always dark mode; 5-method `__init__` pattern; every widget
named; factory + populate + clear for dynamic widgets.
Layout approach: **pure code** (no Qt Designer .ui files).
Dynamic widgets generated at runtime: run-list entries (one row per discovered
run), alignment-status rows (one per check)~~, and measure-tool point
markers~~ (removed — Amendment J). Each needs the factory pattern.
Security: local offline app, no network, no sensitive data — full security file
not bundled; the input-validation rules below still apply.

## ⭐ Key screens / endpoints / commands:
One main window:
- **Left panel:** run list (auto-discovered), per-run **QC & alignment status**
  (data QC checks with PASS/WARN/FAIL, PTP offset + r, image↔trigger offset,
  matched/unmatched counts). Missing-input rows show a "Locate…" button that
  opens the browse dialog.
- **Center:** rear image viewer (QGraphicsView; raw/undistorted toggle; ~~measure
  tool; ChArUco verify button;~~ mm/px readout), with the **ROW image on top of
  the Rear image**, paired by trigger, view-only (Amendment J).
- **Right/bottom:** two pyqtgraph plots — Gocator Left and Right profiles (x mm
  vs z mm) for the current ground-distance window, with frame PTP/UTC readout.
- **Toolbar:** open run root, prev/next image, distance slider, export CSV.
Menu-level commands: Open Run Root, Export Alignment Table~~, Run Board
Verification~~.

**Command line (Amendment J):** `cli.py runs | check | process <collection>`,
with `--locate KEY=PATH`, `--no-images/--no-gocator/--no-csv`, `--calibrations`,
`--overrides`, `-q`. The exit code is the contract: 0 all written, 1 ran but
something needs a person, 2 could not run at all.

## ⭐ Roles / permissions:
None — single local user.

## ⭐ Data to store + key rules:
- No database. Outputs: export CSV per run (user picks destination; default
  `<run-root>/Exports/`). Optional small JSON cache of parsed trigger/alignment
  results per run (safe to delete; never modifies source data).
- ~~Collected data folders are READ-ONLY. Never write inside `Images/`,
  `GoCatorData/`, or `SBGData/`.~~ **SUPERSEDED — see Amendment A.** The batch
  writes GPS EXIF into `Images/**/*.jpg` and GPS columns into
  `GoCatorData/**/*.csv`, in place, by design. `SBGData/` is still never written.
- QSettings for window state and last-opened folder only.

## ⭐ Security & data handling:
N/A — offline desktop tool, no network, no credentials, no PII. Still: validate
every parsed file (column counts, units rows, monotonic timestamps); a malformed
file must produce a clear error naming the file and line, never a crash or a
silently wrong alignment.

## ⭐ Integrations:
File-format integrations only (no live hardware): LMI Gocator profile CSV
export, SBG Systems DataLogger text logs, SBG Qinertia ASCII export, OpenCV
calibration YAML. All formats are fully specified in **Data formats** below from
real collected samples — the coding agent does not need vendor manuals for MVP.

## ⭐ Target OS and hardware:
Windows 11 PC (Derek's "dpdev" machine). No GPU required. Data lives on `F:`.

## ⭐ Version control and environment:
Git (init a repo at the project root). Python `venv` at `.venv`. `launch.bat`
activates the venv and starts the app. `requirements.txt` with pinned versions.

## ⭐ Preferred tech stack:
Verified current via web search on 2026-08-18:
- Python 3.12 (broad wheel support; all libs below support it)
- PySide6 6.11.2 — GUI framework
- pyqtgraph 0.14.0 — profile plots (fast with dense data)
- numpy 2.3.x — array math, interpolation, correlation
- opencv-python 5.0.0.93 — undistort, ChArUco/ArUco detection (needs
  `cv2.aruco`, included in the main package)
- pytest 8.x — tests
- stdlib `csv`, `pathlib`, `datetime` — parsing and export (no pandas/polars;
  keep dependencies minimal per Derek's preference)

## ⭐ Testing approach:
pytest. Two layers:
1. Unit tests with small synthetic fixtures (checked into `tests/fixtures/`) for
   every parser and the alignment math — these run anywhere, fast.
2. Integration tests against the real sample run (paths in CONTEXT.md), marked
   `@pytest.mark.sampledata` and skipped automatically when `F:` data is absent.
   Ground-truth assertions for run `20260818.142721`:
   - eventOutA triggers: 82 (file has 2 header lines + 82 data rows)
   - rear images: 83 (off-by-one vs triggers must be detected)
   - PTP offset: 37.0 ± 0.1 s, correlation r ≥ 0.95
   - encoder scale: ≈ 4223.6 counts/m (± 1%)
   - first Gocator profile at ≈ 0.06 m DMI distance; last at ≈ 61.47 m
   - Gocator L: 2545 profiles; run total DMI distance 61.472 m

## ⭐ Preferences and tradeoffs:
Keep it simple and dependency-light. Accuracy and verifiability over polish:
every solved offset must be displayed, never hidden. Prefer explicit numbers
(offsets, correlations, errors) in the UI so a bad run is obvious. Memory-map or
stream large CSVs; never load a 130 MB file whole into the GUI thread.

## ⭐ Reference material:
See `CONTEXT.md` at the project root for the authoritative list, sample-data
paths, and known reference gaps. Camera calibration YAMLs live in
`F:\Sidewalk\002_App\AllCalibrations` (Pave: `20260815_PaveIntrinsic\PAVE_234500499.yaml`).

---

## Data formats (authoritative — decoded and verified from real collected data)

All facts below were verified empirically on run `20260818.142721` and export
`20260816.105016_0001` on 2026-08-18. Treat as ground truth; if a file
contradicts this section, stop and report rather than guessing.

### Coordinate/geometry constants

> **THE NUMBERS IN THIS SUBSECTION ARE SUPERSEDED — Amendment J.** They were
> the mounting-table nominals. The app no longer holds any of them as fact: it
> reads `AllCalibrations/LaserImageAlignmentLeverArms.md`, Derek's file, on
> every run. The axis conventions below are still right; only the values moved.

- Laser (Gocator) origin: X = 0. Positive X is forward (direction of travel).
  **Amendment J note:** the app's own frame is the BODY frame about the SBG
  cover target (the IMU origin), X forward, Y right, Z down — that is what the
  lever-arm file states and what `offset_by_lever_arm` rotates by heading.
- ~~Pave/Rear lens: X = −1.27 m, Y = 0, Z = −1.30 m, lens height above ground
  1.70 m.~~ **Measured: X = −0.928 m, lens height 1.667 m.** Camera points
  straight down (nadir). Bottom of the image is closest to the cart. The
  Gocator scans a ground point **~0.928 m** of travel BEFORE the rear camera
  passes over it. The 1.27 m figure is the mounting-table nominal and is kept
  in the code only as `NOMINAL_CAM_BEHIND_LASER_M`, so the old number stays
  recognisable.
- ~~(ROW lens, out of scope, for the record: X = −0.77, Z = −1.14, height
  1.54 m, 55.5° down, rearward.)~~ **ROW is in scope and measured: X = −1.69 m**
  — the ground at the image CENTRE row trails the cart by that much. Rough
  (two bars, one run, ±0.05 m).
- Gocators: **Y = ∓0.3575 m** (L negative, R positive); 0.7150 m apart.
- ~~Rear image ground footprint at nominal height: mm/px = 1700 mm / fx~~
  **mm/px = lens height × 1000 / fx, with the height read from the lever-arm
  file (1.667 m today)** → ≈ 0.604 mm/px, ≈ 1.74 m across-track × 1.12 m
  along-track. Consecutive images (0.75 m spacing) still overlap along-track.
  The height is a live input, not a constant: change the file and the scale,
  the plot window and the profile span all follow.

### Run root layout
```
<RunRoot>/
  Images/<YYYYMMDD.HHMMSS>/Rear/000000000750.jpg   (filename = distance counter in mm, step 750)
  GoCatorData/<YYYYMMDD.HHMMSS>/<YYYYMMDD>T<HHMMSS>L.csv and ...R.csv
  SBGData/<DDMMYY or YYMMDD>/<YYYYMMDD.HHMMSS> DataLogger/eventOutA.txt, utcTime.txt,
      DmiStationEx <YYYYMMDD.HHMMSS>.csv, (many other files — ignore)
  SBGData/.../export/ascii-output.txt   (Qinertia post-processed; may live in a
      separate processed dataset folder — let the user browse to it if absent)
```
Runs are matched across the three trees by the shared `YYYYMMDD.HHMMSS` stamp.
Note the SBG date folder may be `260818` (YYMMDD-style) — match on the run
stamp, not the parent folder name.

### Gocator profile CSV (`...L.csv` / `...R.csv`, up to ~133 MB)
Comma-separated, one header line:
`frameIndex,timestamp,ptpTimestamp,encoder,numberOfProfilePoints,numberOfValidPoints,bridgedValue,status,x0,z0,x1,z1,...`
- ~424 (x, z) pairs per row, values in mm; empty fields = invalid points (skip).
- `ptpTimestamp`: microseconds on the **PTP/TAI epoch**. UTC = ptp/1e6 − 37 s
  (TAI−UTC leap-second offset; see alignment spec below — verify per run, don't
  blindly hard-code).
- `timestamp` (col 2): the sensor's own free-running clock. It is NOT the SBG
  clock and NOT PTP-locked (differs by seconds and drifts ~2%). **Ignore it.**
- `encoder`: cumulative counts, ≈ 4223.6 counts/m.
- Access pattern: on first open, stream the file once to index (frameIndex,
  ptpTimestamp, encoder, byte offset) per row; afterwards read only the profile
  rows needed for display.

### eventOutA.txt (camera triggers, every 0.75 m)
Tab-separated, TWO header lines (names, then units):
`timestamp  status  timeOffset0..3` with units `(us) (na) (us)...`
- `timestamp`: SBG internal clock, microseconds. One row per 0.75 m trigger.
- `eventOutB.txt` is identical content (second output channel) — parse A only.

### utcTime.txt (SBG clock → UTC mapping, 1 Hz)
Tab-separated, two header lines:
`timestamp status gpsTimeOfWeek year month day hour minute second nanosecond ...`
- Build UTC(SBG µs) by linear interpolation between rows. Rows are 1 s apart;
  the SBG clock is stable over that span.

### DmiStationEx CSV (distance vs time)
Comma-separated, one header line: `Dmi (m), Speed (m/s), UTC TS (s), TimeOfWeek (s)`
- `UTC TS` is Unix-epoch UTC seconds. `Dmi` is cumulative travelled meters.
- **`TimeOfWeek` is unreliable** (does not match computed GPS TOW) — never use it.

### Qinertia ASCII export (`ascii-output.txt`, ~30 MB, 50 Hz)
Header: version line, project line, a column-description block, then a
tab-separated column-name line and a units line; data follows. 21 columns:
`GPS Time (s TOW), UTC Time (HH:MM:SS.SSS), Roll, Pitch, Yaw, [stds], velocities,
Delayed Heave, Delta Angles, Latitude (°), Longitude (°), Altitude MSL (m),
Timestamp (ms), Identification, Number`
- Parse by locating the `GPS Time` header line; do not hard-code the header
  line count.
- `UTC Time` is time-of-day only — take the date from the run stamp/GPS week.
  Handle a possible midnight rollover within a session.
- Verified: GPS Time − UTC time-of-day = exactly 18 s (GPS−UTC leap seconds).
- `Identification`/`Number` are event columns but were ALL `N/A` in the sample
  export (events not included). Do not depend on them; if a future export has
  events, surface them as a bonus but the alignment must not require them.
- `N/A` appears as a literal string in numeric columns (e.g. Delayed Heave) —
  treat as missing.
- Location lookup: linear interpolation of lat/lon/alt at a UTC time. Rows are
  20 ms apart so interpolation error is negligible. **Never extrapolate** —
  a query outside the export's window is an error shown to the user.

### Pave intrinsic calibration YAML
`F:\Sidewalk\002_App\AllCalibrations\20260815_PaveIntrinsic\PAVE_234500499.yaml`
OpenCV-style: `serial_number, calibration_datetime, rms_reprojection_error,
image_count_used, verdict, camera_matrix (3×3: fx 2757.59, fy 2758.04,
cx 1417.74, cy 918.78), dist_coeffs (5: k1 k2 p1 p2 k3), image_resolution
(2880×1860), board (squares_x 12, squares_y 8, cell 76.2 mm, marker 60 mm,
aruco_dict DICT_5X5_50)`.
- Select the newest calibration folder whose YAML prefix is `PAVE_`.
- The `board` block is the ground truth for the verification feature.

---

## Data quality checks (run automatically when a run is opened)

Three layers, reported per run in a QC panel as PASS / WARN / FAIL per check.
FAIL blocks export (viewer still works, with the failure shown). WARN allows
export but writes the warning into the CSV `notes` column.

### 1. Presence (required inputs)
For the selected run stamp, the app must locate: `Images/<run>/Rear/` with ≥1
jpg, Gocator `...L.csv` and `...R.csv`, `eventOutA.txt`, `utcTime.txt`,
`DmiStationEx *.csv`, the Qinertia `ascii-output.txt`, and the Pave calibration
YAML. (**Amended — see Amendment C:** the DMI CSV and the calibration YAML are
now OPTIONAL. Missing or malformed gives WARN, not FAIL, because neither is used
to place anything.)
- **If any required input is not found where expected, open a browse dialog
  telling the user exactly what is missing and what it is used for, and let
  them point at the file/folder.** This is normal, not an error — e.g. the
  ascii-output.txt often lives in a different processed-data folder.
- User-chosen paths are remembered per run (JSON sidecar in the app's own data
  dir, NOT in the collected-data folders) and re-validated on next open.
- Only if the user cancels the browse dialog does the check become FAIL.

### 2. Format (parseability)
Each file parses per **Data formats**: expected headers/columns present, units
lines where specified, numeric fields numeric (with `N/A` handling in
ascii-output). Any violation → FAIL naming file + line.

### 3. Content (consistency — each check with its threshold)
- Images: filenames are a continuous 750 mm arithmetic sequence — no gaps, no
  duplicates (gap → WARN listing missing counters); files readable as JPEG.
- eventOutA: timestamps strictly increasing; trigger count within ±3 of image
  count (else WARN); trigger-to-trigger DMI spacing 0.75 ± 0.05 m (§B check).
- utcTime: rows ≈1 Hz with no gap > 2 s; time span covers all triggers.
- DmiStationEx: distance monotonic non-decreasing; UTC span covers all
  triggers; final distance within 5% of (trigger count × 0.75 m).
- Gocator (each of L and R): ptpTimestamp strictly increasing; frameIndex
  continuous (gaps → WARN with count); encoder monotonic; valid-point ratio
  ≥ 50% median (else WARN); PTP offset solver §A must PASS; profile distance
  coverage reaches ≥ 95% of DMI total.
- ascii-output: rows ≈ 50 Hz with no gap > 1 s inside the run window; UTC
  window covers all trigger times AND all Gocator profile times (uncovered →
  FAIL, since location lookup never extrapolates); lat/lon within plausible
  bounds (|lat| ≤ 90, |lon| ≤ 180, non-constant).
- Calibration: YAML loads, resolution matches the images (2880×1860), RMS
  reprojection error ≤ 1.0 px (else WARN), verdict recorded.

The QC result set is structured data (check id, severity, message, values), so
tests can assert on it and the GUI just renders it.

---

## Alignment algorithms (the core of the app — implement exactly)

### A. PTP→UTC offset (Gocator)
1. Nominal offset = 37 s (TAI−UTC). Solve it per run instead of assuming:
   sweep candidate offsets (30–45 s, 0.05 s step); for each, map Gocator
   frame-midpoint times (ptp − offset) onto the RULER's UTC and correlate
   encoder-derived speed (Δencoder/Δt, normalized) against the ruler's
   interpolated speed. The solved offset is the correlation peak.
   (**Amended — see Amendment B:** the ruler is the corrected Qinertia export,
   not DmiStationEx. A coarse sweep is followed by a 0.01 s fine sweep and
   parabolic sub-step refinement.)
2. PASS if peak r ≥ 0.95 and |offset − 37| ≤ 0.5 s. Otherwise flag the run
   (show the number anyway) and refuse silent export.
   Ground truth on sample run: peak at 37.0 s, r = 0.993.

### B. Trigger→UTC→distance (images)
1. Parse eventOutA timestamps (SBG µs) → UTC via utcTime interpolation.
2. ~~Trigger distance = DmiStationEx `Dmi` interpolated at trigger UTC.~~
   **SUPERSEDED — see Amendment B.** Distance comes from the corrected Qinertia
   export, never from the raw wheel.
3. Sanity check: consecutive trigger distance deltas ≈ 0.75 m (tolerance
   ±0.05 m; report the distribution).

### C. Image↔trigger matching (solves the off-by-one)

> **SUPERSEDED — see Amendment B.** The distance-fitting method below was
> implemented, tested against real data, and REPLACED by ordinal tail-anchored
> matching. Distance cannot identify the shift. Do not re-implement the below.
1. Image claimed distance = filename value / 1000 (m); images are an arithmetic
   sequence with 0.75 m step.
2. Find the integer shift k that best maps image ordinals to trigger distances:
   minimize median |trigger_dist(i) − (image_dist(i+k) − d0)| over candidate
   k ∈ [−3, +3], where d0 is the per-run constant offset between the image
   counter zero and DMI zero (solved jointly — a 1-D fit per k).
3. Images with no trigger within 0.375 m after the fit are UNMATCHED — kept in
   the export, flagged, with no location. (Sample run: 83 images, 82 triggers →
   exactly one unmatched, expected to be the first image.)
4. Show k, d0, residual stats, and matched/unmatched counts in the status panel.

### D. Locations
For each matched image: trigger UTC → interpolate ascii-output.txt lat/lon/alt.
For each Gocator profile: (ptp/1e6 − solved offset) → same interpolation.
PTP time in exports = UTC + solved offset, reported in µs.

### Export CSV columns
`image_file, camera, run_id, image_ordinal, trigger_index, matched (bool),
utc_iso, ptp_us, dist_along_track_m, latitude_deg, longitude_deg,
altitude_msl_m, match_residual_m, ptp_offset_s, ptp_offset_r, notes`

(**Amended:** `camera` added — the export now covers every discovered camera,
one row per image per camera. `dmi_distance_m` renamed `dist_along_track_m`
because the distance no longer comes from the DMI. See Amendments B and D.)

---

## Rules for the coding agent

- **Read the bundled platform standards file (`gui-coding-standards.md`) before
  writing any code**, and follow it exactly: always dark mode; every QWidget
  subclass uses the 5-method `__init__` pattern (`_create_widgets`,
  `_create_layouts`, `_connect_signals`, `_apply_styles`,
  `_load_initial_state`); every widget has a named instance variable; no
  business logic in `_create_*` methods; dynamic widgets use factory +
  populate + clear.
- Ask if anything is unclear BEFORE starting — never assume silently.
- Never delete or overwrite existing files without explicit confirmation.
- ~~Collected-data folders are read-only. Never write inside them.~~
  **SUPERSEDED — see Amendment A.** Read-only EXCEPT the deliberate in-place GPS
  writes described there. Never write inside `SBGData/`. Never write anything the
  user has not confirmed through the pre-check dialog.
- Follow the phased plan (LaserImageAlignment_Phase.md). Between phases: run the
  full test suite, report results, and wait for "proceed". Do not advance if any
  test fails. Each phase must leave the codebase runnable. The proceed decision
  is made at the strongest tier (Opus 5), not delegated.
- Never work on two interdependent files simultaneously.
- If a dependency cannot be installed or versions conflict, STOP and report the
  exact error before continuing.
- If unsure whether a library is current or deprecated, web-search to verify
  before using it — version knowledge goes stale.
- No AI-generated placeholder data in production logic.
- Validate and sanitize all parsed input; a malformed data file must fail loudly
  with file+line context, never produce a silent misalignment.
- Keep the GUI thread free of file I/O — parse/index in worker threads
  (QThreadPool), report progress.

## Agent routing hints (capability-based, with named defaults)

Route by capability tier so cost is saved on trivial work. The strongest tier is
the default and the supervisor. Model names change — pair the tier with the
current default model so this still works if a name goes stale.

*Model names verified July 2026 — if this spec is much older than that, confirm
the names below are still current before relying on them.*

| Tier | Default model | Model string | Use for |
|------|---------------|--------------|---------|
| Strongest | Claude Opus 5 | `claude-opus-5` | Architecture, spec review, security review, complex/cross-file debugging, phase-gate decisions, the alignment algorithms (§A–D) |
| Mid | Claude Sonnet 5 | `claude-sonnet-5` | Feature implementation, boilerplate, refactoring, test writing |
| Cheapest | Claude Haiku 4.5 | `claude-haiku-4-5-20251001` | Renames, formatting, trivial one-line edits |

- **When in doubt, use the strongest tier.** Lesser tiers take only genuinely
  trivial, easily-verified work.
- **Escalate on doubt.** If a lesser tier produces anything questionable, or a
  task is harder than it looked, hand it to the strongest tier.
- GUI layout generation gets a human review step before proceeding.
- The alignment math (offset solver, matching) is strongest-tier work — it is
  the product; a subtle sign error here poisons every export.

## First tasks when coding begins

1. Read `CONTEXT.md` at the project root to orient — what each folder holds,
   which reference material is authoritative, what to ignore, and what's sample
   input vs. deliverable. Keep `CONTEXT.md` current.
2. Read the bundled `gui-coding-standards.md`.
3. Read and list all required software, libraries, and extensions.
4. Web-search to verify current stable versions and mutual compatibility.
5. Confirm the project folder exists (`F:\Sidewalk\002_App\LaserImageAlignment`).
6. Create `.venv`, install pinned dependencies, write `requirements.txt` and
   `launch.bat`.
7. Report setup complete with a version summary, then wait for "proceed" before
   writing any application code.

---

# Amendments (v0.2 — 2026-08-20)

Everything above was written before any code existed. v0.1 was built, run
against real collected data, and the findings changed the design. **Where this
section and the body disagree, this section wins.**

Each amendment says what changed and — more importantly — **why**, so a future
agent does not "fix" a deliberate decision back to the original spec.

---

## Amendment A — The app WRITES corrected positions into the data

**This is now the app's primary purpose.** The viewer is the tool for checking
the work; the write is the product.

**Why.** ACS assigns positions its own way and gets them wrong. Downstream
processes and the client consume the JPEGs and Gocator CSVs directly, so a
side-car table does not solve the problem — the corrected positions have to be
*in the deliverables*.

**What "Process All (write GPS)" does**, for every run under the open root:

- **JPEGs — every camera.** GPS EXIF written **in place**: lat/lon/alt/time,
  plus heading and speed. Cameras are *discovered* under `Images/<run>/`, not
  hard-coded (Rear/Pave, ROW, anything else present). Metadata-only insert:
  pixel data is byte-identical and existing ARAN tags (CameraID, DateTime,
  DocumentName…) are preserved. Schema matches Fugro PhotoDeveloper output.
- **Gocator CSVs.** `latitude,longitude,elevation` columns inserted **in place**
  immediately before `x0`, one position per profile. Streaming rewrite.
- **`Exports/`.** Per-run alignment CSV plus a timestamped batch report.
- **`SBGData/` is never written.**

**Mandatory safety properties.** These are requirements, not implementation
detail — do not weaken any of them:

1. **Pre-check before anything is written.** Inspect every run first and show
   the user the plan: which runs are ready, and which cannot be done — with the
   exact count of images and Gocator files that will get **no GPS**, and why.
   The user then chooses: process, locate a missing file, or cancel. A run that
   is skipped is left completely untouched.
2. **Atomic writes.** Every in-place write goes to a temp file in the same
   directory, is fsync'd, then `os.replace()`d. An interrupted run can never
   leave a damaged source file.
3. **Idempotent.** Re-running replaces values; it never duplicates columns or
   tags. Re-running the batch must always be safe.
4. **QC FAIL blocks the write.** A run that fails QC is skipped entirely —
   nothing modified — and listed in the report.
5. **Never fabricate a position.** An image with no match, or a time outside the
   export window, is left untagged and reported. A Gocator row with no position
   gets blank cells.
6. **The report is the hand-off.** Every anomaly must be listed there for
   checking before the next process in the chain. Repeated failures are
   collapsed: one root cause affecting 166 files is one line plus an example.
7. **The pre-check and the batch both run off the GUI thread** (they parse the
   full 50 Hz export for every run).

---

## Amendment B — Position and distance come from the corrected export ONLY

**Supersedes:** §A (correlate against DmiStationEx), §B step 2 (trigger distance
from `Dmi`), §C (distance-fitting matcher).

### B1. The ruler is the Qinertia export, not the DMI wheel

All locations **and** all along-track distances come from the post-processed
`ascii-output.txt` via PTP/UTC interpolation. Along-track distance is the
cumulative horizontal distance along the trajectory.

**Why.** `DmiStationEx` is uncorrected realtime data. The export is the
corrected product. Using the realtime distance for placement would bake a known
error into a deliverable.

It is kept only as a cross-check (`content.realtime_vs_export`). **Do not delete
the DMI parser** because it is "unused"; its job is to disagree.

> **Corrected 2026-08-20 — see Amendment F.** Earlier text here called
> `DmiStationEx` "the raw DMI wheel" and said a large disagreement means a
> hardware problem. Both are wrong. It carries the SBG's *estimated* distance,
> not encoder counts, so a disagreement is two estimates differing. Measure it
> over the collecting window only: over the full logged window it is dominated
> by GNSS noise integrated while the cart is parked.

### B2. Image↔trigger matching is ORDINAL (tail-anchored), not distance-fitted

`k = n_images − n_triggers`; trigger `i` pairs with image `i + k`. Refuse to
match at all if `|k| > 3`.

**Why the spec's distance fit was abandoned.** Both sequences ARE the same
physical trigger events in order. The image counter is driven by the camera
system's own wheel count while trigger distances come from the SBG DMI, and the
two disagree by a small scale factor (~0.15%, verified over 443 m on run
20260816.110840). So a constant offset `d0` cannot hold across a run — and once
a scale term is allowed, **distance cannot identify the shift at all**, because
both sequences are near-linear ramps. Every candidate `k` fits about equally
well.

The only identifying information is the count difference plus the physical fact
that recording stops with collection (the sequences end together) while extra
images come from pre-collection triggers at the **start**. Consistent with every
sample run: 83 img / 82 trg, 593 img / 592 trg.

Distance is still used — as a **diagnostic**. A robust linear fit
`trigger_dist ≈ a · image_dist + d0` reports the counter-vs-DMI scale `a` and
per-image residuals. `|a − 1| > 0.5%` is a WARN meaning wheel-calibration
mismatch (~0.9985 on sample data).

### B3. No-fix rows are dropped at parse

Real exports carry a few `(0,0)` placeholders and half-converged garbage at
session start/end. A single one poisons the cumulative along-track distance by
thousands of km. Rule: keep only rows within ~1° of the session's median
position (the median is robust to the outliers themselves). Report the count as
a WARN. Verified: 76 + 1 such rows in the 20260817 session.

---

## Amendment C — Optional inputs

**Supersedes:** the presence list in "Data quality checks §1".

`DmiStationEx *.csv` and the Pave calibration YAML are **optional**. Missing or
malformed → WARN, never FAIL.

**Why.** Neither is used to place anything. The DMI is a cross-check
(Amendment B1). The calibration drives only viewer features — undistort,
measure, board verification, the Gocator start/end overlay lines. Writing GPS
needs PTP + triggers + the export and nothing else, so **a missing or bad
calibration must never block a production write.**

**Calibration selection rule.** Auto-select the newest `PAVE_*.yaml`, but
**ignore any file with `Refined` in the name.** Those come from a separate
bar-run tool and have been observed with a 6× wrong focal length and no `board`
block (Derek, 2026-08-19).

---

## Amendment D — Multi-camera

**Partially supersedes:** the ROW non-goal.

Cameras are discovered under `Images/<run>/`, and **the batch geotags all of
them.** Each camera is matched to the triggers independently, so a camera with a
different image count cannot corrupt another. The export gains a `camera`
column.

Still out of scope, unchanged: **viewing and measuring** the ROW camera. Its
oblique ground-plane projection is not implemented. The viewer shows the primary
(Rear/Pave) camera only.

---

## Amendment F — Hardware topology (authoritative; Derek, 2026-08-20)

Recorded because the absence of this made two separate misdiagnoses possible in
one session. **Do not infer the topology from file names.**

- **ONE physical wheel encoder** feeds both the SBG and the Gocator.
- The **SBG** uses that encoder to aid its Kalman filter AND to fire the 0.75 m
  camera trigger pulses. `eventOutA.txt` is the SBG's log of those pulses.
- The **Gocator triggers itself** on the same encoder, records the encoder count
  on every profile, and takes PTP time from the SBG.
- **The raw encoder counts are never logged by the SBG.** Post-processing
  outputs velocity only. The count survives only on the Gocator rows and in the
  image filename counter.
- `DmiStationEx` therefore carries the SBG's **estimated** travelled distance,
  NOT encoder counts scaled to metres.
- **ACS** (ARAN Collection Software) also writes into the collection folder.

### What follows from this

1. `realtime_vs_export` compares two ESTIMATES. A disagreement is not evidence
   of a hardware fault. Measure it over the collecting window; over the full
   logged window it is dominated by GNSS noise integrated while parked
   (20260817_revrus3: 10-12 m of phantom distance in ~37 s of parking, which
   presented as "14% apart").
2. A drifting `Dmi` is expected of an estimate. On 20260817_revrus3 the SBG's
   implied trigger spacing walked 0.7471 -> 0.7696 m across four runs (+3%)
   while the Gocator-counts-per-trigger held at ~3170 (~1% spread, no trend).
   **That indicts the estimate, not the wheel.** Do not send anyone to check
   tyre pressure on the strength of this number. (Both of those numbers are
   now better explained by the camera trigger running on a VIRTUAL odometer —
   see "What fires the cameras" below.)
3. Gocator encoder decreases confined to the final profiles are the logger
   stopping, not travel. Seen on 20260817.180042: one -6528-count step on the
   last profile of BOTH sensors simultaneously. Profile placement uses PTP time,
   not the encoder, so it changes nothing.

### What fires the cameras — SETTLED 2026-08-20

**The SBG sync outputs are in `Virtual Odometer` mode, Distance 0.750 m,
Falling Edge, on BOTH channels.** Confirmed from the SBG configuration screen.

A *virtual* odometer is distance derived from the SBG's own navigation solution,
not from the wheel encoder. So:

> **THE CAMERA TRIGGER IS NOT ON THE PHYSICAL WHEEL.** The Gocator self-triggers
> on the real encoder; the cameras fire every 0.750 m of INS-*estimated* travel.
> They are two different rulers.

This retracts the earlier draft of this section, which claimed the camera trigger
and the laser shared one physical ruler and pitched "encoder-space alignment" on
that basis. **That idea is dead in its simple form** — you cannot assume
trigger `i` sits at `enc0 + i·N`, because the interval in real counts moves with
the quality of the nav solution.

Two further things this retracts, both wrong when first written:

- *"Image filenames are wheel counts."* They are not. Across all 8 camera folders
  of 20260817_revrus3 (660 images) every run starts at exactly 750 and every step
  is exactly 750, no exceptions — that is `ordinal × 750`, a label. Combined with
  the above, the filename does not even assert a reliable *ground* spacing: it
  asserts the virtual odometer's opinion.
- *"Gocator counts-per-trigger held to ~1%, so it's measurement noise."* It is
  not noise. That ~1% run-to-run spread is **the virtual odometer disagreeing
  with the real wheel**, and it is now the most direct measure of realtime INS
  distance error available in the data.

### What this does and does not break

**It does NOT corrupt placement.** The app never uses the nominal 0.75 m for
anything. An image's position comes from the trigger's recorded *time* through
the postprocessed export; a profile's comes from its PTP time through the same
export. Why the camera fired is irrelevant to where we say it was.

**It does mean two QC checks are weaker than they look.** Both
`align.counter_scale` and `content.trigger_spacing` compare the trigger cadence
against the nav ruler — realtime INS against postprocessed INS. That is a
useful convergence signal, but it is NOT an independent check and it says
nothing about a wheel. `align.counter_scale`'s message still says
"wheel-calibration mismatch if large" — that wording is wrong and should go.

**The only recorded quantity touching the physical wheel is the Gocator
`encoder` column.** ACS's `<DMIScaleFactor>` = 1063.17 counts/m (effective
2026-08-15, 60 m calibration course); the Gocator measures ~4223.6 counts/m,
≈4× that, consistent with X4 quadrature decoding of the same signal — inference
from the numbers, not confirmed.

If a physical ruler is ever wanted, the Gocator encoder is the only candidate,
and reaching it from a camera trigger still requires the PTP/SBG time link.
Validate anything built on it against `PostProcessed_*/camlaser_labels.csv`
(Derek's manual joint/bar clicks) first.

---

## Amendment G — The calibration bar (Derek, 2026-08-24)

> **THE TOOLING DESCRIBED BELOW IS NO LONGER IN THIS APP — Amendment J.**
> `core/bar.py`, its panel and its CSVs were removed on 2026-09-01. The bar
> *method* is kept here because it is the provenance of the two numbers the app
> now applies: it is how **0.928 m** (Rear) and **1.69 m** (ROW) were measured,
> and it is what somebody would repeat if the mount is changed. Bar detection
> belongs to a separate calibration workflow now; this app reads the answer out
> of `AllCalibrations/LaserImageAlignmentLeverArms.md`.
>
> Everything below is history. Read it as "how the number was got", never as a
> description of code that exists.

**A machined bar of known size, laid on the ground and driven over, is the
instrument that settles the lever arm.**

```
length     1000 mm   along-track
width        55 mm   across-track
thickness   6.35 mm  (1/4 inch)
```

### Why a bar and not another joint

As of 2026-08-20 the effective camera-behind-laser arm is known only to
**0.876–1.021 m** — a 145 mm spread. That spread has ONE cause, and it is not
the camera:

| side of the measurement | agreement |
|---|---|
| image — where the joint is in the picture | ±4 mm |
| laser — WHICH SCAN shows the same joint | ±4 scans = ±96 mm |

Picking the scan is a judgement call made by eye over a plot of 13 overlaid
profiles, and being wrong by four of them is the whole disagreement.

A bar removes the judgement. It is 1 m long, so it appears in ~42 consecutive
scans and then stops. The scan where it starts is found by **arithmetic** — the
first scan whose profile contains a 55 mm wide, 6.35 mm tall plateau — not by
eye. Its leading edge in the laser is therefore a number, and the arm falls out
of comparing that number to the same edge clicked in the picture.

### What it measures, and against what

| dimension | image | laser |
|---|---|---|
| length 1000 mm | rows × `fy` | ~42 scans × 23.98 mm |
| width 55 mm | columns × `fx` | profile x axis |
| thickness 6.35 mm | — (invisible to a nadir camera) | profile z axis |

Three known dimensions read by two independent instruments. The width and
thickness check the Gocator's own scales with no camera involved; the length
checks the along-track scale on both; the edge positions give the arm.

### Resolution floor

An edge is known only to lie between the last scan that missed the bar and the
first that caught it: **±12 mm** (half a scan pitch). That is the floor of the
method. It is reported in `laser_length_tol_mm`, not hidden. It is also seven
times better than the ±96 mm the joint method is currently delivering.

### Four corners, not two (Derek, 2026-08-24)

The bar is clicked as **four corners**, in any order — `order_corners()` sorts
them by angle about their centroid, so all 24 possible click orders give the
same rectangle and the user never has to remember a convention.

Two clicks could only measure one dimension, and only if both happened to land
on the same edge. Four give length and width **twice each** (opposite edges)
plus **squareness** — a real rectangle has equal diagonals, so any difference
is click error or a bar not lying flat. That check does not exist with two
clicks; a sloppy pair just returns a confidently wrong number. Squareness and
edge spread are written to the CSV as click-quality figures, so a measurement
can be weighted or thrown out later on evidence.

Both ends also come from the same four clicks, so one gesture yields both
lever-arm estimates instead of two.

### Workflow rules

- **Nothing is written without Save.** A measurement that commits itself the
  instant the fourth corner lands cannot be inspected first, and bad clicks are
  exactly what the list and Delete exist to keep out of the average.
- **Save is disabled until all four corners are down.** Three corners are not a
  rectangle, and a button that looks pressable but silently does nothing is
  worse than one that is plainly not ready.
- **Undo drops one corner; Cancel drops all four.** A mis-click must not cost
  the other three.
- **Pending corners are dropped when the image changes.** Corners are IMAGE
  pixels; on another image the same pixel is a different piece of ground, so
  carrying them over would silently measure the wrong thing. The panel says so
  — not the status bar, which `_show_current_image()` overwrites a moment later.
- **Selecting a saved row jumps to its image and redraws its clicks in blue**,
  distinct from the orange pending ones.
- **Delete rewrites the CSV atomically** (temp → fsync → `os.replace`), the
  same rule the batch GPS writes follow. Deleting the last row removes the file
  rather than leaving a header-only CSV, which reads as "measured, found
  nothing" instead of "never measured".

### Cursor and panning

Bar mode sets a **cross cursor** and moves panning to the **middle button**.
`ScrollHandDrag` owns the viewport cursor, so it has to be off for a cross to
survive; and corners are usually picked zoomed in, where the view still has to
be moved between clicks. An open-hand drag cursor covers the exact pixel being
aimed at, which is the measurement.

### Implementation notes

- `core/bar.py` — `detect_bar_in_profile()` fits a robust ground line
  (sigma-trimmed, so the bar itself does not drag the fit), then looks for a
  plateau of the right width AND height. **Both z signs are tried** — whether
  the bar reads as a rise or a dip depends on the Gocator's z convention, and
  guessing it wrong would make the detector silently find nothing.
- The search window is the image footprint **plus a whole bar length at each
  end**. A 1 m bar is longer than the 0.573 m half-span of the picture, so a
  window matching the photo would clip an end off every time and report a short
  bar. A bar with an end outside the searched window is flagged and its length
  reported as a **lower bound**, never as a short bar.
- Detections are cached per `(side, row)`; consecutive images overlap by most of
  a window, so stepping through a run stays responsive. The cache is cleared
  when the run changes — `(side, row)` means nothing once the Gocator files do.
- Bar mode is **opt-in**. It reads every profile in a ~2.3 m window off disk;
  that is far too slow to do on every image step just in case.
- Output: `Exports/bar_measurements.csv`, one row per clicked span, carrying
  both the image measurement and the laser measurement so the two can be
  compared later without the app.

### Still to confirm against real data

The plateau thresholds (`PLATEAU_FRACTION`, `WIDTH_TOL`, `THICKNESS_TOL` in
`core/bar.py`) are tuned on synthetic profiles. They are relative to the bar's
nominal size, so they should transfer — but the **first run on a real bar is
the test**, and the readout states measured width and thickness against nominal
precisely so that test needs no extra tooling.

---

## Amendment E — Still deferred

~~Unchanged from v0.1: lever-arm and pitch correction **of the written
positions**, laser-on-image overlay, per-image lens-height refinement from
Gocator Z, `.exe` packaging.~~

~~Not to be confused with the viewer fix of 2026-08-20: the profile window IS
now lever-arm corrected (`camera_ground_center_m`, 1.27 m) so the plots show
the ground in the picture. What stays deferred is correcting the lat/lon
*written into the JPEGs* from the nav reference point to the camera
footprint.~~

**REWRITTEN 2026-09-02.** The lever-arm half of this is done — see Amendment J.
The positions written into the JPEGs and the Gocator CSVs ARE corrected, per
sensor, rotated by heading.

Still deferred:

- **PITCH and ROLL** rotation of the arm. Only heading is applied. The measured
  arms already absorb a constant camera tilt and a constant cart pitch (that is
  what an *effective* arm measured from features seen in both streams means),
  so adding a pitch term on top would double-count. What is not covered is
  pitch that VARIES along a run.
- **Laser-on-image overlay** — profiles are still shown in their own plots.
- **Per-image lens-height refinement from Gocator Z.** The height is one number
  in the lever-arm file, constant for the run.
- **`.exe` packaging.** `launch.bat` for the viewer, `cli.py` for the batch.

---

## Known-stale statements elsewhere in this file

Marked inline above; listed here so nothing is missed.

| Section | Statement | Status |
|---|---|---|
| Explicit non-goals | "strictly read-only on inputs" | Reversed — Amendment A |
| Data to store | "Never write inside `Images/`, `GoCatorData/`, `SBGData/`" | `SBGData/` only — Amendment A |
| Rules for the coding agent | "Collected-data folders are read-only" | Amendment A |
| Data quality checks §1 | DMI + calibration in the required list | Now optional — Amendment C |
| Alignment §A | correlate against DmiStationEx speed | Ruler is the export — Amendment B1 |
| Alignment §B step 2 | trigger distance from `Dmi` | Amendment B1 |
| Alignment §C | distance-fitting matcher, joint (k, d0) fit | Replaced — Amendment B2 |
| Export CSV columns | no `camera`; `dmi_distance_m` | Amendment B/D |
| Amendment B1 (as first written) | "the raw DMI wheel"; disagreement = hardware fault | Corrected — Amendment F |
| Data formats — DmiStationEx | implies `Dmi (m)` is wheel distance | It is an SBG estimate — Amendment F |
| Coordinate constants | Rear X −1.27 m, height 1.70 m; ROW X −0.77 m | Measured: −0.928, 1.667, ROW −1.69 — Amendment J |
| Coordinate constants | mm/px = 1700 / fx | Height is a live input from the lever-arm file — Amendment J |
| Run root layout | runs matched on the exact stamp string | Fuzzy within 3 s for `SBGData/` — Amendment H |
| MVP §6, Success criteria | measure tool, ChArUco board verification | Removed — Amendment J |
| Key screens | measure tool, Run Board Verification, GUI only | Removed; ROW pane and a CLI added — Amendment J |
| Explicit non-goals | "ROW camera — everything"; lever arm deferred | Both reversed — Amendments D, J |
| Amendment G | the calibration bar as app tooling | Removed from this app — Amendment J |
| Amendment E | lever-arm correction of written positions deferred | Done — Amendment J |
| Data quality checks §1–3 | no Status X, no `QC_Video.csv` | Added — Amendment J |

**Unchanged and still authoritative:** the entire "Data formats" section
*except the coordinate/geometry constants and the stamp-matching sentence*
(both listed above), §A's pass thresholds (r ≥ 0.95, |offset − 37| ≤ 0.5 s),
§D location lookup with no extrapolation, all QC content thresholds not listed
above, `gui-coding-standards.md`, and the tech stack.

**If this file and the code disagree, the code is right and this table is
missing a row.** Add the row.


---

## Amendment H — the run stamp and the SBG log folder are two different clocks (Derek, 2026-08-26)

The original spec matched a run across the `Images/`, `GoCatorData/` and
`SBGData/` trees on **the exact `YYYYMMDD.HHMMSS` stamp string**. That is wrong.
The collection system and the SBG logger open their per-run folders
independently, so on the same physical run the two can disagree by a second:

    Images/20260824.100258/          GoCatorData/20260824.100258/
    SBGData/260824/20260824.100259 DataLogger/

Under exact matching, that one run was discovered as **two**, each missing what
the other had — one with 86 images and no triggers, one with triggers and no
images. Both failed the presence check, both were skipped by the batch, and
**86 images plus 2 Gocator CSVs silently received no GPS.** The batch report
read "13 runs" for a 12-run day, which is the only visible symptom.

### The rule now

* A run is named by the **collection system** stamp (`Images/`,
  `GoCatorData/`). That id names the export CSV, the `Ortho/` folders and every
  saved bar measurement; renaming a run to follow the SBG clock would orphan
  all of them.
* Its SBG log folder is the nearest DataLogger within
  `MAX_SBG_STAMP_SKEW_S = 3` seconds, compared **as times, not strings** (so
  23:59:59 and 00:00:00 next day are one second apart, as they should be).
* Assignment is **nearest-first and greedy**: an exact match always claims its
  own logger before any fuzzy candidate can take it, and no logger is ever
  given to two runs.
* Anything that cannot be paired stays unpaired, and a DataLogger that pairs
  with no run still becomes a run of its own so it remains visible in the list.

### Why 3 seconds

Consecutive runs start about a minute apart in practice — 60 s was the closest
gap observed across 20260824_revruns. Three seconds is ~20x inside that margin,
so the window cannot reach a neighbouring run.

### Why a fuzzy pairing is never silent

A skew ≠ 0 raises `presence.sbg_stamp_skew` (**WARN**), naming the DataLogger
folder actually used and the offset. WARN, not FAIL: the pairing does not need
to block a write, and the checks that would actually catch a *wrong* pairing —
the ordinal image/trigger count match (`MATCH_MAX_SHIFT`) and the PTP fit
(r ≥ 0.95, |offset − 37| ≤ 0.5 s) — run either way and would refuse the run.
That was confirmed on the real case: 20260824.100258 paired with the +1 s logger
matched 42/43 images at k=+1 with a 11 mm median residual, identical in shape to
every correctly-named run that day.

### Superseded

| Original text | Replaced by |
| --- | --- |
| Discovery §"matched across the trees by their shared `YYYYMMDD.HHMMSS` stamp" | Collection stamp names the run; the logger is paired within ±3 s — Amendment H |


---

## Amendment I — the batch report must distinguish "none found" from "none written" (Derek, 2026-08-26)

The per-run summary line printed `{images_tagged} images`. On a run that failed
QC, nothing is tagged, so it read:

    [FLAG] 20260824.101724: 0 images (-), 0 profiles tagged

That run held **76 images** (38 Rear + 38 ROW). "0 images" states that the run
has no images; the truth was that its images were deliberately left untouched.
Those are opposite conclusions about the data, and the report is the only record
anyone reads afterwards.

### The rule now

* `RunOutcome` carries **`cameras_found`** and **`gocator_found`** — counted
  from disk *before* processing is attempted, so the numbers survive any
  failure, including an unhandled exception.
* The summary line always reads **"N of M images tagged"**, never a bare
  "N images".
* **Every** non-written outcome — `skipped`, `flagged`, and `error` alike —
  emits a `NOT WRITTEN (no GPS): …` note listing the files left alone.
  Previously only `skipped` did, so a flagged run gave no indication of how much
  data was affected.

### Principle

A count of what the app *did* is not a count of what is *there*. Wherever the
report prints an action count that can legitimately be zero, it must also print
the population it acted on, or the zero will be misread as absence.


---

## Amendment J — production processing app (Derek, 2026-09-01 / 2026-09-02)

Four changes, made over two days, that together move this from "a viewer with
calibration tools in it" to **an app that processes a collection day**.

### J1 — the app measures nothing

> *"I want to remove the code for measuring, verify board, bars completely out
> of the app just so it is an app for production processing."* — Derek

Removed: the two-point **Measure** tool, **Verify Board** (ChArUco), **Ground
Truth** clicks, and the whole calibration-bar tool (`core/bar.py`,
`gui/bar_panel.py`, `bar_measurements.csv`, `ground_truth_clicks.csv`). About
2,200 lines, plus 190 lines of click handling out of `gui/image_view.py`.

What stays, and why: **the viewer, and Undistort with it.** Derek's scope, in
his words — *"the gui interface with displayed images to verify the alignment
of the lasers and images would stay. Measurement is out and calibration
processes are out"*, and *"Undistort stays / Ground Truth clicks out"*. Seeing
the picture next to the laser profiles is how a run gets checked; the
ground/pixel model is only exact on the undistorted picture.

Consequence for the reader of this spec: **wherever a measurement tool is
described above, it is history.** The bar method in Amendment G is kept only as
the provenance of the numbers in J2.

### J2 — the lever arms come out of a file the user owns

`AllCalibrations/LaserImageAlignmentLeverArms.md`. Derek's file, deliberately
plain, so a human can change a number without touching code:

```
## Rear camera      X -0.928  Y 0  Z 0
## ROW camera       X -1.69   Y 0  Z 0
## Gocator left     X 0  Y -0.3575  Z 0
## Gocator right    X 0  Y +0.3575  Z 0
## Rear lens height above the pavement
1.667
```

Metres from the SBG cover target (the IMU origin), X forward, Y right, Z down.

Rules the code follows and must keep following:

* **Read every time, never cached.** The first version keyed a cache on
  `(path, mtime)`; Windows mtime is coarse enough that two edits inside one
  tick share a timestamp, so the second edit was ignored and the app went on
  using numbers the file no longer contained. Some editors also restore the old
  mtime. Either way the failure is silent, and it moves where a photograph says
  it was taken. The file is a few hundred bytes. Read it.
* **A missing or broken file falls back to the built-in constants and SAYS SO.**
  "The arms came from the file" and "the file was not there" must not look the
  same.
* **Reading it must never be able to raise.** It is read from inside
  `process_collection`, outside any `try`, so an exception here kills a whole
  collection day before a single report is written. It was possible: the file
  is hand-edited and Notepad still offers ANSI and UTF-16, so
  `read_text(encoding="utf-8")` on a file containing a degree sign raised
  `UnicodeDecodeError` — a `ValueError`, not caught by the `except OSError`
  around it. It now sniffs the BOM, tries UTF-8, and falls back to cp1252,
  which decodes any byte at all.
* **A camera not listed gets `None`, not zeros.** Zero is a claim that the lens
  sits exactly on the IMU. Stamping a confidently wrong position on a client
  deliverable is worse than stamping the uncorrected one and reporting it.
* The lens height is the one scalar in the file. It is *not* a lever arm — it
  is a height above the ground — but it sets mm/px, the profile window and the
  plot span, so it belongs where a human can change it. It touches no written
  position.

### J3 — one position per image, and it is the armed one

> *"The viewer shows an un-armed position while EXIF gets the armed one — your
> app is aligned, how does that work?"* … *"it is the only way to ensure
> alignment."* — Derek

There was a second, un-armed `align_images()` call whose result the viewer and
some export paths used. **It is gone.** `align_images` takes `arm_xyz`, every
camera is aligned once with its own arm, and `result.alignment` is simply the
primary camera's entry in `camera_alignments`. There is now no code path that
can produce a different position for the same image.

The bug this closed: the **Locate…** path never populated `run.cameras`, so
`camera_alignments` was empty and both the CSV export and the batch fell back
to the un-armed alignment — 0.928 m out on Rear, 1.69 m on ROW, silently.

**Rule: one image, one position.** If a second alignment ever appears, the two
will drift and only one of them will be in the client's file.

### J4 — the ACS Daily file decides what runs

* **`Status` X (any case, any padding) means DO NOT PROCESS.** False starts,
  abandoned sections, alignment rolls. Nothing is written into such a run — no
  GPS, no rename, no `BeforeCollection/`.
* A blank Status is **not** an exclusion. Every Daily file written before the
  column existed would otherwise stop the app dead.
* Excluded runs are **kept, not dropped**, and reported separately from
  `ignored`. "No row in the Daily file" and "a row that says do not use this"
  are different facts, and a folder full of data deserves the right
  explanation.
* **"Not touched" includes the repair pass.** `_recover_interrupted_renames`
  runs before anything is read — that is the point of it — and so it ran before
  the Daily file was consulted and reached straight into Status X runs. It now
  takes the excluded set and skips them.
* **`QC_Video.csv`**, when the collection has one, is cross-checked against the
  triggers — but **by AMOUNT ONLY, never by position.** ACS records how much
  video was lost, not where. Comparing positions would invent a precision the
  source does not have.

### J5 — the command line

`cli.py`: `runs`, `check`, `process`. Thin — it parses flags and turns the
batch report into an exit code. It is possible only because `core/` has never
imported Qt, and `tests/test_cli.py` fails if that ever changes.

**The exit code is the contract.** The point of a command line is that a
scheduler or a batch file can branch on the answer without reading English:

| code | meaning |
|---|---|
| 0 | every run was written |
| 1 | it ran, but something needs a person |
| 2 | it could not run at all |

1 and 2 are deliberately different. *"Nine runs written, one skipped because
its export is missing"* is a normal end to a collection day and must not look
the same as *"that folder is not a collection"*. A bad `--locate` key or a path
that is not there exits **2**: nothing was attempted, so it must not read as
"ran, and something needs a person".

`--locate KEY=PATH` is the **Locate…** button for a shell, and applies to
*every* run missing that input — the reason an export is missing is almost
always that the whole day's Qinertia output lives somewhere else. It writes the
same override sidecar the viewer reads, so a path located in one is known to
the other.

`--no-images --no-gocator --no-csv` is a dry run and **has to mean it.** Two
bugs were found by writing the test that says so:

1. It still renamed every image and moved pre-section frames into
   `BeforeCollection/`. A dry run that moves files is worse than no dry run at
   all, because the person running it believes nothing moved.
2. Its saved batch report ended *"AFFECTED COLLECTION: none — every deliverable
   was written."* — the opposite of the truth, in the document somebody opens a
   week later to ask what that batch did.
3. The mid-rename repair pass renamed files anyway, because it runs before any
   flag is consulted. It now **finds** the stranded images and reports them —
   which a dry run must do, since that is exactly the condition that makes a
   run scan short and get refused — without moving one.

All fixed. The report now carries what the batch was allowed to write, headers
itself `*** DRY RUN ***`, and says what would have happened.

**The general rule this keeps producing:** a flag that says "do not write" has
to reach *every* writer, including the ones that run before the main loop and
think of themselves as maintenance rather than output. Grep for `os.rename`,
not just for the obvious ones.

### Principle behind all five

An app that is about to be shipped should contain only what production needs,
and should be honest about what it did. Every fix above is one of those two:
take out what the operator will never use, or stop a report from saying
something that is not so.
