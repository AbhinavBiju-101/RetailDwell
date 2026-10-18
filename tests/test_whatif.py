"""What-if model. Run:  python -m tests.test_whatif

Planted floor, 20 x 10 m. Lane y=5 (door to tills), a low lane y=1, zone Z far from the lane (y 8.5 to 10)."""
import os
import re
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import whatif

ZONES = {"Door": [0, 4.5, 1.5, 5.5, "entrance"], "Tills": [18.5, 4.5, 20, 5.5, "checkout"],
         "Z": [12, 8.5, 15, 10, "merchandise"], "Shelf": [8, 6, 10, 8, "merchandise"], "Low": [8, 0, 11, 0.8, "merchandise"]}


def track(points, stops=()):
    """Walk the waypoints at 1.5 m/s, 2 samples a second; stops = {waypoint index: seconds standing}."""
    out = [points[0]] * 6
    for k in range(1, len(points)):
        a, b = points[k - 1], points[k]
        n = max(int(np.hypot(b[0] - a[0], b[1] - a[1]) / 1.5 / 0.5), 1)
        out += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n + 1)]
        out += [b] * int(dict(stops).get(k, 0) * 2)
    return out


def build(n_lane=60, n_low=60, n_z=30):
    lane = [(0.75, 5), (19.25, 5)]
    low = [(0.75, 5), (0.75, 1), (19.25, 1), (19.25, 5)]
    z = [(0.75, 5), (13.5, 5), (13.5, 9.25), (13.5, 5), (19.25, 5)]
    rows, tid, t0 = [], 0, 0.0
    for pts, st, n in [(lane, (), n_lane), (low, (), n_low), (z, ((2, 8),), n_z)]:
        for _ in range(n):
            tid += 1
            for k, (x, y) in enumerate(track(pts, st)):
                rows.append((tid, t0 + k * 0.5, x, y))
            t0 += 40.0
    return pd.DataFrame(rows, columns=["track_id", "t", "x", "y"])


def analyse(**kw):
    return an.analyse_layout(build(**kw), ZONES, 20.0, 10.0, 300, exclude_staff=False)


def test_moving_a_far_zone_beside_the_lane_counts_who_would_pass():
    R = analyse()
    r = whatif.what_if(R, "Z", cx=4.5, cy=6.5)             # new rectangle x 3-6, y 5.75-7.25, 0.75 m from the lane
    assert r["n_visits"] == 150 and abs(r["reach_now"] - 30 / 150) < 1e-9, r
    assert r["passers_now"] == 30 and abs(r["capture"] - 1.0) < 1e-9
    assert r["passers_new"] == 90                            # the 60 on the lane and the 30 who walk it to reach Z
    assert abs(r["reach_pred"] - 0.6) < 1e-9 and r["sampling_lo"] <= r["change"] <= r["sampling_hi"]


def test_moving_it_away_from_everyone_predicts_a_fall():
    R = analyse()
    s = whatif.what_if(R, "Z", cx=4.5, cy=9.0)             # still off the lane: nobody passes
    assert s["passers_new"] == 0 and s["reach_pred"] == 0.0 and s["change"] < 0


def test_refusals_say_why():
    R = analyse()
    for kw, text in [(dict(zone="Z", cx=1.0, cy=1.0), "outside the floor"), (dict(zone="Z", cx=9.0, cy=7.0), "overlaps Shelf"),
                     (dict(zone="Tills", cx=9.0, cy=3.0), "merchandise"), (dict(zone="Nope", cx=9.0, cy=3.0), "merchandise")]:
        try:
            whatif.what_if(R, **kw)
        except whatif.WhatIfError as e:
            assert text in str(e), (kw, str(e))
        else:
            raise AssertionError(f"expected a refusal for {kw}")
    R2 = analyse(n_z=5)                                      # only 5 pass Z: too few to know its capture rate
    try:
        whatif.what_if(R2, "Z", cx=4.5, cy=6.5)
    except whatif.WhatIfError as e:
        assert "capture rate cannot be estimated" in str(e)
    else:
        raise AssertionError("expected a refusal for a zone almost nobody passes")


