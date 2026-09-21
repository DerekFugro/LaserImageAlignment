# LaserImageAlignment — please add `--daily`

**For:** whoever (or whatever) works on `F:\Sidewalk\002_App\SidewalkProfilier\002_DataAlignment\LaserImageAlignment`
**From:** the SidewalkPipelineOrchestrator side, 2026-09-18
**Related:** `095_WhatMyAppDoes\PipelineCLIContract.md` — see its "One day at a time" section

---

## Why

Uploads now arrive with several collection days in one folder. Ten days of survey gives
ten `YYYYMMDD` folders, each with its own `Daily_ARAN104_*.csv`, while `Images\`,
`GoCatorData\` and `SBGData\` hold every day's runs together.

The orchestrator runs the whole pipeline once per day, oldest first. Every other app
follows `Processed\Alignment\run_mapping.csv`, which you write — so once you can be told
which day to work on, the rest of the pipeline follows automatically. **You are the only
app that needs a change.**

## The problem, as it stands today

In `core\daily.py`:

```python
def find_daily_file(root):
    """The day folder is named YYYYMMDD; the Daily file sits inside it."""
    hits = sorted(root.glob("*/Daily_ARAN104_*.csv"))
    return hits[0] if hits else None
```

One glob, take the first. With ten day folders, sorted gives the **oldest** — so you read
day one's Daily file and nothing else. `discover_runs(..., registered_only=True)` then
drops every stamp that file does not list, which is all nine other days. They would never
reach `run_mapping.csv`, and so would never be processed by anything downstream.

Nothing warns. It looks like a clean run of a small day.

## What to add

**`--daily PATH`** — the ACS Daily report to use, instead of globbing for one.

Add it to the same place as `--calibrations` and `--overrides`, so it applies to `runs`,
`check` and `process` alike. A `--day YYYYMMDD` alias that resolves to
`<collection>\<day>\Daily_ARAN104_<day>.csv` would be a bonus, not a requirement.

### Rules

1. **With `--daily` given**, use exactly that file. Do not glob, and do not fall back to
   another one if it turns out to be empty.
2. **With `--daily` absent**, behave exactly as today. Running the app by hand, and the
   GUI, must not change at all.
3. **A `--daily` path that does not exist, or cannot be read, is exit 2** with one clear
   line on stderr naming the path. Never carry on with a different day's file.
4. **A day whose runs are not all on disk is normal**, not an error. Rows in the Daily
   file with no folder already come back as `not_found` in `run_mapping.csv`; keep that.
5. **`--help` still exits 0 fast and touches no data.** `orchestrate.bat doctor --probe`
   uses it.

### Where it lands

`find_daily_file(root)` is called from `core\discovery.py` in `_daily_entries(root)`,
which feeds `daily_stamps(root)` and therefore `discover_runs(..., registered_only=True)`.
Whatever shape you prefer — an optional argument threaded through, or a module-level
override set once in `cli.py` before discovery — the requirement is only that
`_daily_entries` ends up reading the file the caller named. Nothing else about discovery,
renaming or the report needs to change. [The call chain above is read from the source; the
best way to thread it through is your call.]

## What the orchestrator will call

Today, unchanged:

```
.venv\Scripts\python.exe cli.py process <collection>
```

Once `--daily` exists, one line changes in the orchestrator's `pipeline.json`:

```
.venv\Scripts\python.exe cli.py process <collection> --daily <collection>\20260821\Daily_ARAN104_20260821.csv
```

...called once per day, oldest first, with the day's own report each time. No code change
on the orchestrator side. Until then it **refuses** a multi-day folder with an explanation
rather than processing day one and silently dropping the rest.

## How I will check it

1. `cli.py process --help` exits 0 and lists `--daily`.
2. A collection with **one** day, no `--daily`: same `run_mapping.csv` as before this
   change, byte for byte.
3. A collection with **three** day folders, `--daily` pointing at the second one:
   `run_mapping.csv` holds that day's runs only, and none from the other two.
4. Same collection, run three times with each day's report in turn: each run's mapping
   holds only that day. (The orchestrator copies each one aside as
   `Processed\_orchestrator\mappings\run_mapping_<day>.csv` before the next day overwrites
   it — a copy, the original stays put.)
5. `--daily` pointing at a file that is not there: exit **2**, one line on stderr saying
   which path.

## Please do not

- Change where `run_mapping.csv` is written, or its columns. Every downstream app reads it.
- Write anything into `Processed\_orchestrator\` — that folder is the orchestrator's.
- Make `--daily` required. Absent must keep working exactly as it does now.

## Afterwards, if you have time

`--json`: one JSON object on stdout at the end, progress to stderr, with at least
`summary` (one readable line) and `problems` (short strings). The orchestrator would then
put your own summary in the review log instead of guessing from your last printed line.
Not needed for the day change — separate job, listed in the contract as rule 9.
