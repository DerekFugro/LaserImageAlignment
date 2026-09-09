---
# Implementation Plan for LaserImageAlignment

> 🚫 **NO LEVER-ARM OR LENS-HEIGHT VALUE IN THIS FILE IS CURRENT.** Every such
> number below is what a phase gate was checked against on the day, kept as
> provenance. The ONLY source is
> `LaserImageAlignmentLeverArms.md` in the calibrations folder — open it and
> read it. Do not copy a value out of this file into anything.
>
> ✅ **ALL SIX PHASES ARE COMPLETE (2026-09-02). This file is history now, not
> a to-do list.** It is kept because it records the order the app was built in
> and the ground-truth numbers each gate was checked against — both still
> useful. What it does not record is what changed after the gates. Before
> treating anything below as current, read this box, then the Amendments in
> `LaserImageAlignment_Instructions.md`.
>
> **What landed differently from the plan:**
>
> | Phase | Planned | What is actually there |
> |---|---|---|
> | 2 | runs matched on the exact `YYYYMMDD.HHMMSS` stamp | the collection stamp names the run; the `SBGData/` logger folder is paired within ±3 s (Amendment H) |
> | 3 §C | image↔trigger matcher by joint `(k, d0)` distance fit | **ORDINAL, tail-anchored** — distance cannot identify the shift, the two counters differ by a ~0.15% scale (Amendment B2) |
> | 3 | the DMI wheel as the distance ruler | the corrected Qinertia export is the ruler (Amendment B1) |
> | 4 | Rear image only | **ROW above Rear**, paired by trigger, view-only (Amendment J) |
> | 5 | undistort, mm/px, measure tool, ChArUco verification | **the phase is gone from the app except undistort and mm/px** (Amendment J1) |
> | 5 | mm/px = 1700 / fx, 1.70 m nominal | the height is read from `AllCalibrations/LaserImageAlignmentLeverArms.md` — 1.667 m today (Amendment J2) |
> | 6 | GUI only, via `launch.bat` | plus `cli.py` — `runs` / `check` / `process`, exit codes 0/1/2 (Amendment J5) |
> | — | not planned at all | the app WRITES GPS into the deliverables and RENAMES every placed image (Amendment A); the ACS Daily file decides what runs (Amendment J4) |
>
> The largest thing here has no phase behind it at all: **the batch** — process
> a whole collection day, write the positions, rename, and report. That is the
> product. See Amendments A, I and J.
>
> State at hand-off: **329 tests + 5 skipped** (the skipped ones need the F:
> sample data). Branch `camera-lever-arm`.

> *(The original gating rule, as written 2026-08-18:)* Gated phases. After each
> phase, run the full test suite, report results, and wait for "proceed". Do not
> advance if any test fails. Each phase must leave the codebase in a runnable
> state. The proceed decision is made at the strongest model tier (currently
> Claude Opus 5).

## Phase 1 — Environment Setup
**Complexity:** Simple
**Goal:** Reproducible Python environment, project skeleton, launch.bat, empty
app window opens.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| Python | 3.12 | Runtime |
| PySide6 | 6.11.2 | GUI framework |
| pyqtgraph | 0.14.0 | Profile plots |
| numpy | 2.3.x | Array math, interpolation, correlation |
| opencv-python | 5.0.0.93 | Undistort + ChArUco detection |
| pytest | 8.x | Tests |

### Tasks
1. Read `CONTEXT.md` and the bundled `gui-coding-standards.md`.
2. `git init`; create `.venv`; install pinned deps; write `requirements.txt`.
3. Project skeleton: `core/` (parsers, alignment), `gui/` (widgets, styles.py),
   `tests/` (+ `tests/fixtures/`), `launch.bat` (activate venv, run app).
4. Minimal dark-mode main window (empty panels, correct 5-method structure).
5. Verify `import cv2; cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)`
   works — the ArUco module is required later.

### Done when:
- [ ] `launch.bat` opens the dark-mode window on Windows 11.
- [ ] `pytest` runs (even with only a smoke test) and passes.
- [ ] `requirements.txt` pins all versions; repo committed.

---

## Phase 2 — Data Layer (parsers + run discovery)
**Complexity:** Moderate
**Goal:** Every input format parses correctly into typed structures; runs are
auto-discovered from a run root. No GUI wiring yet beyond a stub.

### Dependencies
No new libraries.

### Tasks
1. Run discovery: scan `Images/*/Rear`, `GoCatorData/*`, `SBGData/**/"* DataLogger"`,
   ~~match runs by the `YYYYMMDD.HHMMSS` stamp~~ (the SBG folder is paired
   within ±3 s — Amendment H); report which parts each run has.
