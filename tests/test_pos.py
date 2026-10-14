"""Sales matching and revenue per visitor. Run:  python -m tests.test_pos"""
import io
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
import validation as val
from stores import STORES

ZONES = {"Checkout": [8, 0, 12, 4, "checkout"], "Shelf": [1, 5, 4, 8, "merchandise"]}


def register_tracks(ends, stand_s=12.0, start_id=1):
    """Each shopper walks in, stands at the register for stand_s, and leaves. ends[i] is when they finish."""
    rows = []
    for i, end in enumerate(ends):
        tid = start_id + i
        t = end - stand_s
        for k in range(int(stand_s * 2) + 1):                       # standing at (10, 2)
            rows.append((tid, t + k * 0.5, 10 + 0.05 * (k % 3), 2.0))
        for k in range(1, 9):                                       # walking away
            rows.append((tid, end + k * 0.5, 10 - 0.7 * k, 2.0 + 0.7 * k))
    return pd.DataFrame(rows, columns=["track_id", "t", "x", "y"])


def sales_for(ends, offset, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"txn_id": [f"T{i}" for i in range(len(ends))],
                         "t": [e + offset + rng.normal(0, noise) for e in ends]})


def test_register_visit_ends_when_the_last_stop_ends():
    df = register_tracks([100.0])
    s = pos.register_services(df, ZONES)
    assert len(s) == 1 and abs(s.t_end.iloc[0] - 100.0) <= 1.0, s


def test_clock_offset_is_found_and_sales_match_the_right_people():
    ends = [60 + 28 * i for i in range(70)]
    df = register_tracks(ends)
    tx = sales_for(ends, offset=7.0, noise=1.5)
    m = pos.match_transactions(pos.register_services(df, ZONES), tx)
    S = m["summary"]
    assert S["offset_found"] and abs(S["offset_s"] - 7.0) < 1.0, S
    assert S["n_usable"] == 70 and S["n_ambiguous"] == 0, S
    got = dict(zip(m["pairs"].txn_id, m["pairs"].track_id))
    assert all(got[f"T{i}"] == i + 1 for i in range(70))


def test_unrelated_times_do_not_invent_a_clock_offset():
    rng = np.random.default_rng(3)
    off, found = pos.estimate_offset(rng.uniform(0, 3000, 80), rng.uniform(0, 3000, 80))
    assert not found and off == 0.0


def test_two_shoppers_paying_together_are_ambiguous_and_left_out():
    df = register_tracks([100.0, 101.0, 400.0])
    srv = pos.register_services(df, ZONES).sort_values("t_end")
    mid = float(srv.t_end.iloc[:2].mean())                          # the sale falls halfway between the two
    tx = pd.DataFrame({"txn_id": ["A", "B"], "t": [mid, 400.0]})
    m = pos.match_transactions(srv, tx, offset_s=0.0)
    st = dict(zip(m["pairs"].txn_id, m["pairs"].status))
    assert st["A"] == "ambiguous" and st["B"] == "clear", st
    lines = pd.DataFrame({"txn_id": ["A", "B"], "t": [mid, 400.0], "zone": ["Shelf", "Shelf"], "amount": [50.0, 70.0]})
    R = an.analyse_layout(df, ZONES, 14.0, 10.0, 300)
    P = pos.analyse_pos(R, lines)
    assert P["match"]["n_ambiguous"] == 1 and P["kpi"]["matched_revenue"] == 70.0


def test_a_sale_with_nobody_at_the_register_stays_unmatched():
    df = register_tracks([100.0])
    tx = pd.DataFrame({"txn_id": ["A", "Z"], "t": [100.0, 900.0]})
    S = pos.match_transactions(pos.register_services(df, ZONES), tx, offset_s=0.0)["summary"]
    assert S["n_usable"] == 1 and S["n_unmatched_txn"] == 1


def test_no_sales_means_no_report():
    R = an.analyse_layout(register_tracks([100.0]), ZONES, 14.0, 10.0, 300)
    assert pos.analyse_pos(R, pd.DataFrame(columns=pos.POS_COLS)) is None


