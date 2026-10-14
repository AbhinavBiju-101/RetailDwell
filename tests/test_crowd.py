"""Staff filtering and parties. Run:  python -m tests.test_crowd"""
import itertools
import os
import re
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import pos
import simulate
from stores import STORES

ZONES = {"Till": [8, 0, 12, 4, "checkout"], "Shelf": [1, 5, 4, 8, "merchandise"], "Back room": [0, 0, 3, 3, "staff"]}
COLS = ["track_id", "t", "x", "y"]


def walk(tid, t0, t1, p0, p1, hz=2.0):
    ts = np.arange(t0, t1, 1 / hz)
    f = (ts - t0) / max(t1 - t0, 1e-9)
    return pd.DataFrame(dict(track_id=tid, t=ts, x=p0[0] + (p1[0] - p0[0]) * f, y=p0[1] + (p1[1] - p0[1]) * f))


def still(tid, t0, t1, p, hz=2.0):
    return walk(tid, t0, t1, p, p, hz)


# ------------------------------------------------------------------ staff
def test_staff_are_found_by_what_they_do_and_say_why():
    recording = still(1, 0, 6000, (6, 8))                                   # the recording is long enough to judge duration
    cashier = still(2, 100, 1100, (10, 2))                                  # 1000 s inside the checkout zone
    backroom = still(3, 200, 300, (1, 1))                                   # 100 s in a staff zone
    stocker = walk(4, 0, 3000, (5, 6), (6, 7))                              # 50 minutes on the floor
    shopper = walk(5, 500, 700, (6, 7.9), (10, 2))
    t = pd.concat([recording.assign(track_id=9), cashier, backroom, stocker, shopper])
    found = an.find_staff(t, ZONES)
    assert set(found) == {2, 3, 4, 9} or set(found) == {2, 3, 4}, found     # 9 and 4 are long-stayers, 5 is a shopper
    assert "register" in found[2] and "staff zone" in found[3] and "on the floor" in found[4] and 5 not in found


def test_a_slow_shopper_or_a_short_recording_is_not_called_staff():
    slow = walk(1, 0, 1500, (5, 6), (7, 7))                                  # 25 minutes: long, but not staff-long
    queue = still(2, 100, 400, (9, 1.5))                                    # 5 minutes waiting at the till
    short = walk(3, 0, 2600, (5, 6), (6, 7))                                 # 43 minutes in a 43-minute recording: cannot tell
    assert an.find_staff(pd.concat([slow, queue]), ZONES) == {}
    assert an.find_staff(short, ZONES) == {}


def test_staff_do_not_count_as_shoppers_or_inflate_the_checkout():
    shoppers = pd.concat([walk(100 + i, 60 * i, 60 * i + 90, (6, 7.9), (10, 2)) for i in range(12)]
                         + [still(200 + i, 60 * i + 90, 60 * i + 120, (10, 2)) for i in range(12)], ignore_index=True)
    shoppers = shoppers.drop_duplicates(["track_id", "t"])
    cashier = still(1, 0, 900, (10.5, 3.5))
    R_with = an.analyse_layout(pd.concat([shoppers, cashier]), ZONES, 12, 8, 300)
    R_raw = an.analyse_layout(pd.concat([shoppers, cashier]), ZONES, 12, 8, 300, exclude_staff=False)
    R_ref = an.analyse_layout(shoppers, ZONES, 12, 8, 300)
    assert R_with["staff"].keys() == {1} and 1 not in set(R_with["labels"].track_id) and 1 in set(R_raw["labels"].track_id)
    occ = lambda R: float(R["checkout"]["occupancy"].mean())
    assert abs(occ(R_with) - occ(R_ref)) < 1e-9 and occ(R_raw) > occ(R_ref)   # the cashier alone would add to the queue


def test_zone_type_staff_is_accepted_by_validation_and_the_editor():
    import validation as val
    zones = val.clean_zones({"Back room": [0, 0, 2, 2, "staff"]}, 10, 8)
    assert zones["Back room"][4] == "staff"
    assert "staff" in open("templates/zone_editor.html").read()


# ------------------------------------------------------------------ parties
def _path(tid, t0, gap=0.0):
    """Walk in, browse at a shelf, walk out: about 2 minutes, at a normal walking pace."""
    a, b, c = (2 + gap, 0.4), (3 + gap, 6.5), (11 + gap, 7)
    return pd.concat([walk(tid, t0, t0 + 15, a, b), still(tid, t0 + 15, t0 + 100, b), walk(tid, t0 + 100, t0 + 115, b, c)])


def _pair(a, b, t0=0, gap=0.9):
    return pd.concat([_path(a, t0), _path(b, t0 + 0.5, gap)])


def test_two_people_walking_together_are_a_party_and_a_stranger_is_not():
    t = pd.concat([_pair(1, 2), _path(3, 0, gap=6.0)])
    P = an.find_parties(t)
    assert P["of"][1] == P["of"][2] and P["of"][3] != P["of"][1] and P["sizes"][P["of"][1]] == 2


def test_strangers_queueing_side_by_side_are_not_a_party():
    # Arrive 20 s apart and leave 40 s apart, shuffling forward together for over a minute. Close, moving, long, but no party.
    a = pd.concat([walk(1, 0, 20, (6, 7), (10, 2)), still(1, 20, 90, (10, 2)), walk(1, 90, 100, (10, 2), (6, 7))])
    b = pd.concat([walk(2, 20, 40, (6, 7), (10.6, 2.3)), still(2, 40, 130, (10.6, 2.3)), walk(2, 130, 140, (10.6, 2.3), (6, 7))])
    P = an.find_parties(pd.concat([a, b]))
    assert P["of"][1] != P["of"][2]


