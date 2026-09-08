<#
.SYNOPSIS
  Stage everything someone needs to run LaserImageAlignment on another machine.

.WHY
  Two folders feed this app, and only a sliver of the second one matters:

    LaserImageAlignment\   the app itself, minus the venv and the offline wheels
    AllCalibrations\       3.1 GB on our machine, of which the app reads TWO
                           files: the lever-arm file and one PAVE_*.yaml

  Copying AllCalibrations wholesale would send 3.1 GB of raw intrinsic images
  that nothing reads. Copying it by hand risks missing the lever-arm file,
  which since 2026-09-02 is REQUIRED - without it every run is skipped.

.EXAMPLE
  .\Make-SlackPackage.ps1

.EXAMPLE
  .\Make-SlackPackage.ps1 -Dest "C:\Temp\ToSlack" -IncludeWheels
#>
[CmdletBinding()]
param(
    # the app folder; empty means "this script's parent", resolved below.
    # NOT a param default: $PSScriptRoot is not reliably populated while the
    # param block is being bound, and an empty one made Split-Path throw.
    [string]$AppDir       = "",
    # Moved 2026-09-08. The old 002_App\AllCalibrations still exists but now
    # holds only raw intrinsics, backups and ReverseRunProcessor's own files -
    # pointing at it would stage a package with no lever-arm file, which fails
    # this script's own check rather than failing quietly on the far end.
    [string]$Calibrations = "F:\Sidewalk\002_App\SidewalkProfilier\100_AllCalibrations",
    [string]$Dest         = "F:\Sidewalk\002_App\ToSlack",

    # 294 MB of Windows/py3.12 wheels so launch.bat can build the venv with no
    # internet. Off by default: launch.bat falls back to PyPI on its own.
    [switch]$IncludeWheels,

    # capture_times_*.csv - an irreplaceable record of original file times, but
    # nothing the app reads. Off by default to keep the zip small.
    [switch]$IncludeCaptureTimes
)

$ErrorActionPreference = "Stop"

if (-not $AppDir) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $AppDir = Split-Path -Parent $here
}
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

if (-not (Test-Path $AppDir))       { Fail "app folder not found: $AppDir" }
if (-not (Test-Path $Calibrations)) { Fail "calibrations folder not found: $Calibrations" }
if (-not (Test-Path (Join-Path $AppDir "app.py"))) {
    Fail "$AppDir does not look like the app (no app.py)"
}

# ---------------------------------------------------------------- clean target
Step "Staging into $Dest"
if (Test-Path $Dest) {
    Write-Host "  clearing the previous package"
    Remove-Item $Dest -Recurse -Force
}
$appDest = Join-Path $Dest "LaserImageAlignment"
$calDest = Join-Path $Dest "AllCalibrations"
New-Item -ItemType Directory -Path $appDest -Force | Out-Null
New-Item -ItemType Directory -Path $calDest -Force | Out-Null

# ------------------------------------------------------------------- the app
# /XD takes folder NAMES, so nested __pycache__ folders are caught too.
Step "Copying the app"
$excludeDirs = @(".venv", ".git", "__pycache__", ".pytest_cache", "_to_delete")
if (-not $IncludeWheels) { $excludeDirs += "wheels" }
$excludeFiles = @("*.pyc", "*.tmp", "*.__renaming__")
if (-not $IncludeCaptureTimes) { $excludeFiles += "capture_times_*.csv" }

