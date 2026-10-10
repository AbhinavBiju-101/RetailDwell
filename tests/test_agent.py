"""Camera agent tests. No pytest needed:  python -m tests.test_agent   (or run with pytest if you have it).

Covers calibration, tracking, the pipeline's privacy and quality rules, the YOLO adapter (with a fake model),
the API sink's retry and spool behaviour, and a full run from synthetic video into the real Flask ingest API.
"""
import gzip
import json
import os
import sys
import tempfile
import types

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")

from agent import synth
from agent.detect import YoloDetector
from agent.floor import CalibrationError, Floor
from agent.pipeline import Pipeline
from agent.sinks import ApiSink, CsvSink, SinkError
from agent.tracker import FloorTracker


class ListSink:
    def __init__(self):
        self.rows, self.count = [], 0

    def add(self, *r):
        self.rows.append(r)
        self.count += 1

    def close(self):
        pass


class FakeDet:
    """Detector that returns scripted floor positions by projecting them back into the image."""
    name, gives_ids = "fake", False

    def __init__(self, floor, script, frame_wh):
        self.floor, self.script, self.wh, self.i = floor, script, frame_wh, 0

    def detect(self, frame):
        pts = self.script[self.i] if self.i < len(self.script) else []
        self.i += 1
        out = []
        for x, y in pts:
            px, py = self.floor.to_image(x, y)[0]
            sx, sy = frame.shape[1] / self.wh[0], frame.shape[0] / self.wh[1]
            px, py = px * sx, py * sy
            out.append((px - 10, py - 40, px + 10, py, 0.9, None))
        return out


def blank(w=960, h=540):
    return np.zeros((h, w, 3), np.uint8)


# ---------------------------------------------------------------- calibration
def test_calibration_recovers_points():
    f = synth.camera()
    assert f.rms_m < 0.01
    back = f.to_floor(*f.to_image(6.0, 4.0)[0])[0]
    assert np.allclose(back, [6.0, 4.0], atol=1e-6)


def test_calibration_rejects_bad_input():
    for img, flo in [([[0, 0], [1, 1], [2, 2]], [[0, 0], [1, 1], [2, 2]]),                    # too few
                     ([[0, 0], [1, 1], [2, 2], [3, 3]], [[0, 0], [1, 0], [2, 0], [3, 0]])]:   # collinear
        try:
            Floor(img, flo)
        except CalibrationError:
            continue
        raise AssertionError("expected CalibrationError")


def test_calibration_flags_a_mismeasured_point():
    f = synth.camera()
    fp = f.floor_pts.copy()
    fp[2] += [1.5, -1.0]
    bad = Floor(f.image_pts, fp, f.frame_size, f.floor_size)
    assert bad.max_m > 0.3 and bad.quality() != "good"


def test_keep_drops_off_floor_and_excluded():
    f = Floor(synth.camera().image_pts, synth.camera().floor_pts, floor_size=(12, 8), exclude=[[0, 0, 2, 2]])
    assert f.keep(6, 4) and not f.keep(30, 4) and not f.keep(1, 1) and f.keep(12.5, 4)


# ---------------------------------------------------------------- tracker
def test_tracker_keeps_ids_for_crossing_walkers():
    n = iter(range(1, 100))
    tr = FloorTracker(new_id=lambda: next(n))
    seen = {"A": set(), "B": set()}
    for k in range(40):
        t = k * 0.125
        a, b = (1 + 1.0 * t, 3.0), (9 - 1.0 * t, 3.4)           # pass each other, 0.4 m apart in y
        for tid, x, y in tr.update(t, [a, b]):
            seen["A" if y < 3.2 else "B"].add(tid)
    assert len(seen["A"]) == 1 and len(seen["B"]) == 1 and seen["A"] != seen["B"]


def test_tracker_never_reuses_ids_and_expires_tracks():
    n = iter(range(1, 100))
    tr = FloorTracker(new_id=lambda: next(n))
    first = tr.update(0.0, [(1, 1)])[0][0]
    tr.update(10.0, [])                                         # long gap: track expires
    second = tr.update(10.5, [(1, 1)])[0][0]
    assert first != second


# ---------------------------------------------------------------- pipeline
def run_script(script, **kw):
    f = synth.camera()
    sink = ListSink()
    pipe = Pipeline(f, FakeDet(f, script, synth.FRAME), sink, id_base=1000, **kw)
    for i in range(len(script)):
        pipe.process(blank(), i * 0.125)
    return sink.rows, pipe