def test_standing_side_by_side_without_walking_together_is_not_a_party():
    t = pd.concat([still(1, 0, 90, (5, 5)), still(2, 0, 90, (5.8, 5))])
    assert an.find_parties(t)["of"][1] != an.find_parties(t)["of"][2]


def test_a_crowd_is_not_one_party():
    t = pd.concat([walk(i, 0, 100, (2 + 0.4 * i, 0.4), (11, 7)) for i in range(1, 11)])
    sizes = an.find_parties(t)["sizes"]
    assert max(sizes.values()) <= 6


def test_party_detection_on_the_simulated_pharmacy_finds_the_true_groups():
    spec = STORES["corner-pharmacy"]
    for lk in "AB":
        df, truth = simulate.crowd("corner-pharmacy", lk, simulate.generate("corner-pharmacy")[lk][0])
        zones = {n: list(z) for n, z in spec["layouts"][lk]["zones"].items()}
        clean = df[~df.track_id.isin(list(an.find_staff(df, zones)))]
        P = an.find_parties(clean)
        groups = {}
        for c, l in truth["parties"].items():
            groups.setdefault(l, [l]).append(c)
        true = {frozenset(p) for g in groups.values() for p in itertools.combinations(g, 2)}
        mem = {}
        for t, r in P["of"].items():
            mem.setdefault(r, []).append(t)
        found = {frozenset(p) for m in mem.values() for p in itertools.combinations(m, 2)}
        assert len(true & found) / len(true) >= 0.9 and len(true & found) / len(found) >= 0.95, (lk, len(true), len(found), len(true & found))
        assert sorted(an.find_staff(df, zones)) == sorted(truth["staff"])


def test_stores_with_no_companions_get_almost_no_false_parties():
    for sk in ("lakeside-grocery", "thread-and-co"):
        for lk, (df, _p) in simulate.generate(sk).items():
            sizes = an.find_parties(df)["sizes"]
            assert sum(1 for v in sizes.values() if v > 1) <= 3, (sk, lk)


# ------------------------------------------------------------------ sales and parties
def _pharmacy(lk):
    spec = STORES["corner-pharmacy"]
    df, lines, truth = simulate.generate_with_sales("corner-pharmacy")[lk]
    base = simulate.generate("corner-pharmacy")[lk][0]
    zones = {n: list(z) for n, z in spec["layouts"][lk]["zones"].items()}
    args = (zones, spec["w"], spec["h"], spec["window_s"])
    return an.analyse_layout(df[COLS], *args), an.analyse_layout(base[COLS], *args), lines


def test_a_companion_at_the_register_does_not_break_matching_or_conversion():
    R, R_ref, lines = _pharmacy("A")
    P, ref = pos.analyse_pos(R, lines), pos.analyse_pos(R_ref, lines)
    broken = dict(R, parties=dict(of={}, sizes={}))
    bad = pos.analyse_pos(broken, lines)
    assert abs(P["kpi"]["conversion"] - ref["kpi"]["conversion"]) < 0.05           # same as with no companions at all
    assert abs(P["kpi"]["avg_basket"] - ref["kpi"]["avg_basket"]) / ref["kpi"]["avg_basket"] < 0.05
    assert bad["kpi"]["conversion"] < P["kpi"]["conversion"] - 0.1                  # ...and clearly worse if parties are ignored
    assert bad["match"]["n_unmatched_services"] > 10 and P["match"]["n_unmatched_services"] <= 2
    assert P["kpi"]["n_parties"] > 30 and P["kpi"]["revenue_per_party"] > P["kpi"]["revenue_per_shopper"]
    assert abs(P["kpi"]["matched_revenue"] - ref["kpi"]["matched_revenue"]) / ref["kpi"]["matched_revenue"] < 0.05


def test_a_party_buys_together_in_the_zone_funnel_but_revenue_is_counted_once():
    R, _, lines = _pharmacy("A")
    P = pos.analyse_pos(R, lines)
    assert sum(r["revenue"] for r in P["zones"]) <= P["kpi"]["matched_revenue"] + 1e-6   # a basket is not counted per member
    members = P["lines"].track_id.nunique()
    assert members <= P["match"]["n_usable"]                                              # one payer per sale


def test_overview_and_revenue_pages_say_what_was_left_out_and_found():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    spec = STORES["corner-pharmacy"]
    st = repo.create_store(sess, dict(name="P", width_m=spec["w"], height_m=spec["h"], window_s=600, hero_zone="Wellness"))
    df, lines, _ = simulate.generate_with_sales("corner-pharmacy")["A"]
    lay = repo.upsert_layout(sess, st["id"], "A", "Before", {n: list(z) for n, z in spec["layouts"]["A"]["zones"].items()})
    ds = repo.create_dataset(sess, lay["id"], "Day", "upload")
    repo.save_tracks(ds, st, df[COLS])
    repo.save_pos(ds, st, lines)
    c = create_app(repo=repo).test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    c.post("/login", data=dict(email="o@t.example", password="password-1", csrf=tok))
    ov = c.get(f"/app/s/{st['id']}/").data.decode()
    assert "left out as staff" in ov and "stood at the register" in ov and "moved in" in ov
    rv = c.get(f"/app/s/{st['id']}/revenue").data.decode()
    assert "Revenue per party" in rv and "arrived together" in rv


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
