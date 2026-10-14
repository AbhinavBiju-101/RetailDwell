"""Multi-camera fusion. Run:  python -m tests.test_fusion"""
import os
import re
import tempfile
from types import SimpleNamespace

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import fusion as F
import simulate
from stores import STORES
from tests.test_pos import _csrf, _login


def seg(cam, local, t0, t1, p0, p1, off=(0.0, 0.0), hz=2.0):
    """A camera's track of someone walking from p0 to p1 over [t0, t1], with that camera's own position error `off`."""
    ts = np.arange(t0, t1 + 1e-9, 1 / hz)
    f = (ts - t0) / (t1 - t0)
    return pd.DataFrame(dict(track_id=F.tag_camera(local, cam), t=ts, x=p0[0] + (p1[0] - p0[0]) * f + off[0], y=p0[1] + (p1[1] - p0[1]) * f + off[1]))


def walker(cam0_local, cam1_local, off0=(0, 0), off1=(0.3, 0.0), t_cross=(5.0, 8.0), total=12.0):
    """Someone walking along y=4 from x=4 to x=16 at 1 m/s. Camera 0 sees the first part, camera 1 the second, overlapping by t_cross."""
    pos = lambda t: (4 + t, 4.0)
    a = seg(0, cam0_local, 0, t_cross[1], pos(0), pos(t_cross[1]), off0)
    b = seg(1, cam1_local, t_cross[0], total, pos(t_cross[0]), pos(total), off1)
    return a, b


# ------------------------------------------------------------------ the camera tag
def test_the_camera_number_lives_in_the_upper_bits_and_camera_zero_is_unchanged():
    assert F.tag_camera(12345, 0) == 12345 and int(F.camera_of([12345])[0]) == 0
    t = F.tag_camera(2 ** 40 + 7, 3)
    assert int(F.camera_of([t])[0]) == 3 and t & (2 ** 48 - 1) == 2 ** 40 + 7 and t < 2 ** 63
    from agent.cli import CAMERA_SHIFT, MAX_CAMERA
    assert CAMERA_SHIFT == F.CAMERA_SHIFT and F.tag_camera(1, MAX_CAMERA) < 2 ** 63                     # agent and server agree


def test_an_agent_started_with_a_camera_number_tags_every_id():
    from agent.pipeline import Pipeline
    got = []
    sink = SimpleNamespace(add=lambda tid, t, x, y: got.append(tid), count=0, close=lambda: None)
    p = Pipeline(SimpleNamespace(floor_size=(12.0, 8.0)), SimpleNamespace(gives_ids=True), sink, id_base=(2 << F.CAMERA_SHIFT) | 12345, min_track_s=0.5)
    for i in range(10):
        t = i * 0.5
        raw = p.id_base + 1                                                 # as Pipeline.process does for a detector id
        p._observe(p._resolve(raw, t, 5.0, 4.0), t, 5.0, 4.0)
    assert len(got) >= 5                                                    # points really were emitted
    assert all(int(F.camera_of([g])[0]) == 2 and (g & (2 ** 48 - 1)) == 12345 + 1 for g in got)


# ------------------------------------------------------------------ one camera, or fusion off
def test_one_camera_is_returned_as_it_came_and_so_is_everything_when_switched_off():
    a, b = walker(1, 2)
    one, rep = F.fuse(a)
    assert one.equals(a) and rep["cameras"] == 1 and rep["tracks_in"] == rep["tracks_out"] == 1
    both = pd.concat([a, b])
    off, rep = F.fuse(both, {"fusion_enabled": False})
    assert off.equals(both) and rep["cameras"] == 2 and rep["enabled"] is False and rep["tracks_out"] == 2
    empty, rep = F.fuse(both.iloc[0:0])
    assert empty.empty and rep["tracks_in"] == 0


# ------------------------------------------------------------------ duplicates in the overlap
def test_one_person_seen_by_two_cameras_becomes_one_track_with_positions_averaged():
    a, b = walker(1, 2)                                                    # 3 s of overlap, cameras 0.3 m apart
    out, rep = F.fuse(pd.concat([a, b]))
    assert out.track_id.nunique() == 1 and rep["overlap_merges"] == 1 and rep["tracks_in"] == 2 and rep["tracks_out"] == 1
    assert abs(rep["overlap_error_m"] - 0.3) < 0.05 and out.t.min() == 0 and out.t.max() == 12 and not out.duplicated(["track_id", "t"]).any()
    mid = out[(out.t > 5.4) & (out.t < 7.6)]                               # in the overlap: halfway between the two cameras
    assert abs((mid.x - (4 + mid.t)).mean() - 0.15) < 0.03
    assert out.track_id.iloc[0] == min(a.track_id.iloc[0], b.track_id.iloc[0])