def test_pipeline_drops_flicker_and_keeps_real_tracks():
    real = [[(5 + 0.01 * i, 4.0)] for i in range(80)]                     # 10 s of one person
    ghost = [[(5 + 0.01 * i, 4.0), (9.0, 6.0)] if 10 <= i < 14 else [(5 + 0.01 * i, 4.0)] for i in range(80)]
    rows, _ = run_script(ghost)
    ids = {r[0] for r in rows}
    assert len(ids) == 1                                                 # the 0.5 s ghost never reached the sink
    rows, _ = run_script(real)
    assert 18 <= len(rows) <= 21                                         # ~2 Hz for 10 s


def test_pipeline_thins_to_store_rate_and_orders_time():
    rows, _ = run_script([[(5, 4)] for _ in range(160)])                  # 20 s at 8 fps
    t = [r[1] for r in rows]
    assert t == sorted(t)
    gaps = np.diff(t)
    assert gaps.min() >= 0.5 - 1e-6


def test_pipeline_never_emits_excluded_or_off_floor_points():
    f = synth.camera()
    f = Floor(f.image_pts, f.floor_pts, f.frame_size, (12, 8), exclude=[[0, 0, 3, 3]])
    script = [[(1.5, 1.5), (6, 4)] for _ in range(80)]
    sink = ListSink()
    pipe = Pipeline(f, FakeDet(f, script, synth.FRAME), sink, id_base=0)
    for i in range(80):
        pipe.process(blank(), i * 0.125)
    assert sink.rows and all(not (r[2] < 3 and r[3] < 3) for r in sink.rows)
    assert pipe.dropped_off_floor == 80


def test_pipeline_handles_other_resolutions():
    f = synth.camera()                                                    # calibrated at 960x540
    sink = ListSink()
    script = [[(6.0, 4.0)] for _ in range(60)]
    pipe = Pipeline(f, FakeDet(f, script, synth.FRAME), sink, id_base=0)
    for i in range(60):
        pipe.process(blank(1920, 1080), i * 0.125)                        # camera now streams 1080p
    xs = np.array([[r[2], r[3]] for r in sink.rows])
    assert np.allclose(xs.mean(axis=0), [6.0, 4.0], atol=0.15)


def test_ids_are_random_per_run():
    a = Pipeline(synth.camera(), FakeDet(synth.camera(), [], synth.FRAME), ListSink())
    b = Pipeline(synth.camera(), FakeDet(synth.camera(), [], synth.FRAME), ListSink())
    assert a.id_base != b.id_base


def test_csv_sink_writes_only_four_columns():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "o.csv.gz")
        s = CsvSink(p)
        s.add(5, 1.234, 2.345, 3.456)
        s.close()
        df = pd.read_csv(p)
        assert list(df.columns) == ["track_id", "t", "x", "y"] and df.t[0] == 1.23


# ---------------------------------------------------------------- YOLO adapter (fake model)
class _T:                                   # torch-like tensor
    def __init__(self, a): self.a = np.asarray(a)
    def cpu(self): return self
    def numpy(self): return self.a
    def __len__(self): return len(self.a)


def test_yolo_adapter_reads_boxes_and_ids():
    boxes = types.SimpleNamespace(xyxy=_T([[10, 20, 50, 120]]), conf=_T([0.8]), id=_T([7.0]))
    model = types.SimpleNamespace(track=lambda *a, **k: [types.SimpleNamespace(boxes=boxes)])
    det = YoloDetector(_model=model)
    (x1, y1, x2, y2, c, tid), = det.detect(blank())
    assert (x1, y2, tid) == (10.0, 120.0, 7) and abs(c - 0.8) < 1e-6
    empty = types.SimpleNamespace(boxes=types.SimpleNamespace(xyxy=_T(np.zeros((0, 4))), conf=_T([]), id=None))
    assert YoloDetector(_model=types.SimpleNamespace(track=lambda *a, **k: [empty])).detect(blank()) == []


def test_pipeline_uses_yolo_ids_with_run_prefix():
    f = synth.camera()
    px, py = f.to_image(6, 4)[0]

    class D:
        name, gives_ids = "yolo", True
        def detect(self, frame): return [(px - 10, py - 40, px + 10, py, 0.9, 3)]
    sink = ListSink()
    pipe = Pipeline(f, D(), sink, id_base=5000)
    for i in range(40):
        pipe.process(blank(), i * 0.125)
    assert {r[0] for r in sink.rows} == {5003}


# ---------------------------------------------------------------- API sink
class Resp:
    def __init__(self, code, body=None): self.status_code, self._b = code, body or {}
    def json(self): return self._b


class Scripted:
    def __init__(self, codes): self.codes, self.calls = list(codes), []
    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        c = self.codes.pop(0) if self.codes else 202
        return Resp(c, {"error": "nope"} if c >= 400 else {})


def sink_with(session, **kw):
    return ApiSink("https://x.example", "dtm_key", "A", "Live", session=session, sleep=lambda s: None, **kw)


