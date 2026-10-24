"""A trigger too soon for the camera gets no photo (Derek, 2026-10-24).

20261017.154422: the SBG's first two pulses came 0.111 s apart; the camera
made one picture for the two, and counting photos against every trigger put
photos 3.. one trigger (0.75 m) early. The camera took a pulse 0.145 s after
the one before (20261014.173812), so the limit sits between - 0.13 s."""
from __future__ import annotations

import numpy as np

from core.alignment import (CAMERA_MIN_TRIGGER_GAP_S, TriggerData,
                            camera_trigger_indices, match_images_to_triggers)
from core.formats import ImageSet


def _images(ordinals):
    c = np.asarray([750 * n for n in ordinals], dtype=np.int64)
    return ImageSet(dir="x", files=[f"{v:012d}.jpg" for v in c], counter_mm=c)


def _triggers(t):
    t = np.asarray(t, dtype=np.float64)
    return TriggerData(sbg_us=np.arange(len(t), dtype=np.int64), utc_s=t,
                       dist_m=t * 1.5, spacing_m=np.diff(t * 1.5),
                       spacing_ok_frac=1.0, message="")


# 154422's start: 0.111 s, then normal ~0.5 s gaps
T154422 = np.concatenate([[0.0, 0.111], 0.111 + np.cumsum(np.full(20, 0.5))])


def test_limit_sits_between_the_miss_and_the_catch():
    assert 0.111 < CAMERA_MIN_TRIGGER_GAP_S < 0.145


def test_154422_second_trigger_is_passed_over():
    usable, skipped = camera_trigger_indices(T154422)
    assert [i for i, _ in skipped] == [1]
    assert abs(skipped[0][1] - 0.111) < 1e-9
    assert list(usable[:3]) == [0, 2, 3]


def test_photos_after_the_skip_move_one_trigger_later():
    n_photos = len(T154422)              # start-up frame + one per usable trigger
    imgs, trg = _images(range(1, n_photos + 1)), _triggers(T154422)
    usable, _ = camera_trigger_indices(T154422)
    old = match_images_to_triggers(imgs, trg, mode="head", photo_lag=1)
    new = match_images_to_triggers(imgs, trg, mode="head", photo_lag=1, usable=usable)
    # photo 1 start-up frame, photo 2 = trigger 1 either way
    assert old.trigger_for_image[0] == new.trigger_for_image[0] == -1
    assert old.trigger_for_image[1] == new.trigger_for_image[1] == 0
    # from photo 3 on: one trigger later than the old count
    assert list(new.trigger_for_image[2:6]) == [2, 3, 4, 5]
    assert list(old.trigger_for_image[2:6]) == [1, 2, 3, 4]
    # no trigger is given to two photos, the skipped one to none
    tf = new.trigger_for_image[new.trigger_for_image >= 0]
    assert len(set(tf.tolist())) == len(tf) and 1 not in tf


def test_normal_runs_unchanged():
    # fast driving: every gap 0.145 s, as close as any normal run came
    t = np.arange(40) * 0.145
    usable, skipped = camera_trigger_indices(t)
    assert skipped == [] and list(usable) == list(range(40))
    imgs, trg = _images(range(1, 41)), _triggers(t)
    a = match_images_to_triggers(imgs, trg, mode="head", photo_lag=1)
    b = match_images_to_triggers(imgs, trg, mode="head", photo_lag=1, usable=usable)
    assert list(a.trigger_for_image) == list(b.trigger_for_image)


def test_gap_is_measured_from_the_last_photographed_trigger():
    # 0, 0.08 (skipped), 0.16: 0.16 s after trigger 0, so it IS photographed
    usable, skipped = camera_trigger_indices([0.0, 0.08, 0.16, 0.66])
    assert list(usable) == [0, 2, 3] and [i for i, _ in skipped] == [1]