2. Parsers (each per the **Data formats** section of the Instructions, each with
   synthetic-fixture unit tests):
   - eventOutA.txt (2 header lines, µs timestamps)
   - utcTime.txt → SBG-µs→UTC interpolator (handle the 2 header lines)
   - DmiStationEx CSV → UTC-epoch→distance and →speed interpolators
   - ascii-output.txt → UTC→lat/lon/alt interpolator (header located by the
     `GPS Time` line, `N/A` handling, date from run stamp, midnight rollover,
     no-extrapolation guard)
   - Gocator CSV: streaming indexer (frameIndex, ptpTimestamp, encoder, byte
     offset per row) + on-demand profile-row reader (blank fields skipped)
   - Image list: filename → distance counter, sorted, step-750 validation
   - Pave calibration YAML loader (newest `PAVE_*` in the calibrations folder)
3. QC engine — layers 1 and 2 of the **Data quality checks** section of the
   Instructions: presence checks (returning a structured missing-inputs list —
   the GUI turns these into browse prompts later) and format checks. Structured
   result objects (check id, severity, message, values).
4. Per-run path-override store: JSON sidecar in the app's own data dir mapping
   run → user-chosen paths for missing inputs; re-validated on load. Never
   writes into collected-data folders.
5. Integration tests against sample run `20260818.142721` (marked
   `sampledata`, auto-skip when `F:` absent): 82 triggers, 83 rear images,
   2545 L profiles, DMI total 61.472 m.

### Done when:
- [ ] All parsers pass unit tests on synthetic fixtures.
- [ ] Sample-data integration tests pass on the real run.
- [ ] QC presence/format checks produce correct structured results for a
      complete run, a run with a missing export, and a malformed file — all
      covered by tests.
- [ ] A malformed file (wrong columns, garbage line) raises a clear error with
      file + line — covered by a test.

---

## Phase 3 — Alignment Engine
**Complexity:** Complex — strongest tier (Opus 5). This is the product.
**Goal:** The three solvers (§A, §B/C, §D of the Instructions) implemented,
tested against ground truth, and exposed as a clean API for the GUI.

### Dependencies
No new libraries.

### Tasks
1. §A PTP→UTC offset solver: encoder-rate vs DMI-speed correlation sweep
   (30–45 s, 0.05 s step), peak + r reported, pass/fail per thresholds.
2. §B trigger→UTC→distance pipeline with the 0.75 m spacing sanity check.
3. §C image↔trigger matcher: ~~joint (k, d0) fit~~ **ORDINAL, tail-anchored —
   Amendment B2**; unmatched-image flagging.
4. §D location lookup for images and profiles; PTP↔UTC conversions.
5. QC engine — layer 3 (content checks): image sequence continuity, trigger
   spacing, coverage checks, Gocator monotonicity/coverage, ascii-output window
   coverage, calibration sanity — thresholds exactly as the Instructions'
   **Data quality checks** section. FAIL blocks export; WARN goes to the CSV
   `notes` column.
6. Export CSV writer (exact column list from the Instructions).
7. Ground-truth tests on run `20260818.142721`: offset 37.0 ± 0.1 s, r ≥ 0.95;
   encoder scale 4223.6 counts/m ± 1%; exactly one unmatched image; every
   matched image's UTC inside the export window.

### Done when:
- [ ] All ground-truth assertions pass on the sample run.
- [ ] Export CSV produced for the sample run and manually spot-checked
      (human review at the gate).
- [ ] Solver returns structured results (values + confidences + pass flags),
      no prints.

---

## Phase 4 — GUI Viewer
**Complexity:** Moderate — mid tier + human review of layout.
**Goal:** Full main window: run list, alignment status panel, image viewer,
L/R profile plots, navigation, export button.

### Dependencies
No new libraries.

### Tasks
1. Run-list panel (factory pattern per row) + background loading via QThreadPool
   (parsing/indexing off the GUI thread, progress reporting).
2. QC & alignment status panel: all QC checks with PASS/WARN/FAIL colors
   (dark-mode palette from styles.py), solved offsets, r, matched/unmatched.
   Missing-input rows get a "Locate…" button → browse dialog stating what is
   missing and what it's used for → QC re-runs automatically; chosen path saved
   to the per-run override store. FAIL state disables the export button with a
   tooltip naming the failing check.
3. Image viewer: QGraphicsView, fit/zoom/pan, prev/next, distance slider,
   current image's UTC/PTP/lat-lon readout.
4. Gocator L/R pyqtgraph plots showing profiles within the current image's
   ground-distance span (center ± 0.575 m along-track); frame readout.
5. Export button → §D CSV with file-save dialog defaulting to
   `<run-root>/Exports/`.

### Done when:
- [ ] Open sample run root → browse all runs → images and profiles track
      together with no GUI freezes (130 MB CSVs stay responsive).
- [ ] Opening a run missing its Qinertia export triggers the Locate… flow;
      after pointing at the file, QC re-runs and the path survives an app
      restart.
- [ ] Export from the GUI matches the Phase-3 CSV byte-for-byte.
- [ ] gui-coding-standards checklist passes (human review at the gate).

---

## Phase 5 — Calibration & Verification
## ❌ REMOVED FROM THE APP 2026-09-01 — see Amendment J1

