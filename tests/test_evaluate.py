"""Evaluation harness tests. Run:  python -m tests.test_evaluate

The scorer is only useful if it reports failure correctly, so most tests damage a perfect estimate in one known way
(noise, split tracks, missing people, ghost tracks, clock shift) and check that the right score moves the right way.
"""
import json
import os
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
from agent import cli, evaluate as ev, synth

GT = synth.truth_only(seconds=240, fps=8, n_people=30, seed=11)


def at_2hz(df):
    """The truth thinned to 2 Hz the same way the scorer thins it, so a 'perfect' agent really is perfect."""
    return ev.to_grid(df, 2)[["track_id", "t", "x", "y"]]


def score(est, **kw):
    return ev.evaluate_tracks(GT, est, zones=synth.ZONES, **kw)


def test_a_perfect_estimate_scores_perfectly():
    r = score(GT)
    t, p, s = r["tracking"], r["person_zone"], r["as_shown"]
    assert t["recall"] == 1 and t["precision"] == 1 and t["id_switches"] == 0 and t["ghost_tracks"] == 0
    assert r["zones"]["point_agreement"] == 1 and p["stop"]["recall"] == 1 and p["stop"]["precision"] == 1
    assert p["dwell_mae_s"] == 0 and s["stopped_share_mae_pp"] == 0 and s["visits_ratio"] == 1


def test_position_noise_shows_up_as_position_error_and_some_zone_mixups():
    rng = np.random.default_rng(0)
    est = at_2hz(GT).assign(x=lambda d: d.x + rng.normal(0, 0.15, len(d)), y=lambda d: d.y + rng.normal(0, 0.15, len(d)))
    r = score(est)
    assert 0.1 < r["tracking"]["median_err_m"] < 0.3 and r["tracking"]["recall"] > 0.99
    assert 0.8 < r["zones"]["point_agreement"] < 1.0


def test_split_tracks_are_counted_as_switches_and_inflate_the_visit_count():
    g = at_2hz(GT)
    mid = g.groupby("track_id").t.transform(lambda s: (s.min() + s.max()) / 2)
    est = g.assign(track_id=np.where(g.t < mid, g.track_id, g.track_id + 1000))
    r = score(est)
    assert r["tracking"]["id_switches"] == 30 and abs(r["tracking"]["tracks_per_found_person"] - 2.0) < 1e-9
    assert abs(r["as_shown"]["visits_ratio"] - 2.0) < 1e-9
    assert r["person_zone"]["stop"]["recall"] > 0.7           # after stitching by the scorer, most stops are still found


def test_missing_people_lower_recall_and_are_counted():
    est = at_2hz(GT)
    est = est[~est.track_id.isin(sorted(GT.track_id.unique())[:6])]
    r = score(est)
    assert r["tracking"]["missed_people"] == 6 and abs(r["tracking"]["recall"] - 0.8) < 0.08
    assert r["tracking"]["precision"] == 1


def test_ghost_tracks_lower_precision_and_are_counted():
    g = at_2hz(GT)
    rng = np.random.default_rng(2)
    ghosts = pd.DataFrame({"track_id": np.repeat(np.arange(5000, 5004), 120), "t": np.tile(np.arange(120) / 2.0, 4),
                           "x": rng.uniform(0.2, 11.8, 480), "y": rng.uniform(0.2, 7.8, 480)})
    r = score(pd.concat([g, ghosts]))
    assert r["tracking"]["ghost_tracks"] == 4 and r["tracking"]["precision"] < 0.97 and r["tracking"]["recall"] == 1


def test_a_clock_gap_is_visible_and_the_offset_option_fixes_it():
    est = at_2hz(GT).assign(t=lambda d: d.t + 3.0)
    bad, good = score(est), score(est, time_offset_s=-3.0)
    assert bad["tracking"]["recall"] < good["tracking"]["recall"] and good["tracking"]["recall"] == 1
    assert good["tracking"]["median_err_m"] == 0


def test_dwell_bias_is_reported_when_stops_are_cut_short():
    # An agent that loses people while they stand still: drop every other point in the middle of long stops.
    g = at_2hz(GT).sort_values(["track_id", "t"]).reset_index(drop=True)
    step = np.hypot(g.groupby("track_id").x.diff(), g.groupby("track_id").y.diff()).fillna(1.0)
    est = g[(step > 0.2) | (np.arange(len(g)) % 3 != 0)]
    r = score(est)
    assert r["person_zone"]["dwell_bias"] < 0               # shorter dwell than truth, and the report says so


def test_no_overlap_in_time_and_bad_files_give_a_clear_error():
    far = at_2hz(GT).assign(t=lambda d: d.t + 100000)
    for call in (lambda: score(far), lambda: ev.load_tracks(__file__), lambda: ev.evaluate_tracks(GT, GT.iloc[0:0])):
        try:
            call()
            raise AssertionError("accepted")
        except ev.EvalError:
            pass


def test_mmptrack_loader_follows_the_documented_file_format():
    d = tempfile.mkdtemp()
    for frame, rows in {10: "4,253,139,904.15\n7,100,50,900.0\n", 11: "4,254,139,904.15\n", 12: ""}.items():
        open(os.path.join(d, f"topdown_{frame}.csv"), "w").write(rows)
    df = ev.load_mmptrack_truth(d, voxel_m=0.1, origin=(-5.0, 2.0), fps=15.0)
    p4 = df[df.track_id == 4].sort_values("t")
    assert len(df) == 3 and np.allclose(p4.x, [-5 + 25.3, -5 + 25.4]) and np.allclose(p4.y, 2 + 13.9)
    assert np.allclose(p4.t, [0, 1 / 15])
    try:
        ev.load_mmptrack_truth(d, voxel_m=None)
        raise AssertionError("accepted a missing voxel size")
    except ev.EvalError:
        pass


def test_cli_scores_files_and_writes_json():
    d = tempfile.mkdtemp()
    GT.to_csv(os.path.join(d, "truth.csv"), index=False)
    at_2hz(GT).to_csv(os.path.join(d, "est.csv.gz"), index=False)
    json.dump(synth.ZONES, open(os.path.join(d, "zones.json"), "w"))
    out = os.path.join(d, "scores.json")
    cli.main(["evaluate", "--truth", os.path.join(d, "truth.csv"), "--est", os.path.join(d, "est.csv.gz"),
              "--zones", os.path.join(d, "zones.json"), "--json", out])
    s = json.load(open(out))
    assert s["tracking"]["recall"] == 1 and "as_shown" in s
    try:
        cli.main(["evaluate", "--truth", os.path.join(d, "nope.csv"), "--est", os.path.join(d, "est.csv.gz")])
        raise AssertionError("accepted a missing file")
    except SystemExit as e:
        assert "Evaluation problem" in str(e)


def test_the_real_agent_pipeline_on_synthetic_video_is_scored_end_to_end():
    from agent.detect import MotionDetector
    from agent.pipeline import Pipeline, run_source
    from agent.sinks import CsvSink
    d = tempfile.mkdtemp()
    video, out = os.path.join(d, "s.mp4"), os.path.join(d, "t.csv")
    floor, gt = synth.render(video, seconds=90, n_people=10, seed=3)
    run_source(video, Pipeline(floor, MotionDetector(), CsvSink(out), id_base=0), track_fps=8.0)
    r = ev.evaluate_tracks(gt, ev.load_tracks(out), zones=synth.ZONES)
    assert r["tracking"]["recall"] > 0.6 and r["tracking"]["median_err_m"] < 0.5
    assert r["as_shown"]["visits_ratio"] > 0


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
