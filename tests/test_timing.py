"""Time-of-day baselines. Run:  python -m tests.test_timing"""
import os
import re
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an

ZONES = {"Z": [4, 4, 7, 6, "merchandise"], "Other": [9, 6.5, 11, 7.5, "merchandise"]}


def planted(visits_per_window, shoppers=60, windows=4):
    """Shoppers walk past at 1 m/s (no stops). The first k in each window cross zone Z, the rest stay on y=1.5."""
    rows, tid = [], 0
    for w in range(windows):
        for i in range(shoppers):
            tid += 1
            t0 = w * 1800 + i * (1700.0 / shoppers)
            y = 5.0 if i < visits_per_window[w] else 1.5
            for k in range(24):
                rows.append((tid, t0 + k * 0.5, 0.5 + k * 0.5, y))
    return pd.DataFrame(rows, columns=["track_id", "t", "x", "y"])


def run(df):
    return an.analyse_layout(df, ZONES, 12.0, 8.0, 300)["timing"]


def test_flags_a_planted_dip_and_only_that():
    tb = run(planted([30, 30, 9, 30]))
    flagged = [(c["zone"], c["w"], c["flag"]) for c in tb["cells"] if c["flag"]]
    assert flagged == [("Z", 2, "low")], flagged
    msgs = an.timing_flags(tb)
    assert len(msgs) == 1 and "fewer shoppers" in msgs[0]["message"] and "minutes 60 and 90" in msgs[0]["message"]


def test_flags_a_planted_surge():
    tb = run(planted([15, 15, 15, 45]))
    assert [(c["w"], c["flag"]) for c in tb["cells"] if c["flag"] and c["zone"] == "Z"] == [(3, "high")]


def test_steady_traffic_gives_no_flags():
    tb = run(planted([30, 30, 30, 30]))
    assert not any(c["flag"] for c in tb["cells"])


def test_busy_window_does_not_count_as_good_for_every_zone():
    df = pd.concat([planted([30, 30], shoppers=60, windows=2),
                    planted([60, 60], shoppers=120, windows=2).assign(track_id=lambda d: d.track_id + 10_000, t=lambda d: d.t + 3600)])
    tb = run(df)                                   # windows 2 and 3 have twice the shoppers, same 50% share
    assert not any(c["flag"] for c in tb["cells"])


def test_too_little_data_says_why():
    tb = run(planted([30], windows=1))
    assert tb["reason"] and "two" in tb["reason"] and not tb["cells"]
    tb = run(planted([3, 3, 3, 3], shoppers=10))
    assert tb["reason"]


def test_demo_stores_do_not_raise_false_alarms():
    import simulate
    from stores import STORES
    for slug, spec in STORES.items():
        for key, (df, _p) in simulate.generate(slug).items():
            zones = {n: list(z) for n, z in spec["layouts"][key]["zones"].items()}
            R = an.analyse_layout(df[["track_id", "t", "x", "y"]], zones, spec["w"], spec["h"], spec.get("window_s", 300))
            assert not an.timing_flags(R["timing"]), (slug, key)


def test_pages_show_the_grid_and_the_flag():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    st = repo.create_store(sess, dict(name="Shop", width_m=12.0, height_m=8.0))
    layout = repo.upsert_layout(sess, st["id"], "A", "Before", ZONES)
    ds = repo.svc_get_or_create_dataset(layout["id"], "Rec", "ingest")
    repo.append_points(ds, planted([30, 30, 9, 30]))
    c = create_app(repo=repo).test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    c.post("/login", data=dict(email="o@t.example", password="password-1", csrf=tok))
    z = c.get(f"/app/s/{st['id']}/zones").data.decode()
    assert "against its own usual share" in z and "cell-low" in z and "60 to 90 min" in z
    f = c.get(f"/app/s/{st['id']}/flags").data.decode()
    assert "Quieter than usual" in f and "minutes 60 and 90" in f


if __name__ == "__main__":
    for n, fn in sorted(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok   ", n)
