# Missing images mode - plan

Draft 2026-11-10, updated the same day with Derek's answers and a test on
the two runs. Nothing here is built yet.

## Short answer: can we geotag them properly?

Yes, for the photos that exist. A photo gets its GPS from the camera pulse
that took it, so the job is to find the right pulse for every photo.

- **20261031.101107:** after cleaning the pulse list (below), every
  camera-log check point fits, so every photo can be tagged as well as on a
  normal run.
- **20261101.093228:** after cleaning, 6 pulses still have no photo, in two
  known 100-photo stretches. Everything outside those two stretches is
  certain. Inside them, the photos are placed by photo overlap, or estimated.

The missing photos themselves can't be recovered or tagged. The camera never
took them.

## What a loose encoder power wire does

Found on 20261031.101107 and 20261101.093228.

1. **Hole:** no pulses while the power is off, so no photos. 101107 has
   holes of 6.7 m, 6.9 m and 17.5 m, plus some 1-2 m ones.
2. **Pulses too close together:** some only 5 ms apart, and in 093228 also
   81 that are 0.05-0.13 s apart. The camera can't take a photo that fast,
   so these pulses get no photo.
3. **Bad time:** about once per power drop, a pulse is stamped 4-10 s earlier
   than the one before it. It took no photo.
4. **ACS reports it wrong:** it names photos by counting them, so its
   QC_Video.csv puts the shortfall at the end of the run. The count is about
   right, but the place is wrong.
5. **The lasers likely have holes too.** They run on the same encoder.
6. **A few duplicate photos:** zero movement between them. There are 3 in
   101107 and 8 in 093228.

## Test of the cleaning (read-only, 2026-11-10)

Cleaning means: drop pulses whose time goes backwards, then apply the
existing 0.13 s "too soon for the camera" rule.

| Run | Pulses | After cleaning | Photos | Camera-log check points |
|---|---|---|---|---|
| 101107 | 1370 | 1365 | 1366 (= 1365 + start-up frame) | all 13 fit, both cameras, error 0-8 ms |
| 093228 | 3041 | 2978 | 2972 Rear / 2973 ROW | fit to photo 600; 5 extra pulses between photos 600 and 700; 1 more between 1100 and 1200; the rest fit, error 0-17 ms |

So the camera log (one line every 100th photo) shows exactly which stretch
lost a photo. That is the key to doing this properly.

## When the mode turns on

Automatically, per run, when the pulse file shows any of these:

- a pulse time that goes backwards
- a hole: no pulse for more than 1.5 steps while the van is moving
- the camera-log check points don't fit one pairing for the whole run

Runs with none of these are processed exactly as now.

## Steps

1. **Clean the pulses.** Drop backwards-time pulses, then apply the 0.13 s
   rule.
2. **Check every camera-log line** (every 100th photo) against the cleaned
   pulses. Stretches where the pairing holds are confirmed.
3. **Settle each stretch where the pairing jumps** (093228: photos 600-700
   and 1100-1200).
   - Find the pulses that got no photo with photo overlap. A missing photo
     shows as a double-length move between two photos.
   - If that is unclear, place the photos in that stretch between the
     confirmed ends, by photo order. Mark them "estimated".
4. **Duplicates** (zero movement): move them to a `Duplicates/` subfolder,
   like `BeforeCollection/`, keep them in the counts, and give them the same
   GPS as the photo they repeat.
5. **Geotag.**
   - Confirmed photos: the normal way (Qinertia export, trigger delay, lever
     arm, ROW view distance).
   - Estimated photos: GPS is written, and they are marked "estimated" in the
     table.
6. **Report.**
   - `MissingPhotos_<run>.csv`, one row per hole: the photos on either side,
     the time (UTC), the length, about how many photos are missing, and the
     GPS at start and end.
   - A `photo_status` column in the alignment table: confirmed, estimated or
     duplicate.
   - The run is marked `partial` in run_mapping.csv, reason first, for
     example: "missing images: 3 holes, 35 m, about 49 photos".
   - Laser holes in metres, from the existing coverage check.
   - No re-collect advice. We do the best we can with what was collected.

## Derek's decisions (2026-11-10)

1. Duplicates go to a subfolder.
2. Estimated GPS is written to the photos.
3. No re-collect threshold. Do the best we can.
4. Qinertia exports for Oct 31 and Nov 1 are being made. Sessions
   20261031.095809_0001 (likely holds 101107) and 20261101.092924_0001
   (holds 093228) were done on 2026-11-10.

## Testing

1. 101107: all photos confirmed. Every hole in MissingPhotos matches a
   no-overlap pair of photos.
2. 093228: the 6 extra pulses are found and the counts close.
3. No change on normal runs: PT_110 (29 runs) and the 202 older runs must
   come out identical.

## In the field

The collection-checker app could flag backwards pulse times and holes the
same day, so a loose wire is found before a whole day is collected.
