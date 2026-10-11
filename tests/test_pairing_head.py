"""PAIRING_MODE "head" (Derek, 2026-10-11): photo n <-> logged trigger n.

On 2026-10-10 the yardstick, seen by both lasers on six runs, put photo 1 on
trigger 1 every time. Three runs had one photo fewer than triggers because the
cameras stopped while the van was still moving, so the LAST trigger had no
photo; the old tail rule read that as a missing FRONT photo and moved every
photo of 095408 one trigger (750 mm) forward."""
from __future__ import annotations

import numpy as np

from core.alignment import PAIRING_MODE, TriggerData, match_images_to_triggers
from core.formats import ImageSet


def _images(ordinals, n_set_aside=0):
    c = np.asarray([750 * n for n in ordinals], dtype=np.int64)
    return ImageSet(dir="x", files=[f"{v:012d}.jpg" for v in c], counter_mm=c,
                    n_set_aside=n_set_aside)


def _triggers(n):
    d = np.arange(n) * 0.75
    return TriggerData(sbg_us=np.arange(n, dtype=np.int64) * 300_000, utc_s=np.arange(n) * 0.3,
                       dist_m=d, spacing_m=np.diff(d), spacing_ok_frac=1.0, message="")


def test_head_is_the_default():
    assert PAIRING_MODE == "head"


def test_last_photo_lost_photo_n_still_takes_trigger_n():
    # 095408: 76 photos, 77 logged triggers
    m = match_images_to_triggers(_images(range(1, 77)), _triggers(77))
    assert list(m.trigger_for_image[:3]) == [0, 1, 2]
    assert int(m.trigger_for_image[-1]) == 75          # trigger 77 (index 76) has no photo
    assert m.n_unmatched == 0
    assert "after the last photo" in m.message


def test_equal_counts_pair_one_to_one():
    m = match_images_to_triggers(_images(range(1, 63)), _triggers(62))
    assert list(m.trigger_for_image) == list(range(62))


def test_set_aside_photos_do_not_shift_the_rest():
    # photos 1 and 2 already filed into BeforeCollection by an earlier batch
    m = match_images_to_triggers(_images(range(3, 63), n_set_aside=2), _triggers(62))
    assert int(m.trigger_for_image[0]) == 2
    assert int(m.trigger_for_image[-1]) == 61


def test_tail_mode_still_available():
    m = match_images_to_triggers(_images(range(1, 77)), _triggers(77), mode="tail")
    assert int(m.trigger_for_image[0]) == 1            # the old one-trigger shift
