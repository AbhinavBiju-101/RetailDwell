"""Zone flow. Run:  python -m tests.test_flow"""
import os
import re
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import flow

ZONES = {"A": [2, 1.5, 5, 5, "merchandise"], "B": [8, 1.5, 11, 5, "merchandise"], "C": [14, 1.5, 17, 5, "merchandise"],
         "D": [8, 7.5, 11, 9.5, "merchandise"], "Door": [0, 5.75, 1.5, 6.75, "entrance"], "Tills": [18.5, 5.75, 20, 6.75, "checkout"],
         "Seats": [2, 7.5, 5, 9.5, "seating"]}
CORR = 6.25     # walking lane between the shelf rows


def centre(z):
    x0, y0, x1, y1, _ = ZONES[z]
    return (x0 + x1) / 2, (y0 + y1) / 2


def walk(pts, a, b, speed=1.5):
    n = max(int(np.hypot(b[0] - a[0], b[1] - a[1]) / speed / 0.5), 1)
    for k in range(1, n + 1):
        pts.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))


def shopper(route, stop_s=8.0):
    """Door, then each zone in turn (via the lane, stopping stop_s), then back to the door."""
    pts = [centre("Door")] * 6
    cur = centre("Door")
    for z in route:
        c = centre(z)
        for wp in [(cur[0], CORR), (c[0], CORR), c]:
            walk(pts, cur, wp)
            cur = wp
        pts += [c] * int(stop_s * 2)
    for wp in [(cur[0], CORR), centre("Door")]:
        walk(pts, cur, wp)
        cur = wp
    pts += [centre("Door")] * 6
    return pts


def planted(groups):
    rows, tid, t0 = [], 0, 0.0
    for route, n in groups:
        for _ in range(n):
            tid += 1
            for k, (x, y) in enumerate(shopper(route)):
                rows.append((tid, t0 + k * 0.5, x, y))
            t0 += 25.0
    return pd.DataFrame(rows, columns=["track_id", "t", "x", "y"])


GROUPS = [(["A", "B", "C", "Tills"], 60), (["A", "B", "D", "Tills"], 40), (["B", "C", "B", "Tills"], 40), (["A", "D", "Tills"], 4)]


def analyse(groups=GROUPS):
    return an.analyse_layout(planted(groups), ZONES, 20.0, 10.0, 300, exclude_staff=False)


def test_paths_skip_entrance_and_circulation_and_collapse_repeats():
    R = analyse()
    seqs, n = flow.zone_sequences(R)
    got = pd.Series([tuple(s) for s in seqs.values()]).value_counts().to_dict()
    assert got[("A", "B", "C", "Tills")] == 60 and got[("A", "B", "D", "Tills")] == 40, got
    assert got[("B", "C", "B", "Tills")] == 40 and got[("A", "D", "Tills")] == 4, got
    assert all("Door" not in s and "Circulation" not in s for s in seqs.values())


def test_small_routes_are_not_drawn_but_still_counted():
    F = flow.analyse_flow(analyse())
    sk = F["sankey"]
    pairs = {(sk["nodes"][l["source"]]["label"], sk["nodes"][l["target"]]["label"]): l["value"] for l in sk["links"]}
    assert ("A", "B") in pairs and pairs[("A", "B")] == 100, pairs
    assert ("A", "D") not in pairs                                  # only 4 shoppers took A then D
    assert all(l["value"] >= flow.FLOW_MIN_EDGE for l in sk["links"])
    d = next(r for r in F["rows"] if r["zone"] == "D")
    assert d["reached"] == 44                                       # the table still counts all of them
    assert next(r for r in F["rows"] if r["zone"] == "Tills")["leaves_pct"] == 1.0
    assert not any(r["zone"] == "Door" for r in F["rows"])


def test_turn_back_flag_fires_on_the_planted_zone_only():
    F = flow.analyse_flow(analyse())
    assert [f["zone"] for f in F["findings"]] == ["C"], F["findings"]
    c = next(r for r in F["rows"] if r["zone"] == "C")
    assert abs(c["turn_pct"] - 0.4) < 1e-9 and c["arrivals"] == 100
    assert "most often B" in F["findings"][0]["text"]


def test_returning_from_a_seat_is_not_a_turn_back():
    # everyone waits in the seats, walks to shelf D and sits down again: not a turn-back at D
    groups = [(["Seats", "D", "Seats", "Tills"], 40), (["A", "B", "C", "Tills"], 60), (["A", "B", "D", "Tills"], 40)]
    F = flow.analyse_flow(analyse(groups))
    assert not any(f["zone"] == "D" for f in F["findings"]), F["findings"]


def test_too_few_zones_to_judge_means_no_flag():
    F = flow.analyse_flow(analyse([(["B", "C", "B", "Tills"], 40), (["B", "C", "Tills"], 40)]))
    assert F["findings"] == [] and F["turn_median"] is None


def test_too_little_data_says_so():
    F = flow.analyse_flow(analyse([(["A", "B", "Tills"], 3)]))
    assert F["sankey"] is None and "Fewer than" in F["note"]