def test_sales_validation():
    csv = b"TXN_ID,t,zone,amount,card_number,cashier\nA1,10,Produce,5.5,4111,bob\nA1,12,Snacks,3,4111,bob\nA2,20,Produce,8,,\n"
    df = val.pos_from_csv(csv)
    assert list(df.columns) == ["txn_id", "t", "zone", "amount"] and df.txn_id.nunique() == 2
    assert set(df[df.txn_id == "A1"].t) == {10.0}                    # one time per sale
    # a sale a second before the recording started is clock drift, not an error: it is kept at time 0
    assert val.pos_from_csv(b"txn_id,t,zone,amount\nA,-1,Z,4\nB,30,Z,4\n").t.tolist() == [0.0, 30.0]
    for bad in (b"txn_id,t,zone\nA,1,Z\n", b"txn_id,t,zone,amount\nA,1,Z,-4\n",
                b"txn_id,t,zone,amount\nA,-500,Z,4\n"):                                   # 500 s early: before the recording, nothing left
        try:
            val.pos_from_csv(bad)
            raise AssertionError("accepted")
        except val.ValidationError:
            pass
    try:
        val.pos_from_json([{"txn_id": "A", "t": 1, "zone": "Z", "amount": 2, "card": "4111"}])
        raise AssertionError("accepted extra field")
    except val.ValidationError:
        pass
    assert len(val.pos_from_json([{"txn_id": "A", "t": 1, "zone": "Z", "amount": 2}])) == 1


def _analyse(store, layout):
    spec = STORES[store]
    df, lines, truth = simulate.generate_with_sales(store)[layout]
    zones = {n: list(z) for n, z in spec["layouts"][layout]["zones"].items()}
    R = an.analyse_layout(df[["track_id", "t", "x", "y"]], zones, spec["w"], spec["h"], spec["window_s"])
    R["pos"] = pos.analyse_pos(R, lines)
    return R, truth


def _accuracy(R, truth):
    pr = R["pos"]["pairs"].merge(truth, on="txn_id", suffixes=("", "_true"))
    u = pr[pr.status != "ambiguous"]
    return float((u.track_id == u.track_id_true).mean())


def test_simulated_sales_are_matched_to_the_true_shopper():
    R, truth = _analyse("lakeside-grocery", "A")
    S = R["pos"]["match"]
    assert _accuracy(R, truth) >= 0.95 and S["match_rate"] >= 0.9, S
    assert abs(S["offset_s"] - 4.0) < 1.5 and S["offset_found"], S      # simulated POS clock runs 4 s ahead
    R2, truth2 = _analyse("corner-pharmacy", "B")                       # two tills finish together: harder
    assert _accuracy(R2, truth2) >= 0.9
    assert R2["pos"]["match"]["n_ambiguous"] > S["n_ambiguous"]


def test_revenue_figures_add_up():
    R, _ = _analyse("thread-and-co", "A")
    P = R["pos"]
    k = P["kpi"]
    assert 0 < k["matched_revenue"] <= k["total_revenue"] and 0 <= k["conversion"] <= 1
    zone_rev = sum(r["revenue"] for r in P["zones"])
    assert zone_rev <= k["matched_revenue"] + 1e-6
    for r in P["zones"]:
        assert r["bought_after_stop"] <= r["stopped"] and r["stopped"] <= r["reached"] + 5
        assert r["bought"] == r["bought_after_stop"] + r["bought_without_stop"] + r["bought_not_seen"] \
            or r["bought"] >= r["bought_after_stop"]
    assert abs(sum(m["n"] for m in P["missions"]) - len(P["lab"][P["lab"].known])) == 0


def test_flags_find_the_planted_offer_problem_and_nothing_made_up():
    R, _ = _analyse("volt-electronics", "B")
    kinds = [(f["kind"], f.get("zone")) for f in R["pos"]["items"]]
    assert ("Offer problem", "Laptops") in kinds, kinds
    R2, _ = _analyse("thread-and-co", "B")                              # Men's buy rate is normal by construction
    assert ("Offer problem", "Men") not in [(f["kind"], f.get("zone")) for f in R2["pos"]["items"]]


def test_revenue_map_hides_thin_cells_and_before_after_compares_per_shopper():
    Ra, _ = _analyse("lakeside-grocery", "A")
    Rb, _ = _analyse("lakeside-grocery", "B")
    Z, xs, ys = pos.revenue_map(Ra, Ra["pos"], 20.0, 12.0)
    assert Z.shape == (24, 40) and np.isnan(Z).any() and np.isfinite(Z).any()
    c = pos.compare_revenue(Ra, Rb, "Weekly offers")
    assert c["lift"] > 0 and c["ci_low"] < c["lift"] < c["ci_high"]     # offers moved onto the aisle: more spend from them
    assert pos.compare_revenue(Ra, Rb) is not None
    Rb2 = dict(Rb, pos=None)
    assert pos.compare_revenue(Ra, Rb2) is None


