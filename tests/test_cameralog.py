"""Camera-log pairing (core/cameralog.py): which trigger took photo n."""
from datetime import datetime, timezone

import numpy as np

from core import cameralog as cl

RUN = "20261014.094844"
T0 = datetime(2026, 10, 14, 9, 48, 44, tzinfo=timezone.utc).timestamp()  # PC clock
CLOCK = 14350.0        # utc = PC + CLOCK (2026-10-14 read 14349.783)


def _triggers(n=400, seed=1):
    """Irregular trigger times (speed changes), UTC seconds."""
    rng = np.random.default_rng(seed)
    return T0 + CLOCK + 10.0 + np.cumsum(rng.uniform(0.2, 0.4, n))


def _stamp(t):
    s = datetime.fromtimestamp(t, tz=timezone.utc)
    return s.strftime("%Y-%m-%d %H:%M:%S,") + f"{int(s.microsecond / 1000):03d}"


def _write_logs(folder, trig, k, cams=("Rear",), start_pc=None, clock=True):
    """A VideoLogCXP log with frame lines for photos 100/200/300 taken by
    trigger n+k, and an SBG log tying the PC clock to UTC."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    if start_pc is not None:
        lines.append(f"{_stamp(start_pc)} 0x1 root - VC2_StartAcquisition out\n")
    for cam in cams:
        for n in (100, 200, 300):
            cam_t = 500.0 + (trig[n + k - 1] - trig[0]) + 0.32
            lines.append(
                f"{_stamp(trig[n + k - 1] - CLOCK)} 0x1 root - ProcessingFunction, FrameID: {n}, "
                f"EventTimeStamp: {cam_t:.4f}, MilErr: 0, "
                f"File: D:\\ACS\\Images\\{RUN}\\{cam}\\{n * 750:012d}.jpg\n")
    (folder / "ARAN01-SERVER-VideoLogCXP.log").write_text("".join(lines))
    if clock:
        sbg = []
        for i in range(0, 600, 10):
            pc = T0 + i
            s = datetime.fromtimestamp(pc, tz=timezone.utc)
            sbg.append(s.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(s.microsecond / 1000):03d}"
                       + f" INFO CallbackUtc, TS HWUnix: {int(pc + CLOCK)}\n")
        (folder / "ARAN01-Server-SBG.log").write_text("".join(sbg))
    return folder / "ARAN01-SERVER-VideoLogCXP.log"


def test_fit_finds_the_offset_from_the_frame_times():
    trig = _triggers()
    for k in (-1, 2, 4):
        frames = [(n, 500.0 + trig[n + k - 1] - trig[0]) for n in (100, 200, 300)]
        r = cl.fit_frames(frames, trig)
        assert r.confident and r.k == k


def test_fit_needs_two_frames():
    assert cl.fit_frames([(100, 1.0)], _triggers()) is None


def test_log_is_read_and_picked_for_its_run(tmp_path):
    trig = _triggers()
    p = _write_logs(tmp_path / "20261015" / "Logs" / "ARAN01-SERVER", trig, 4,
                    cams=("Rear", "ROW"))
    assert cl.find_logs(tmp_path) == [p]
    assert cl.pick_log([p], RUN) == p
    assert cl.pick_log([p], "20261014.120000") is None
    r = cl.decide(p, RUN, "Rear", trig, 0.32, 396)
    assert r.confident and r.k == 4 and r.method == "camera log"


def test_start_ready_early_means_startup_frame(tmp_path):
    trig = _triggers(150)            # short run: one frame line at most
    p = _write_logs(tmp_path / "Logs" / "PC", trig, -1, cams=(), start_pc=T0 + 1.5)
    r = cl.decide(p, RUN, "Rear", trig, 0.32, 151)
    assert r.method == "camera start" and r.confident and r.k == -1


def test_start_after_triggers_is_an_estimate(tmp_path):
    trig = _triggers(150)
    trig[4:] += 2.0                                       # a clear gap to call
    ready_pc = trig[3] + 0.32 + 1.0 - CLOCK               # 4 triggers missed
    p = _write_logs(tmp_path / "Logs" / "PC", trig, -1, cams=(), start_pc=ready_pc)
    r = cl.decide(p, RUN, "Rear", trig, 0.32, 147)
    assert r.method == "camera start" and not r.confident and r.k == 3
    assert "ESTIMATED" in r.message


def test_no_clock_falls_back_to_the_assumed_rule(tmp_path):
    trig = _triggers(150)
    p = _write_logs(tmp_path / "Logs" / "PC", trig, -1, cams=(), start_pc=T0 + 1.5,
                    clock=False)
    r = cl.decide(p, RUN, "Rear", trig, 0.32, 151)
    assert r.method == "assumed" and r.k == -1 and not r.confident
    assert "count fits" in r.message


def test_no_log_says_so_and_checks_the_count():
    trig = _triggers(150)
    r = cl.decide(None, RUN, "Rear", trig, 0.32, 160)
    assert r.method == "assumed" and r.k == -1
    assert "no camera log" in r.message and "COUNT DOES NOT FIT" in r.message


def test_matcher_takes_a_negative_lag():
    """photo n <- trigger n+4: photo 1 takes trigger 5, nobody is refused."""
    from types import SimpleNamespace
    from core.alignment import match_images_to_triggers
    n_img, n_trg = 96, 100
    images = SimpleNamespace(dist_m=np.arange(1, n_img + 1) * 0.75,
                             counter_mm=np.arange(1, n_img + 1) * 750.0, n_set_aside=0)
    trig = SimpleNamespace(dist_m=np.arange(n_trg) * 0.75, n_prepended=0)
    m = match_images_to_triggers(images, trig, mode="head", photo_lag=-4)
    assert m.n_matched == n_img
    assert int(m.trigger_for_image[0]) == 4 and int(m.trigger_for_image[-1]) == 99
    assert "4 trigger(s) before photo 1" in m.message


def test_summary_names_the_pairing_not_the_count_gap():
    """094844 used to read 'match k=-5' (photos minus triggers)."""
    from types import SimpleNamespace
    from core.qc import QCReport, Severity, match_summary
    rep = QCReport(run_id=RUN)
    rep.add("align.camera.Rear.pairing", "p", Severity.PASS, "", k=4,
            method="camera log", confident=True)
    rep.add("align.camera.ROW.pairing", "p", Severity.WARN, "", k=2,
            method="camera start", confident=False)
    m = SimpleNamespace(shift_k=-5, n_matched=323)
    s = match_summary(rep, m, 323)
    assert "Rear n+4 (camera log)" in s and "ROW n+2 (estimated)" in s
    assert "k=-5" not in s
    assert match_summary(QCReport(run_id=RUN), m, 323).startswith("match k=-5")


def _trigger_count_check(n_photos, n_trg, k):
    from core import discovery as disc
    from core.formats import ImageSet
    from core.qc import ParsedRun, QCReport, Severity, check_content
    counter = np.arange(1, n_photos + 1, dtype=np.int64) * 750
    im = ImageSet(dir="synthetic", files=[f"{c:012d}.jpg" for c in counter],
                  counter_mm=counter)
    im.n_set_aside = 0
    pr = ParsedRun(run=disc.RunPaths(run_id=RUN, root="synthetic"))
    pr.cameras, pr.images = {"Rear": im}, im
    pr.triggers_sbg_us = np.arange(n_trg, dtype=np.int64)
    rep = QCReport(run_id=RUN)
    rep.add("align.camera.Rear.pairing", "p", Severity.PASS, "", k=k)
    check_content(pr, rep)
    return {c.check_id: c for c in rep.checks}["content.trigger_count"]


def test_trigger_count_is_judged_against_the_pairing():
    from core.qc import Severity
    c = _trigger_count_check(323, 328, 4)          # 094844 Rear
    assert c.severity is Severity.PASS and "1 trigger(s) at the end" in c.message
    assert _trigger_count_check(316, 315, -1).severity is Severity.PASS
    c = _trigger_count_check(330, 328, 4)
    assert c.severity is Severity.WARN and "6 photo(s) more" in c.message