def test_unknown_segment_is_refused():
    try:
        flow.zone_sequences(analyse([(["A", "Tills"], 6)]), "nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def _client():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    st = repo.create_store(sess, dict(name="Shop", width_m=20.0, height_m=10.0))
    layout = repo.upsert_layout(sess, st["id"], "A", "Before", ZONES)
    ds = repo.svc_get_or_create_dataset(layout["id"], "Rec", "ingest")
    repo.append_points(ds, planted(GROUPS))
    c = create_app(repo=repo).test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    c.post("/login", data=dict(email="o@t.example", password="password-1", csrf=tok))
    return c, st["id"]


def test_stopping_in_a_zone_finds_exactly_those_visits():
    R = analyse()
    assert len(flow.ids_stopping_in(R, "D")) == 44 and len(flow.ids_stopping_in(R, "C")) == 100
    assert len(flow.ids_stopping_in(R, "Seats")) == 0 and len(flow.ids_stopping_in(R, "Nope")) == 0
    assert len(flow.ids_stopping_in(R, "A")) == 104            # 60 + 40 + 4 visits start at A


def test_flow_can_be_narrowed_to_visits_that_stopped_in_a_zone():
    R = analyse()
    F = flow.analyse_flow(R, "shoppers", flow.ids_stopping_in(R, "D"))
    assert F["n_paths"] == 44 and next(r for r in F["rows"] if r["zone"] == "D")["share"] == 1.0
    assert not any(r["zone"] == "C" for r in F["rows"]), "nobody who stopped at D also stopped at C on this floor"


def test_visits_are_placed_in_the_same_time_windows_as_the_baselines():
    R = analyse()
    slots = an.visit_slots(R["df"], 1200)                       # shoppers start 25 s apart: 48 per 1200 s
    assert slots.iloc[0] == 0 and slots.iloc[47] == 0 and slots.iloc[48] == 1 and slots.iloc[-1] == 2
    pooled = an.visit_slots(R["df"], 1200, slot_fn=lambda t: (t // 1800).astype(int))
    assert set(pooled.unique()) == {0, 1}


def test_party_filter_separates_parties_from_people_alone():
    from app import journey_filters, journey_ids
    R = analyse()
    ids = [int(t) for t in R["labels"].track_id]
    of = {t: t for t in ids}
    of.update({2: 1, 6: 5, 7: 5})                               # a pair (1, 2) and a trio (5, 6, 7); everyone else is alone
    R["parties"] = dict(of=of, sizes={1: 2, 5: 3, **{t: 1 for t in ids if t not in (1, 2, 5, 6, 7)}})
    assert journey_filters(R)["parties"] == dict(n_parties=2, n_visits=5)
    assert journey_ids(R, None, None, "party") == {1, 2, 5, 6, 7}
    assert journey_ids(R, None, None, "alone") == set(ids) - {1, 2, 5, 6, 7}
    both = journey_ids(R, "D", None, "party")
    assert both == {1, 2, 5, 6, 7} & flow.ids_stopping_in(R, "D")
    assert journey_ids(R, None, None, "") is None
    try:
        journey_ids(R, None, None, "everyone")
    except ValueError:
        return
    raise AssertionError("expected a refusal for an unknown party filter")


def test_journeys_filters_in_the_app():
    c, sid = _client()
    api = f"/api/s/{sid}"
    base = c.get(f"{api}/journeys/A?n=5").get_json()
    assert base["available"] == 144 and base["of_segment"] == 144 and "D" in base["filters"]["zones"]
    d = c.get(f"{api}/journeys/A?n=5&visited=D").get_json()
    assert d["available"] == 44 and d["of_segment"] == 144
    wins = base["filters"]["windows"]
    assert len(wins) >= 2
    parts = [c.get(f"{api}/journeys/A?n=5&w={w['w']}").get_json()["available"] for w in wins]
    assert sum(parts) == 144 and all(0 < p < 144 for p in parts), parts   # the windows split the visits, none counted twice
    both = c.get(f"{api}/journeys/A?n=5&visited=D&w={wins[0]['w']}").get_json()["available"]
    assert 0 <= both <= min(44, parts[0])
    assert c.get(f"{api}/journeys/A?visited=Nope").status_code == 400
    assert c.get(f"{api}/journeys/A?w=999").status_code == 400
    f = c.get(f"{api}/flow/A?visited=C").get_json()
    assert f["n_paths"] == 100
    assert c.get(f"{api}/flow/A?visited=Nope").status_code == 400
    assert base["filters"]["parties"]["n_parties"] == 0                       # shoppers 25 s apart arrive alone
    assert c.get(f"{api}/journeys/A?n=5&party=party").get_json()["available"] == 0
    assert c.get(f"{api}/journeys/A?n=5&party=alone").get_json()["available"] == 144
    assert c.get(f"{api}/journeys/A?party=everyone").status_code == 400
    html = c.get(f"/app/s/{sid}/journeys").data.decode()
    assert 'id="f-zone"' in html and "Visits that stopped in" in html and 'id="f-party"' in html


def test_pages_and_api():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    st = repo.create_store(sess, dict(name="Shop", width_m=20.0, height_m=10.0))
    layout = repo.upsert_layout(sess, st["id"], "A", "Before", ZONES)
    ds = repo.svc_get_or_create_dataset(layout["id"], "Rec", "ingest")
    repo.append_points(ds, planted(GROUPS))
    c = create_app(repo=repo).test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    c.post("/login", data=dict(email="o@t.example", password="password-1", csrf=tok))
    base = f"/app/s/{st['id']}"
    assert "Where people go next" in c.get(f"{base}/journeys").data.decode()
    r = c.get(f"/api/s/{st['id']}/flow/A")
    assert r.status_code == 200 and r.get_json()["n_paths"] == 144, r.data[:200]
    r2 = c.get(f"/api/s/{st['id']}/flow/A?segment=directed")
    assert r2.status_code == 200
    assert c.get(f"/api/s/{st['id']}/flow/A?segment=nope").status_code == 400
    flags = c.get(f"{base}/flags").data.decode()
    assert "Turn-back" in flags and "went straight back" in flags


if __name__ == "__main__":
    for n, fn in sorted(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok   ", n)
