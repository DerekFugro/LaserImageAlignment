# LaserImageAlignment — getting it running on this machine

**Read this first.** It is written for GitHub Copilot (or any coding agent)
setting the app up on a machine that is not the one it was built on.

You should have two zips and this file. Unzip both, side by side:

```
LaserImageAlignment\     the app
AllCalibrations\         the calibration files the app reads
```

You supply the third thing yourself: a **collection** to process. None is
included — one run is about a gigabyte.

---

## Step 1 — install uv (recommended, 30 seconds)

```bat
winget install --id=astral-sh.uv
```

(or `pip install uv`, or see <https://docs.astral.sh/uv/>). Open a new terminal
afterwards so it is on `PATH`.

**Why bother:** this app needs **Python 3.12 exactly** — the bundled wheels are
cp312 on 64-bit Windows, so on 3.11 or 3.13 the offline install fails with an
error about wheel tags that looks nothing like "wrong Python version". uv
fetches its own 3.12, so the machine's Python does not matter at all and that
whole class of setup failure disappears.

Not installing uv is fine too. Then you need Python 3.12 yourself:

```bat
py -3.12 --version
```

## Step 2 — build the environment

```bat
cd LaserImageAlignment
launch.bat
```

`launch.bat` uses uv if it is on `PATH` and falls back to `py -3.12` + a venv +
pip if it is not — you do not choose, and both work. It then opens the GUI.

**You need internet for this step.** The dependencies (PySide6, numpy, OpenCV,
piexif and a few small ones) come from PyPI. Roughly 300 MB on a first run,
once; after that the environment is on disk and startup is instant.

There is an offline path, but this package does not use it: if a `wheels\`
folder is present beside `launch.bat`, the same libraries are installed from
those files with no network at all. It was left out here because it is ~290 MB
of binaries and you have internet. If you ever need the offline route — a
locked-down machine, a field laptop — ask Derek for the `wheels\` folder and
drop it in; `launch.bat` will find it on its own, no flag to set.

On macOS or Linux use `./launch.sh` instead. The GUI works there; the
Windows-only `wheels\` folder is ignored either way and PySide6 comes from
PyPI.

**About `.venv`:** the zip deliberately does not contain one. A virtual
environment stores absolute paths written when it was created
(`F:\Sidewalk\...`, `C:\Users\...`) and cannot be moved between machines. If
you ever do end up with a foreign one — copying this folder to another PC, or
restoring an old backup — `launch.bat` catches it: it asks the venv's own
Python to start, and if it cannot, prints `Existing .venv does not work on
this machine - rebuilding it...`, deletes it, and builds a fresh one. Nothing
for you to do; just do not be alarmed by the message.

If the install fails, run it by hand to see the real error:

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install --no-index --find-links wheels -r requirements.txt
```

## Step 3 — point the app at AllCalibrations ← do not skip this

**Edit `lia.ini`**, sitting beside `app.py`. Two lines matter:

```ini
[paths]
calibrations = C:\wherever\you\unzipped\AllCalibrations
collections  = D:\wherever\the\collections\are
```

`collections` is only where the viewer's **Open** dialog starts and can be left
blank. `calibrations` is not optional in practice: the lever-arm file lives in
that folder and is a **required input**. Point it wrong and every run is refused
with `missing lever_arms` and nothing is written. That refusal is deliberate —
the lever arms are baked into every coordinate the app writes, so processing
without them would produce confidently wrong positions rather than no positions.

The built-in default is an `F:\...` path on the machine this was built on, and
almost certainly does not exist here — `cli.py check --help` prints it. `lia.ini`
is what overrides it, for the GUI and the CLI alike.

Check it took:

```bat
uv run cli.py check --help
```

The last line of the `--calibrations` help prints the folder currently in force
and where that answer came from — `lia.ini`, an environment variable, or the
built-in default. If it still says `F:\...` the INI was not read; check you
edited the one next to `app.py` and that the line is under `[paths]`.

Other ways to set it, in order of precedence (highest first):

| how | when to use it |
|---|---|
| `--calibrations "C:\path"` on the CLI | a one-off run against a different folder |
| `set LIA_CALIBRATIONS=C:\path` | a scheduled job or a script; also `LIA_COLLECTIONS` |
| `lia.ini` | the normal answer — set it once and forget it |

Nothing in that chain can crash the app: a missing, blank, or malformed INI
falls through to the next source.

## Step 4 — prove the install

```bat
uv run pytest -q
```

(or `.venv\Scripts\activate` then `python -m pytest -q` without uv)

Everything should pass. A few tests skip — they are real-data integration
tests needing a sample collection on an `F:` drive that is not here. Skips are
expected; failures are not.

---

## Running it

**Command line — this is the production path:**

```bat
uv run cli.py runs    "D:\collection"
uv run cli.py check   "D:\collection"
uv run cli.py process "D:\collection"
```

(without uv: `.venv\Scripts\python.exe cli.py ...`). No `--calibrations` needed
once `lia.ini` is set — pass it only to override the file for one run.

| command | what it does |
|---|---|
| `runs` | lists what is in the collection and what the app will do with each run |
| `check` | pre-flight: what would be written, what would be skipped, why. **Writes nothing.** |
| `process` | the batch: writes GPS, renames images, writes the reports |

Exit codes: `0` all written, `1` ran but something needs a person, `2` could
not run at all. `--no-images --no-gocator --no-csv` together make a dry run.

**GUI:** `launch.bat`. Open a run root, pick a run, and the image appears
beside its laser profiles. "Process All (write GPS)" runs the same batch
behind a pre-check dialog.

## The calibration files

Exactly two, and nothing else in `AllCalibrations` is read:

| file | required? | used for |
|---|---|---|
| `LaserImageAlignmentLeverArms.md` | **yes** | the lever arms of both cameras and both lasers, plus the lens height. In every written position. |
| `PAVE_*.yaml` (newest; `*Refined*` ignored) | no | viewer only — undistort and the profile-window width. A missing one warns; positions are unaffected. |

The lever-arm file must sit at the **top level** of the calibrations folder
with that exact name. The `PAVE_*.yaml` can be in a subfolder — the app
searches recursively.

### Inside the lever-arm file

You should not need to write this from scratch — it comes in the zip — but you
may need to read it, and Derek edits it. It is plain markdown: a heading, then
numbers under it.

**This document does not state the values, deliberately — open the file and
read it.** They change, and a number copied out of a document has caused real
mistakes here. The shape, with placeholders in place of the numbers:

```markdown
## Rear camera

X  <metres, negative = behind the IMU>
Y  <metres, positive = to the right>
Z  <metres, positive = down>

## Gocator left

X  <...>
Y  <...>
Z  <...>

## Rear

<metres above the pavement>
```

- Metres from the SBG cover target (the IMU origin). **X forward, Y right,
  Z down.**
- A heading **ending in `camera`** is a camera, keyed by its first word, and
  that word must match the image folder name under `Images\<run>\`.
- A heading **starting with `Gocator`** is a laser; `left` / `right` picks the
  side.
- A heading that is **exactly `Rear`, or contains `lens height`**, is the lens
  height above the pavement — a bare number, not X/Y/Z, because it is a height
  above the ground rather than an offset from the IMU. It sets display scale
  only and moves no written position.
- A missing axis line means **0.0**, which is a claim, not a blank: it says
  that sensor sits exactly on the IMU in that direction.
- Units or remarks after a number are fine: `X <value> m`,
  `X <value> (measured 2026-08-31)`.

**Do not hard-code any of these values anywhere, and do not ask what they
should be — read the file.** The app re-reads it every run. If your expectation
and the batch report's `arms:` line disagree, the file wins.

If the batch report carries a `WARNING:` line about this file, it means a number
in it sat under a heading the parser did not recognise and was **not** used.
That is worth stopping for: it once left a value sitting in the file, plainly
visible, while every run quietly used a built-in fallback instead.

## The data it expects

Point the app at a **collection-day folder**:

```
<collection>\
  Images\<YYYYMMDD.HHMMSS>\Rear\*.jpg   and \ROW\*.jpg
  GoCatorData\<YYYYMMDD.HHMMSS>\*L.csv, *R.csv
  SBGData\...\<stamp> DataLogger\eventOutA.txt, utcTime.txt, DmiStationEx *.csv
  SBGData\...\export\ascii-output.txt     <- Qinertia post-processed: THE ruler
  SBGData\...\export\Events-output.txt    <- optional; recovers each run's first image
  <YYYYMMDD>\Daily_ARAN104_<YYYYMMDD>.csv <- ACS section register
```

If an input lives elsewhere, `--locate KEY=PATH` supplies it for every run
missing it, e.g. `--locate nav_export=D:\proc\ascii-output.txt`. The GUI has a
`Locate…` button for the same thing. Without the Daily file the app still
writes GPS but leaves filenames alone — there is no section start to measure
from.

## ⚠ This app writes into the collected data

`process` modifies the collection in place: GPS EXIF inside every JPEG, new
`latitude,longitude,elevation` columns in the Gocator CSVs, and it **renames
every image** to its distance into the section.

It is careful — atomic temp-then-replace writes, idempotent re-runs, and a
`rename_manifest.csv` written beside the images that makes the rename
reversible — but **work on a copy of a collection the first few times.**

Never delete `rename_manifest.csv`. A renamed file's name is a section
distance in the same 12-digit-millimetre form as the odometer name it
replaced, so the two are indistinguishable by eye. That manifest is the only
record of which is which, and the app needs it to re-match the folder.

Every batch writes its account into `<collection>\Processed\`: a readable
report, `issues_*.csv` (every failure and warning), `affected_*.csv` (anything
that did not reach the client), and `run_mapping.csv` (which SBG logger folder
belongs to which ACS run). The report's `arms:` line names the lever-arm file
that was actually used — check it.

---

## Working on the code

| where | what |
|---|---|
| `core\` | parsers, QC, alignment, geotagging, rename, batch. **Never imports Qt** — `tests/test_cli.py` fails if that changes. |
| `gui\` | PySide6 widgets. Conventions in `Spec\gui-coding-standards.md` are mandatory. |
| `tests\` | pytest. `conftest.py` builds a synthetic collection, so most tests need no real data. |
| `cli.py` / `app.py` | headless and GUI entry points |
| `README.md` | the user guide |
| `CONTEXT.md` | **read this before changing anything** — what is authoritative, what to ignore, what has already been measured |
| `Spec\` | the original specification and the phased build plan |

Four things that are easy to get wrong:

- **Images are matched to triggers ordinally**, tail-anchored on the count
  difference — never by fitting distances. The camera counter and the SBG
  distance differ by a small scale factor, so distance cannot identify the
  shift. Distance is a diagnostic only.
- **PTP is the master clock.** Gocator stamps are TAI; UTC ≈ `ptp − 37 s`, and
  the exact offset is solved per run by correlating encoder rate against the
  post-processed speed.
- **Lever arms come from the file, never from the constants.** The constants
  left in `core/calibration.py` are history. Everything that writes reads the
  file, parsed once per run into `parsed.lever_arms`.
- **QC failures are scoped.** A `blocks=` tuple on a check decides which
  deliverables a failure stops. One laser's bad clock holds that laser's GPS
  columns only — the images and the table still go out.

**This app is not under version control.** There is no `.git`, no remote, and
no history — that is deliberate, not an oversight, and it means every edit you
make is permanent and cannot be undone. Copy the folder before you start
changing things. If you want git for your own work, `git init` locally; do not
add a remote on the original author's behalf.

**JPEG metadata:** ACS writes no EXIF at all, so a processed image carries a
GPS block and nothing else. Do not expect `DateTime`, `Make` or a camera ID to
be there — take capture time from the alignment CSV, or from `GPSDateStamp` /
`GPSTimeStamp`.

## If something goes wrong

| symptom | cause |
|---|---|
| `launch.bat` fails immediately | usually the wrong Python; a stale `.venv` from the zip is now rebuilt automatically |
| every run says `missing lever_arms` | Step 3 — the app is not finding `AllCalibrations`. Run `uv run cli.py check --help` to see the path it is actually using and where that came from |
| every run says `missing nav_export` | no Qinertia `ascii-output.txt` under the collection; use `--locate nav_export=...` |
| `runs` lists nothing | not a collection root, or no run has a row in the Daily file |
| a run shows as `excluded` | the ACS Daily file marks it Status X — deliberate, not an error |
| GUI opens but every image is `RAW` | no `PAVE_*.yaml` found; the viewer cannot undistort. Positions unaffected. |
| batch report has a `WARNING:` about the lever-arm file | a number in it is under a heading the parser does not know, so it was NOT used. Fix the heading — see **Inside the lever-arm file**. |
| a written position looks off by ~1 m | check the `arms:` line in the batch report and read the file it names. Do not assume the arm is whatever a document says. |
| pip cannot find the wheels | wrong Python version — the wheels are 3.12 / 64-bit Windows only |
