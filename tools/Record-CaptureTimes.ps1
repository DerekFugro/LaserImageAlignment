<#
.SYNOPSIS
  Record file creation/write times for a collection, before anything rewrites them.

.WHY
  The image write time is the only signal that can VERIFY which trigger an image
  belongs to, rather than assuming it from a count difference. It proves the
  buffer frame at the start of a run (7.1 s gap against a 0.79 s cadence, with no
  matching pulse in eventOutA), and it is the only thing that can catch an image
  dropped MID-run -- which is the case that silently breaks ordinal matching,
  because a leading buffer frame (+1) and a mid-run drop (-1) cancel out.

  These timestamps exist ONLY on the original recording drive. Copying a
  collection usually loses them, and LaserImageAlignment's geotagging rewrites
  every JPEG in place (temp file + rename), which replaces them for good.

  So: run this on \\ARAN104-SERVER\Removable1 (or whatever the original drive
  is), BEFORE copying and BEFORE the batch. It only reads.

.EXAMPLE
  .\Record-CaptureTimes.ps1 -Root "\\ARAN104-SERVER\Removable1"

.EXAMPLE
  .\Record-CaptureTimes.ps1 -Root "\\ARAN104-SERVER\Removable1" `
                            -Out "F:\Sidewalk\capture_times_20260817.csv"

.NOTES
  Times are UTC, millisecond precision, so they line up with eventOutA/utcTime
  without a timezone argument. Nothing is written inside -Root.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [string]$Out = "capture_times.csv"
)

$ErrorActionPreference = 'Stop'
$FMT = "yyyy-MM-ddTHH:mm:ss.fffZ"

function Add-Row {
    param($List, $RunId, $Stream, $FileInfo)
    $List.Add(('{0},{1},{2},{3},{4},{5}' -f
        $RunId,
        $Stream,
        $FileInfo.Name,
        $FileInfo.CreationTimeUtc.ToString($FMT),
        $FileInfo.LastWriteTimeUtc.ToString($FMT),
        $FileInfo.Length)) | Out-Null
}

if (-not (Test-Path -LiteralPath $Root)) {
    throw "Root not found or not reachable: $Root"
}

$rows = New-Object System.Collections.Generic.List[string]
$rows.Add('run_id,stream,file,create_utc,write_utc,bytes') | Out-Null

# ---- images: Images\<run>\<camera>\*.jpg -----------------------------------
$imagesDir = Join-Path $Root 'Images'
if (Test-Path -LiteralPath $imagesDir) {
    foreach ($run in Get-ChildItem -LiteralPath $imagesDir -Directory) {
        foreach ($cam in Get-ChildItem -LiteralPath $run.FullName -Directory) {
            $files = Get-ChildItem -LiteralPath $cam.FullName -Filter *.jpg -File |
                     Sort-Object Name
            foreach ($f in $files) { Add-Row $rows $run.Name $cam.Name $f }
            Write-Host ("  {0}/{1}: {2} images" -f $run.Name, $cam.Name, $files.Count)
        }
    }
} else {
    Write-Warning "no Images\ folder under $Root"
}

# ---- gocator CSVs: GoCatorData\<run>\*.csv ---------------------------------
$gocDir = Join-Path $Root 'GoCatorData'
if (Test-Path -LiteralPath $gocDir) {
    foreach ($run in Get-ChildItem -LiteralPath $gocDir -Directory) {
        foreach ($f in Get-ChildItem -LiteralPath $run.FullName -Filter *.csv -File) {
            Add-Row $rows $run.Name 'gocator' $f
        }
    }
}

Set-Content -LiteralPath $Out -Value $rows -Encoding UTF8
Write-Host ""
Write-Host ("wrote {0} rows to {1}" -f ($rows.Count - 1), (Resolve-Path $Out))
Write-Host "Keep this file with the collection - it cannot be reconstructed later."