> Built, used, and then taken out at Derek's request before hand-off: *"I want
> to remove the code for measuring, verify board, bars completely out of the app
> just so it is an app for production processing."* **Undistort and the mm/px
> readout survive** (task 1, and task 2 with a different height source); tasks
> 3, 4 and 5 are gone, along with the calibration bar that came later.
>
> The work was not wasted — it is what measured the lever arms the app now
> applies. It just belongs to a separate calibration workflow, not to a
> production processing app. Read the rest of this phase as history.

**Complexity:** Moderate
**Goal:** Undistortion, mm/px scale, measure tool, ChArUco board verification.

### Dependencies
No new libraries (opencv-python already installed).

### Tasks
1. Undistort toggle (precompute maps once per calibration; cache).
2. ~~mm/px = 1700 / fx readout; document that 1.70 m is nominal lens height.~~
   **Kept, but the height is now read from the lever-arm file — 1.667 m
   measured, not 1.70 m nominal (Amendment J2).**
3. Measure tool: click two points on the undistorted image → distance in mm
   (marker widgets via factory pattern).
4. ChArUco verification: detect board (DICT_5X5_50, 12×8, 76.2 mm cells) in the
   current undistorted image, compute mean detected cell spacing in px, convert
   via mm/px, report measured vs 76.2 mm and % error; PASS at ≤ 2%.
5. Tests: undistort a synthetic grid; verification math on a rendered synthetic
   ChArUco image (generate with cv2.aruco, known scale).

### Done when:
- [ ] Board verification runs on a real board image and reports a number
      (human confirms plausibility at the gate).
- [ ] Measure tool returns 76.2 ± 2% mm across one board cell in that image.
- [ ] All tests green.

---

## Phase 6 — Polish & Hand-off
**Complexity:** Simple
**Goal:** Robustness, docs, final review.

### Tasks
1. Error-path hardening: missing run parts (no export file, missing DMI),
   partial runs, empty folders — clear user-facing messages.
2. README.md: setup, launch, workflow, what each status number means.
3. QSettings persistence (window geometry, last run root).
4. Final full test run + manual walkthrough of all 5 user stories.
5. Update `CONTEXT.md` with the final code layout.

### Done when:
- [x] All user stories demonstrated end-to-end on the sample data.
- [x] Full test suite green; README complete; repo tagged v0.1.

**Phase 6 happened twice.** The second pass, 2026-09-01/02, is the one that
matters for hand-off: the pre-ship audit, the removal of everything that was
not production processing, the CLI, and this documentation sweep. See
Amendment J.

---

## Deferred (design for, don't build)

**Updated 2026-09-02 — three of these five were built.**

- ~~ROW camera support (oblique ground-plane projection) — keep camera handling
  behind an interface so a second camera model can slot in.~~ **PARTLY DONE.**
  ROW is discovered, geotagged with its own arm, and shown in the viewer above
  the Rear image. Its oblique ground-plane *projection* — measuring on it — is
  still not built.
- ~~Lever-arm position correction (camera X = −1.27 m along heading; yaw is
  available in ascii-output.txt).~~ **DONE**, for every camera and both lasers,
  rotated by heading. The arms come from
  `AllCalibrations/LaserImageAlignmentLeverArms.md`, and −1.27 m was the
  mounting-table nominal — the measured Rear arm is **−0.928 m** (Amendment J2).
  Pitch and roll are still not rotated in.
- Laser-on-image overlay by ground distance. **Still deferred.**
- Per-image lens-height refinement from Gocator Z data. **Still deferred** —
  the height is one number per run, out of the lever-arm file.
- ~~Qinertia exports that include event rows (Identification/Number) — surface
  them if present, never require them.~~ **DONE** — `core/events.py`.

---

## Agent routing per phase (capability-based, with named defaults)

Route by tier; the strongest tier is the default and supervisor. Pair the tier
with the current default model so this survives model renames.

*Model names verified July 2026 — confirm they are still current if this spec
is much older than that.*

| Phase | Recommended tier (current default) |
|-------|-----------------------------------|
| 1 — Environment Setup | mid (Sonnet 5 / `claude-sonnet-5`) |
| 2 — Data Layer | mid (Sonnet 5); strongest (Opus 5 / `claude-opus-5`) for the ascii-output date/rollover logic |
| 3 — Alignment Engine | strongest (Opus 5) — the core algorithms |
| 4 — GUI Viewer | mid (Sonnet 5) + human review step |
| 5 — Calibration & Verification | mid (Sonnet 5); strongest (Opus 5) for the verification math review |
| 6 — Polish & Hand-off | cheapest (Haiku 4.5 / `claude-haiku-4-5-20251001`) for mechanical work; mid (Sonnet 5) otherwise |
| Phase-gate decisions (every phase) | strongest (Opus 5) — never delegated |

When in doubt, use the strongest tier. Escalate anything questionable.

---

## Total estimate
| Phase | Complexity |
|-------|------------|
| 1 — Environment Setup | Simple |
| 2 — Data Layer | Moderate |
| 3 — Alignment Engine | Complex |
| 4 — GUI Viewer | Moderate |
| 5 — Calibration & Verification | Moderate |
| 6 — Polish & Hand-off | Simple |

---