def test_a_fast_walker_crossing_a_narrow_overlap_is_still_one_person():
    a, b = walker(1, 2, t_cross=(5.5, 7.0))                                # only 1.5 s in both views
    out, rep = F.fuse(pd.concat([a, b]))
    assert out.track_id.nunique() == 1


def test_two_different_people_are_not_merged_just_because_both_cameras_see_both():
    a1, b1 = walker(1, 2)
    a2, b2 = [t.assign(y=t.y + 2.5) for t in walker(3, 4, off1=(0.0, -0.3))]
    out, rep = F.fuse(pd.concat([a1, b1, a2, b2]))
    assert out.track_id.nunique() == 2 and rep["tracks_in"] == 4
    ys = out.groupby("track_id").y.mean().round(0).tolist()
    assert sorted(ys) == [4.0, 6.0]                                        # each person kept their own track, not crossed over


def test_two_tracks_of_one_camera_are_never_merged_even_when_the_other_camera_sees_one_blob():
    a1 = seg(0, 1, 0, 8, (4, 4.0), (12, 4.0))
    a2 = seg(0, 2, 0, 8, (4, 4.3), (12, 4.3))                              # a second person 0.3 m away, same camera
    b = seg(1, 3, 5, 12, (9, 4.1), (16, 4.1), off=(0.1, 0))
    out, rep = F.fuse(pd.concat([a1, a2, b]))
    assert out.track_id.nunique() == 2                                     # b joins one of them; the other stays its own person


def test_settings_decide_how_close_counts_as_the_same_person():
    a, b = walker(1, 2, off1=(0.5, 0.0))
    assert F.fuse(pd.concat([a, b]), {"fuse_dist_m": 0.3})[0].track_id.nunique() == 2
    assert F.fuse(pd.concat([a, b]), {"fuse_dist_m": 0.8})[0].track_id.nunique() == 1


# ------------------------------------------------------------------ hand-overs
def test_a_track_that_starts_soon_after_another_cameras_track_ended_nearby_continues_it():
    a = seg(0, 1, 0, 10, (4, 4), (10, 4))
    b = seg(1, 2, 12, 20, (10.6, 4.2), (16, 4.2))
    out, rep = F.fuse(pd.concat([a, b]))
    assert out.track_id.nunique() == 1 and rep["handoffs"] == 1 and rep["overlap_merges"] == 0


def test_a_hand_over_needs_a_short_gap_a_close_start_and_a_different_camera():
    a = seg(0, 1, 0, 10, (4, 4), (10, 4))
    cases = {"gap too long": seg(1, 2, 18, 26, (10.6, 4.2), (16, 4.2)),
             "starts too far away": seg(1, 2, 12, 20, (15, 4.0), (19, 4.2)),
             "same camera": seg(0, 2, 12, 20, (10.6, 4.2), (16, 4.2))}
    for name, b in cases.items():
        out, rep = F.fuse(pd.concat([a, b, seg(1, 99, 100, 102, (1, 1), (2, 2))]))      # a camera-1 track keeps this a two-camera set
        assert rep["handoffs"] == 0, name
    off, rep = F.fuse(pd.concat([a, seg(1, 2, 12, 20, (10.6, 4.2), (16, 4.2))]), {"handoff_gap_s": 0})
    assert rep["handoffs"] == 0 and off.track_id.nunique() == 2


def test_each_track_continues_at_most_one_other_and_the_nearer_start_wins():
    a = seg(0, 1, 0, 10, (4, 4), (10, 4))
    near = seg(1, 2, 11, 18, (10.4, 4.0), (16, 4.0))
    far = seg(1, 3, 11, 18, (11.2, 4.5), (16, 5.0))
    out, rep = F.fuse(pd.concat([a, near, far]))
    assert rep["handoffs"] == 1 and out.track_id.nunique() == 2
    joined = out[out.track_id == out.track_id.min()]
    assert abs(joined[joined.t > 11].y.mean() - 4.0) < 0.1                  # the nearer one (y=4.0) was joined


# ------------------------------------------------------------------ on simulated stores
def _people(sk="lakeside-grocery", lk="A"):
    return simulate.generate(sk)[lk][0][["track_id", "t", "x", "y"]]


