"""Write a file so that a killed batch never leaves half of it behind.

Write to a temporary name beside the target, flush, fsync, then os.replace()
onto the real name. os.replace is atomic on one volume, so a reader sees
either the old file or the new one - never a truncated file that looks
complete. That is what the JPEG and Gocator writers always did (geotag.py);
until 2026-09-23 the files OTHER processes depend on did not:

  * rename_manifest.csv - cut short, the folder can no longer be matched
    (ImageSet.scan reads distances as odometer counters) or undone;
  * run_mapping.csv - the gate every downstream app reads;
  * <run>_alignment.csv - one row per image, the table other tools consume;
  * the batch report, issues and affected files.

PipelineCLIContract.md rule 12 asks for exactly this.

The temporary suffix is deliberately NOT ".tmp" or rename.TEMP_SUFFIX, so no
cleanup or recovery pass mistakes it for something it owns.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

WRITING_SUFFIX = ".lia-writing"


@contextmanager
def atomic_open(path, newline: str = "", encoding: str = "utf-8"):
    """`with atomic_open(p) as fh:` - text mode, replaced onto `p` only if
    the block finishes without raising. On any error the target is left as
    it was and the temporary file is removed."""
    path = Path(path)
    tmp = path.with_name(path.name + WRITING_SUFFIX)
    try:
        with open(tmp, "w", newline=newline, encoding=encoding) as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    with atomic_open(path, newline="", encoding=encoding) as fh:
        fh.write(text)