def test_demo_shoppers_are_unchanged_by_adding_sales_staff_and_companions():
    base = simulate.generate("corner-pharmacy")["A"][0]
    b = simulate.generate_with_sales("corner-pharmacy")["A"][0]
    assert b[b.track_id.isin(base.track_id.unique())].reset_index(drop=True).equals(base.reset_index(drop=True))
    assert b.track_id.nunique() > base.track_id.nunique()                      # companions and staff were added on top
    g = simulate.generate("lakeside-grocery")["A"][0]
    assert g.equals(simulate.generate_with_sales("lakeside-grocery")["A"][0])  # stores without a crowd entry are untouched


# ------------------------------------------------------------------ the app, end to end
def _login(c, email, pw):
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    return c.post("/login", data=dict(email=email, password=pw, csrf=tok))


def _csrf(c, path):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).data.decode()).group(1)


def test_app_flow_upload_page_map_and_ingest():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    spec = STORES["corner-pharmacy"]
    st = repo.create_store(sess, dict(name="Pharm", width_m=spec["w"], height_m=spec["h"], window_s=600, hero_zone="Wellness"))
    df, lines, truth = simulate.generate_with_sales("corner-pharmacy")["A"]
    lay = repo.upsert_layout(sess, st["id"], "A", "Before",
                             {n: list(z) for n, z in spec["layouts"]["A"]["zones"].items()})
    ds = repo.create_dataset(sess, lay["id"], "Day", "upload")
    repo.save_tracks(ds, st, df[["track_id", "t", "x", "y"]])
    app = create_app(repo=repo)
    c = app.test_client()
    _login(c, "o@t.example", "password-1")
    base = f"/app/s/{st['id']}"

    page = c.get(base + "/revenue").data.decode()
    assert "no sales data yet" in page
    assert "Revenue" in c.get(base + "/").data.decode()

    tok = _csrf(c, base + "/settings")
    csv = lines.assign(card_number="4111", cashier="x").to_csv(index=False).encode()
    r = c.post(base + "/layouts/A/sales", data=dict(csrf=tok, file=(io.BytesIO(csv), "sales.csv")),
               content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    stored = repo.load_pos(repo.list_datasets(sess, [lay["id"]])[0], st)
    assert list(stored.columns) == ["txn_id", "t", "zone", "amount"]

    page = c.get(base + "/revenue").data.decode()
    assert "Revenue per shopper" in page and "How sales were matched" in page and "Stoppers who buy" in page
    assert "no sales data yet" not in page and "\u20b9" in page
    m = c.get(f"/api/s/{st['id']}/revenue-map/A").get_json()
    assert len(m["z"]) == 20 and len(m["x"]) == 28 and m["currency"] == "\u20b9" and m["average"] > 0
    assert "Revenue per shopper" in c.get(base + "/").data.decode()      # overview KPI appears with sales

    bad = c.post(base + "/layouts/A/sales", data=dict(csrf=tok, file=(io.BytesIO(b"a,b\n1,2\n"), "x.csv")),
                 content_type="multipart/form-data")
    assert bad.status_code == 302 and b"txn_id" in c.get(base + "/settings").data      # clear message, old data kept
    assert len(repo.load_pos(repo.list_datasets(sess, [lay["id"]])[0], st)) == len(stored)

    # till feed with an API key; the same lines twice must not double count
    _, key = repo.create_api_key(sess, st["id"], "Till")
    h = {"Authorization": f"Bearer {key}"}
    body = dict(layout="A", recording="Live", sales=[dict(txn_id="X1", t=10.0, zone="Wellness", amount=99.0)])
    api = app.test_client()
    assert api.post("/api/ingest/v1/sales", json=body).status_code == 401
    assert api.post("/api/ingest/v1/sales", json=body, headers=h).status_code == 202
    assert api.post("/api/ingest/v1/sales", json=body, headers=h).status_code == 202
    ds2 = repo.svc_get_or_create_dataset(lay["id"], "Live", "ingest")
    assert len(repo.load_pos(ds2, st)) == 1
    body["sales"][0]["card"] = "4111"
    assert api.post("/api/ingest/v1/sales", json=body, headers=h).status_code == 400


def test_demo_account_cannot_upload_sales():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    repo.ensure_user("demo@t.example", "demo-demo-1", "Demo", "demo")
    c = create_app(repo=repo).test_client()
    _login(c, "demo@t.example", "demo-demo-1")
    tok = _csrf(c, "/app")
    r = c.post("/app/s/x/layouts/A/sales", data=dict(csrf=tok), content_type="multipart/form-data")
    assert r.status_code in (403, 404)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