def _score(people, est, sk="lakeside-grocery", lk="A"):
    from agent import evaluate as ev
    zones = {n: list(z) for n, z in STORES[sk]["layouts"][lk]["zones"].items()}
    r = ev.evaluate_tracks(people, est, zones=zones)
    return r["as_shown"]["visits_ratio"], r["as_shown"]["stopped_share_mae_pp"]


def test_two_simulated_cameras_stop_people_being_counted_twice():
    people = _people()
    est, truth = simulate.cameras("lakeside-grocery", people, n_cams=2, seed=9)          # a seed the settings were not tuned on
    clusters = {}
    fused, rep = F.fuse(est, clusters=clusters)
    wrong = sum(1 for ms in clusters.values() if len({truth[m] for m in ms}) > 1)
    raw_v, raw_s = _score(people, est)
    v, s = _score(people, fused)
    assert raw_v > 2.5                                                                   # raw cameras: about three tracks per person
    assert v < 0.6 * raw_v and v < 1.6                                                    # fused: close to one per person
    assert s < 0.5 * raw_s                                                                # and the zone stopped shares come right
    assert wrong / len(clusters) < 0.03                                                   # while almost never joining two different people
    assert rep["cameras"] == 2 and rep["tracks_out"] < 0.5 * rep["tracks_in"] and 0.1 < rep["overlap_error_m"] < 0.6


def test_the_reported_camera_gap_tracks_the_real_calibration_error():
    people = _people()
    small = F.fuse(simulate.cameras("lakeside-grocery", people, n_cams=2, bias_m=0.05, seed=3)[0])[1]["overlap_error_m"]
    large = F.fuse(simulate.cameras("lakeside-grocery", people, n_cams=2, bias_m=0.4, seed=3)[0])[1]["overlap_error_m"]
    assert large > small + 0.15                                                          # a worse calibration shows in the number to watch


# ------------------------------------------------------------------ the app
def _two_camera_app(n_days=1):
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    sk, lk = "lakeside-grocery", "A"
    spec = STORES[sk]
    st = repo.create_store(sess, dict(name="Lakeside", width_m=spec["w"], height_m=spec["h"], window_s=300, hero_zone=spec["hero"]))
    lay = repo.upsert_layout(sess, st["id"], lk, "Before", {n: list(z) for n, z in spec["layouts"][lk]["zones"].items()})
    people = _people()
    for i in range(n_days):
        est, _ = simulate.cameras(sk, people, n_cams=2, seed=9 + i)
        ds = repo.create_dataset(sess, lay["id"], f"Day {i + 1}", "upload")
        repo.save_tracks(ds, st, est)
    c = create_app(repo=repo).test_client()
    _login(c, "o@t.example", "password-1")
    return c, repo, sess, st, f"/app/s/{st['id']}", len(people.track_id.unique())


def _visits(html):
    m = re.search(r"([\d,]+)\s*</dd>\s*<dt>Visits", html)
    return int(m.group(1).replace(",", "")) if m else None


def test_the_overview_says_cameras_were_merged_and_how_far_apart_they_place_people():
    c, repo, sess, st, base, n_people = _two_camera_app()
    html = c.get(base + "/").data.decode()
    assert "2 cameras are covering this floor" in html and "duplicates seen by two cameras were merged" in html and "m apart on average" in html
    v = _visits(html)
    assert v is not None and n_people * 0.8 < v < n_people * 1.6
    assert "Several cameras (fusion)" in c.get(base + "/settings").data.decode()


def test_switching_fusion_off_in_settings_brings_the_double_counting_back_and_says_so():
    c, repo, sess, st, base, n_people = _two_camera_app()
    on = _visits(c.get(base + "/").data.decode())
    tok = _csrf(c, base + "/settings")
    c.post(base + "/settings/detection", data=dict(csrf=tok, t_fusion_enabled="0"))
    html = c.get(base + "/").data.decode()
    assert "merging is switched off" in html and _visits(html) > 1.5 * on


def test_a_single_camera_store_shows_no_camera_note_and_pooled_days_are_fused_one_day_at_a_time():
    c, repo, sess, st, base, n_people = _two_camera_app(n_days=2)
    pooled = c.get(base + "/?rec=all").data.decode()
    assert "2 cameras are covering this floor" in pooled and "Showing all 2 recordings" in pooled
    v = _visits(pooled)
    assert 2 * n_people * 0.8 < v < 2 * n_people * 1.6                                   # two days of people, not two days of duplicates
    from tests.test_pooling import _app
    c1, *_r = _app(n=1)
    assert "cameras are covering" not in c1.get(_r[-1] + "/").data.decode()


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