def test_api_sink_batches_under_server_limit_and_sets_auth():
    ses = Scripted([])
    s = sink_with(ses, batch=99999, flush_s=1e9)
    for i in range(12000):
        s.add(1, i * 0.5, 1.0, 1.0)
    s.close()
    sizes = [len(c[1]["points"]) for c in ses.calls]
    assert max(sizes) <= 5000 and sum(sizes) == 12000
    assert ses.calls[0][0].endswith("/api/ingest/v1/tracks") and ses.calls[0][2]["Authorization"] == "Bearer dtm_key"
    assert set(ses.calls[0][1]["points"][0]) == {"track_id", "t", "x", "y"}


def test_api_sink_retries_server_errors_then_succeeds():
    ses = Scripted([500, 503, 202])
    s = sink_with(ses, flush_s=1e9)
    s.add(1, 0.0, 1.0, 1.0)
    s.close()
    assert s.sent == 1 and len(ses.calls) == 3 and s.spooled == 0


def test_api_sink_fails_loudly_on_bad_key_and_missing_layout():
    for code in (401, 404, 400):
        s = sink_with(Scripted([code]), flush_s=1e9)
        s.add(1, 0.0, 1.0, 1.0)
        try:
            s.close()
        except SinkError:
            continue
        raise AssertionError(f"expected SinkError for {code}")


def test_api_sink_spools_offline_and_resends_next_run():
    with tempfile.TemporaryDirectory() as d:
        s = sink_with(Scripted([500] * 5), flush_s=1e9, spool_dir=d, retries=4)
        s.add(1, 0.0, 1.0, 1.0)
        s.close()
        files = os.listdir(d)
        assert len(files) == 1 and s.sent == 0
        saved = json.load(gzip.open(os.path.join(d, files[0]), "rt"))
        assert list(saved["points"][0]) == ["track_id", "t", "x", "y"]
        ses = Scripted([])
        sink_with(ses, spool_dir=d)                                       # next run: resends on start
        assert len(ses.calls) == 1 and os.listdir(d) == []


# ---------------------------------------------------------------- end to end into the real Flask ingest API
class ClientSession:
    """Lets ApiSink talk to the Flask test client the way `requests` would."""
    def __init__(self, client): self.c = client
    def post(self, url, json=None, headers=None, timeout=None):
        path = "/" + url.split("/", 3)[3]
        r = self.c.post(path, json=json, headers=headers)
        return Resp(r.status_code, r.get_json(silent=True))


def test_end_to_end_video_to_dashboard_data():
    from agent.detect import MotionDetector
    from agent.pipeline import run_source
    import analytics as an
    from app import create_app
    from repo_local import LocalRepo
    with tempfile.TemporaryDirectory() as d:
        repo = LocalRepo(os.path.join(d, "local"))
        sess = repo.sign_up("owner@test.example", "password-1", "Owner")["session"]
        store = repo.create_store(sess, dict(name="Synth", width_m=12.0, height_m=8.0))
        zones = {"Entrance": [0, 0, 4, 2, "entrance"], "Checkout": [10, 0, 12, 2.5, "checkout"],
                 "Left wall": [1.5, 4.5, 4.5, 7.5, "merchandise"], "Mid wall": [5, 5.5, 7.5, 7.5, "merchandise"],
                 "Right wall": [8, 4.5, 10.5, 7, "merchandise"], "Front": [3, 2.2, 9.5, 4, "merchandise"]}
        repo.upsert_layout(sess, store["id"], "A", "Synthetic", zones)
        _rec, key = repo.create_api_key(sess, store["id"], "test agent")
        client = create_app(repo=repo).test_client()

        video = os.path.join(d, "scene.mp4")
        floor, gt = synth.render(video, seconds=90, n_people=10, seed=3)
        sink = ApiSink("http://app.test", key, "A", "Live test", session=ClientSession(client), flush_s=1e9)
        run_source(video, Pipeline(floor, MotionDetector(), sink), track_fps=8.0)

        assert sink.sent == sink.count > 300 and sink.spooled == 0
        layout = repo.svc_layout_by_key(store["id"], "A")
        ds = repo.svc_get_or_create_dataset(layout["id"], "Live test", "ingest")
        df = repo.load_tracks(ds, repo.svc_get_store(store["id"]))
        assert len(df) == sink.count and df.track_id.nunique() >= 8
        res = an.analyse_layout(df, {n: list(z) for n, z in zones.items()}, 12.0, 8.0, 300)
        assert res is not None
        m = synth.evaluate(gt, df)
        assert m["median_err_m"] < 0.25 and m["precision"] > 0.9 and m["recall"] > 0.55, m


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL  {name}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