$null = robocopy $AppDir $appDest /E /NFL /NDL /NJH /NJS /NP `
        /XD $excludeDirs /XF $excludeFiles
# robocopy: 0-7 = success (8+ = real failure). It does NOT follow the usual
# convention, so this cannot be a plain $LASTEXITCODE -ne 0 check.
if ($LASTEXITCODE -ge 8) { Fail "robocopy failed with exit code $LASTEXITCODE" }

if (-not $IncludeWheels) {
    Write-Host "  wheels\ skipped - launch.bat installs from PyPI instead"
}
if (-not $IncludeCaptureTimes) {
    Write-Host "  capture_times_*.csv skipped - not read by the app"
}

# --------------------------------------------------------- the calibrations
# EXACTLY what core/calibration.py and core/discovery.py look for. If this
# list ever grows, it grows here too.
Step "Copying the calibration files the app actually reads"

# 1. the lever-arm file: exact name, top level, REQUIRED
$leverName = "LaserImageAlignmentLeverArms.md"
$lever = Join-Path $Calibrations $leverName
if (-not (Test-Path $lever)) {
    Fail "$leverName not found in $Calibrations. The app cannot process anything without it."
}
Copy-Item $lever (Join-Path $calDest $leverName)
Write-Host "  $leverName"

# 2. the Pave intrinsics: newest PAVE_*.yaml, *Refined* ignored (bar-run tool
#    output, seen with a 6x-wrong focal length). Backup trees are skipped so
#    the package can never contain two candidates and pick the other one.
$pave = Get-ChildItem $Calibrations -Recurse -Filter "PAVE_*.yaml" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch "(?i)refined" -and $_.FullName -notmatch "(?i)\\BCK" } |
        Sort-Object LastWriteTime -Descending
if (-not $pave) { Fail "no PAVE_*.yaml found under $Calibrations" }
if ($pave.Count -gt 1) {
    Write-Host "  NOTE: $($pave.Count) PAVE_*.yaml found; taking the newest:" -ForegroundColor Yellow
    $pave | ForEach-Object { Write-Host "        $($_.LastWriteTime.ToString('yyyy-MM-dd'))  $($_.FullName)" }
}
$chosen = $pave[0]
# keep its parent folder name, so the path looks like it does here
$paveDir = Join-Path $calDest $chosen.Directory.Name
New-Item -ItemType Directory -Path $paveDir -Force | Out-Null
Copy-Item $chosen.FullName (Join-Path $paveDir $chosen.Name)
Write-Host "  $($chosen.Directory.Name)\$($chosen.Name)"

# ------------------------------------------------------------- instructions
Step "Adding INSTRUCTIONS.md"
$src = Join-Path $scriptDir "PACKAGE-INSTRUCTIONS.md"
if (Test-Path $src) {
    Copy-Item $src (Join-Path $Dest "INSTRUCTIONS.md")
    Write-Host "  INSTRUCTIONS.md"
} else {
    Write-Host "  WARNING: $src is missing - the package has no instructions" -ForegroundColor Yellow
}

# ------------------------------------------------------------------ verify
Step "Checking the package"
$problems = @()
foreach ($must in @(
    "LaserImageAlignment\app.py",
    "LaserImageAlignment\cli.py",
    "LaserImageAlignment\launch.bat",
    "LaserImageAlignment\requirements.txt",
    # the uv path and the per-installation paths: without lia.ini the app
    # falls back to the F: constant, which is exactly the failure this
    # package exists to avoid
    "LaserImageAlignment\pyproject.toml",
    "LaserImageAlignment\lia.ini",
    "LaserImageAlignment\core\calibration.py",
    "LaserImageAlignment\gui\main_window.py",
    "LaserImageAlignment\README.md",
    "AllCalibrations\$leverName",
    "INSTRUCTIONS.md"
)) {
    if (-not (Test-Path (Join-Path $Dest $must))) { $problems += "missing: $must" }
}
# nothing heavy should have slipped in
foreach ($never in @("LaserImageAlignment\.venv", "LaserImageAlignment\.git")) {
    if (Test-Path (Join-Path $Dest $never)) { $problems += "should not be here: $never" }
}
# the lever-arm file has to have arms in it, or the app refuses every run
$arms = (Get-Content (Join-Path $calDest $leverName) -Raw)
foreach ($section in @("Rear camera", "Gocator left", "Gocator right")) {
    if ($arms -notmatch [regex]::Escape($section)) {
        $problems += "lever-arm file has no '$section' section"
    }
}

$files = Get-ChildItem $Dest -Recurse -File
$mb    = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)

Write-Host ""
if ($problems) {
    $problems | ForEach-Object { Write-Host "  ! $_" -ForegroundColor Red }
    Write-Host ""
    Fail "the package is not complete - see above"
}

Write-Host "  $($files.Count) files, $mb MB" -ForegroundColor Green
Write-Host "  $Dest"
Write-Host ""
Write-Host "Next: zip $Dest and send it. The recipient starts with INSTRUCTIONS.md." -ForegroundColor Green