def test_scan_marks_overlap_as_closed_and_agrees_with_what_if():
    R = analyse()
    sc = whatif.scan(R, "Z")
    i, j = sc["xs"].index(4.5), sc["ys"].index(6.75)   # grid centres sit half a zone height from the wall
    assert sc["z"][j][i] is not None
    i2, j2 = sc["xs"].index(9.5), sc["ys"].index(7.75)
    assert sc["z"][j2][i2] is None                           # inside Shelf
    one = whatif.what_if(R, "Z", cx=4.5, cy=6.75)
    assert abs(sc["z"][j][i] - one["reach_pred"]) < 1e-9


def test_loiterers_are_not_counted_as_passers():
    R = analyse()
    P = whatif._prepare(R)
    lab = R["labels"]
    assert P["n"] == int((lab.mission != "loiterer / waiter").sum())


def test_swap_gives_each_zone_the_others_place_and_nudges_it_off_the_wall():
    R = analyse()
    sw = whatif.swap(R, "Z", "Low")
    z, low = sw["a"], sw["b"]
    assert z["new"] == [8.0, 0.0, 11.0, 1.5], z["new"]       # centred on Low, nudged up off the wall
    assert z["passers_new"] == 60 and abs(z["reach_pred"] - 60 / 150) < 1e-9
    assert low["reach_now"] == 0 and low["reach_pred"] == 0   # nobody stops at Low, so its capture rate is 0
    assert "change_total" not in sw                           # no net total is offered: tests showed it is not predictable


def test_swap_refusals():
    R = analyse()
    for a, b, text in [("Z", "Z", "two different"), ("Z", "Tills", "merchandise"), ("Z", "Nope", "merchandise")]:
        try:
            whatif.swap(R, a, b)
        except whatif.WhatIfError as e:
            assert text in str(e), (a, b, str(e))
        else:
            raise AssertionError(f"expected a refusal for {a} and {b}")


def test_pages_and_api():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    st = repo.create_store(sess, dict(name="Shop", width_m=20.0, height_m=10.0))
    layout = repo.upsert_layout(sess, st["id"], "A", "Before", ZONES)
    ds = repo.svc_get_or_create_dataset(layout["id"], "Rec", "ingest")
    repo.append_points(ds, build())
    c = create_app(repo=repo).test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    c.post("/login", data=dict(email="o@t.example", password="password-1", csrf=tok))
    base, api = f"/app/s/{st['id']}", f"/api/s/{st['id']}/whatif/A"
    html = c.get(f"{base}/whatif").data.decode()
    assert "What if a zone moved?" in html and '<option value="Z">' in html and 'value="Tills"' not in html
    r = c.get(f"{api}?zone=Z&cx=4.5&cy=6.5")
    assert r.status_code == 200 and abs(r.get_json()["reach_pred"] - 0.6) < 1e-9
    assert c.get(f"{api}?zone=Z&cx=9&cy=7").status_code == 422 and "overlaps" in c.get(f"{api}?zone=Z&cx=9&cy=7").get_json()["error"]
    assert c.get(f"{api}?zone=Z").status_code == 400
    sc = c.get(f"{api}/scan?zone=Z")
    assert sc.status_code == 200 and sc.get_json()["zones"]["Z"]["type"] == "merchandise"
    assert c.get(f"{api}/scan?zone=Tills").status_code == 422
    assert "Or swap it with" in html
    sw = c.get(f"{api}/swap?a=Z&b=Low")
    assert sw.status_code == 200 and abs(sw.get_json()["a"]["reach_pred"] - 0.4) < 1e-9
    assert c.get(f"{api}/swap?a=Z&b=Z").status_code == 422 and c.get(f"{api}/swap?a=Z").status_code == 422


if __name__ == "__main__":
    for n, fn in sorted(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok   ", n)
