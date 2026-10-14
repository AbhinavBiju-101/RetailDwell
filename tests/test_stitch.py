"""Track stitching in the agent pipeline. Run:  python -m tests.test_stitch"""
import os

os.environ.setdefault("AUTO_SEED", "0")
from types import SimpleNamespace

from agent.pipeline import Pipeline


class ListSink:
    def __init__(self):
        self.rows, self.count = [], 0

    def add(self, tid, t, x, y):
        self.rows.append((tid, t, x, y))
        self.count += 1

    def close(self):
        pass


def pipe(**kw):
    floor = SimpleNamespace(floor_size=(12.0, 8.0))
    p = Pipeline(floor, SimpleNamespace(gives_ids=True), ListSink(), id_base=0, min_track_s=1.0, **kw)
    return p


def feed(p, raw, t0, t1, x, y, dx=0.0, dy=0.0, hz=2.0):
    t = t0
    while t <= t1 + 1e-9:
        p._observe(p._resolve(raw, t, x + dx * (t - t0), y + dy * (t - t0)), t, x + dx * (t - t0), y + dy * (t - t0))
        t += 1 / hz


def ids(p):
    return sorted({r[0] for r in p.sink.rows})


def test_a_person_who_vanishes_and_reappears_nearby_keeps_one_identity():
    p = pipe()
    feed(p, 1, 0, 10, 5.0, 4.0, 0.2, 0.0)           # walks from x=5 to x=7, then is lost
    feed(p, 2, 14, 24, 7.3, 4.0, 0.2, 0.0)          # a new detector id appears 4 s later, close to where they were
    assert ids(p) == [1] and p.stitched == 1
    assert max(r[1] for r in p.sink.rows) >= 24     # and the later points are stored under the first id


def test_a_different_person_far_away_is_not_stitched():
    p = pipe()
    feed(p, 1, 0, 10, 5.0, 4.0)
    feed(p, 2, 14, 24, 10.0, 6.5)                   # 5 m away, 4 s later: faster than anyone walks
    assert ids(p) == [1, 2] and p.stitched == 0


def test_someone_leaving_by_the_door_is_not_joined_to_the_next_person_who_enters():
    p = pipe()
    feed(p, 1, 0, 10, 5.0, 0.4, 0.0, -0.01)         # last seen at the edge of the floor (y about 0.3): they left
    feed(p, 2, 13, 23, 5.0, 0.5)
    assert ids(p) == [1, 2] and p.stitched == 0


def test_a_gap_that_is_too_long_is_not_bridged_and_two_people_are_never_merged_at_once():
    p = pipe()
    feed(p, 1, 0, 10, 5.0, 4.0)
    feed(p, 2, 25, 35, 5.2, 4.0)                    # 15 s later: beyond the 8 s limit
    assert ids(p) == [1, 2]
    q = pipe()
    feed(q, 1, 0, 10, 5.0, 4.0)
    feed(q, 2, 0, 10, 5.4, 4.0)                     # two people side by side at the same time
    assert ids(q) == [1, 2] and q.stitched == 0


def test_an_id_that_was_taken_over_and_comes_back_becomes_its_own_track():
    p = pipe()
    feed(p, 1, 0, 10, 5.0, 4.0)
    feed(p, 2, 13, 20, 5.2, 4.0)                    # stitched onto 1
    feed(p, 1, 21, 28, 8.0, 5.0)                    # the original detector id returns elsewhere
    assert len(ids(p)) == 2 and p.stitched == 1
    both = [r for r in p.sink.rows if r[1] >= 21]
    assert len({r[0] for r in both}) == 2 or all(r[0] != 1 or r[2] > 7.5 for r in both)


def test_stitching_can_be_switched_off():
    p = pipe(stitch=False)
    feed(p, 1, 0, 10, 5.0, 4.0)
    feed(p, 2, 14, 24, 5.3, 4.0)
    assert ids(p) == [1, 2] and p.stitched == 0


def test_stitching_improves_the_product_numbers_on_the_synthetic_store():
    import tempfile
    import logging
    logging.disable(logging.CRITICAL)
    from agent import evaluate as ev, synth
    from agent.detect import MotionDetector
    from agent.pipeline import run_source
    from agent.sinks import CsvSink
    d = tempfile.mkdtemp()
    video = os.path.join(d, "s.mp4")
    floor, gt = synth.render(video, seconds=120, n_people=14, seed=3)
    out = {}
    for name, kw in (("off", dict(stitch=False)), ("on", dict())):
        f = os.path.join(d, name + ".csv")
        run_source(video, Pipeline(floor, MotionDetector(), CsvSink(f), id_base=0, **kw), track_fps=8.0)
        r = ev.evaluate_tracks(gt, ev.load_tracks(f), zones=synth.ZONES)
        out[name] = (r["as_shown"]["visits_ratio"], r["as_shown"]["stopped_share_mae_pp"], r["tracking"]["recall"])
    assert abs(out["on"][0] - 1) < abs(out["off"][0] - 1)         # visit count closer to the truth
    assert out["on"][1] < out["off"][1]                            # stopped share closer to the truth
    assert out["on"][2] > out["off"][2] - 0.03                     # and no loss of coverage


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
