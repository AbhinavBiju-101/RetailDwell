"""Pooling several recordings of one layout. Run:  python -m tests.test_pooling"""
import os
import re
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import pooling
import pos
import simulate
from stores import STORES
from tests.test_pos import _csrf, _login

SK, LK = "lakeside-grocery", "A"
STARTS = ["2026-10-03T05:30:00+00:00", "2026-10-04T05:30:00+00:00", "2026-10-05T05:30:00+00:00"]    # 11:00 local, three days
_DAYS = {}


def days(seeds=(1, 11, 21), sk=SK, lk=LK):
    """Simulated days of one store layout: the same store, different random shoppers."""
    key = (tuple(seeds), sk, lk)
    if key not in _DAYS:
        lay = STORES[sk]["layouts"][lk]
        old, out = lay["seed"], []
        try:
            for s in seeds:
                lay["seed"] = s
                df, lines, _t = simulate.generate_with_sales(sk)[lk]
                out.append((df[["track_id", "t", "x", "y"]], lines))
        finally:
            lay["seed"] = old
        _DAYS[key] = out
    return _DAYS[key]


def zones_of(sk=SK, lk=LK):
    return {n: list(z) for n, z in STORES[sk]["layouts"][lk]["zones"].items()}


def single(df, sk=SK, lk=LK):
    sp = STORES[sk]
    return an.analyse_layout(df, zones_of(sk, lk), sp["w"], sp["h"], sp["window_s"])


# ------------------------------------------------------------------ the pool itself
def test_pooled_days_never_overlap_and_keep_every_track_and_sale_distinct():
    d = days()
    recs = [dict(label=f"D{i}", tracks=df, lines=ln, started_at=STARTS[i]) for i, (df, ln) in enumerate(d)]
    P = pooling.pool(recs, 1800, "Asia/Kolkata")
    t = P["tracks"]
    assert t.track_id.nunique() == sum(df.track_id.nunique() for df, _ in d) and P["n"] == 3 and P["undated"] == 0
    spans = [(g.t.min(), g.t.max()) for _, g in t.groupby(t.track_id // pooling.ID_STRIDE)]
    assert all(spans[i][1] + 1000 < spans[i + 1][0] for i in range(2))                 # an hour apart at least
    assert P["lines"].txn_id.nunique() == sum(ln.txn_id.nunique() for _, ln in d)
    assert P["lines"].txn_id.str.startswith(("0:", "1:", "2:")).all()


def test_hours_line_up_across_days_that_start_at_different_times():
    df = days()[0][0]
    recs = [dict(label="am", tracks=df, lines=None, started_at="2026-10-03T04:00:00+00:00"),        # 09:30 local
            dict(label="pm", tracks=df, lines=None, started_at="2026-10-04T08:30:00+00:00")]        # 14:00 local
    P = pooling.pool(recs, 1800, "Asia/Kolkata")
    slot, mod = P["slot_fn"], P["minute_of_day_fn"]
    second_start = df.t.max() + pooling.GAP_S
    assert int(mod(np.array([0.0]))[0]) == 9 * 60 + 30 and int(slot(np.array([0.0]))[0]) == 19
    assert int(mod(np.array([second_start]))[0]) == 14 * 60 and int(slot(np.array([second_start]))[0]) == 28
    late = pooling.clock_functions([0.0], [23 * 60 + 45], 1800)                                    # runs past midnight
    assert int(late[1](np.array([30 * 60.0]))[0]) == 15 and int(late[0](np.array([30 * 60.0]))[0]) == 0
    assert pooling.hhmm(14 * 60 + 5) == "14:05" and pooling.hhmm(1450) == "00:10"


def test_a_recording_with_no_start_time_switches_the_pooled_clock_off():
    recs = [dict(label="a", tracks=days()[0][0], lines=None, started_at=STARTS[0]),
            dict(label="b", tracks=days()[1][0], lines=None, started_at=None)]
    P = pooling.pool(recs, 1800, "Asia/Kolkata")
    assert P["undated"] == 1 and P["slot_fn"] is None and P["minute_of_day_fn"] is None


# ------------------------------------------------------------------ the analysis
def _pooled(n=3, with_clock=True, sales=True):
    d = days()[:n]
    recs = [dict(label=f"D{i}", tracks=df, lines=ln if sales else None, started_at=STARTS[i] if with_clock else None) for i, (df, ln) in enumerate(d)]
    P = pooling.pool(recs, 1800, "Asia/Kolkata")
    sp = STORES[SK]
    R = an.analyse_layout(P["tracks"], zones_of(), sp["w"], sp["h"], sp["window_s"],
                          pooled=dict(n=P["n"], slot_fn=P["slot_fn"], minute_of_day_fn=P["minute_of_day_fn"]))
    R["clock"] = None
    return R, P, d


def test_pooled_visits_add_up_exactly_and_no_days_are_merged_into_parties():
    R, P, d = _pooled()
    singles = [single(df) for df, _ in d]
    assert R["stats"].attrs["n_visits"] == sum(s["stats"].attrs["n_visits"] for s in singles)
    assert R["stats"].attrs["n_shoppers"] == sum(s["stats"].attrs["n_shoppers"] for s in singles)
    assert sum(1 for v in R["parties"]["sizes"].values() if v > 1) <= 2                        # no cross-day "parties"
    reach = lambda r: r["stats"].set_index("zone").visit_share
    mean_single = sum(reach(s) * s["stats"].attrs["n_shoppers"] for s in singles) / sum(s["stats"].attrs["n_shoppers"] for s in singles)
    assert (reach(R) - mean_single).abs().max() < 1e-9                                         # pooled share = shopper-weighted average


def test_pooled_time_windows_are_clock_hours_with_more_shoppers_each():
    R, _, d = _pooled()
    one = single(d[0][0])["timing"]
    tb = R["timing"]
    assert tb["reason"] is None and [w["start_min"] for w in tb["windows"]] == [660, 690, 720, 750]      # 11:00 to 13:00
    assert min(w["shoppers"] for w in tb["windows"]) > 2 * min(w["shoppers"] for w in one["windows"])


def test_without_start_times_the_hour_findings_are_switched_off_with_a_reason():
    R, _, _ = _pooled(with_clock=False)
    assert R["timing"]["windows"] == [] and "needs a start time" in R["timing"]["reason"]
    assert R["stats"].attrs["n_visits"] > 0                                                      # everything else still works


def test_pooled_sales_match_like_the_days_do_separately():
    R, P, d = _pooled()
    Pp = pos.analyse_pos(R, P["lines"])
    separate = [pos.analyse_pos(dict(single(df)), ln)["match"]["n_usable"] for df, ln in d]
    assert abs(Pp["match"]["n_usable"] - sum(separate)) <= 0.03 * sum(separate)
    assert Pp["match"]["n_txn"] == sum(ln.txn_id.nunique() for _, ln in d)


def test_pooled_checkout_flag_names_the_hour_not_a_minute_on_a_made_up_timeline():
    from app import checkout_where
    R, _, _ = _pooled()
    R["pooled"] = dict(n=3, clock=True)
    chk = an.flag_checkout(R["checkout"], where=checkout_where(R))
    assert chk is None or re.search(r"around \d\d:\d\d \(on one of 3 days\)", chk["message"])
    assert (R["checkout"].window_start_min.between(0, 1440)).all()


# ------------------------------------------------------------------ the app
def _app(starts=STARTS, n=3):
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    spec = STORES[SK]
    st = repo.create_store(sess, dict(name="Lakeside", width_m=spec["w"], height_m=spec["h"], window_s=300, hero_zone=spec["hero"]))
    repo.update_store(sess, st["id"], {"timezone": "Asia/Kolkata"})
    st = repo.get_store(sess, st["id"])
    lay = repo.upsert_layout(sess, st["id"], LK, "Before", zones_of())
    made = []
    for i, (df, ln) in enumerate(days()[:n]):
        ds = repo.create_dataset(sess, lay["id"], f"Day {i + 1}", "upload")
        repo.save_tracks(ds, st, df)
        repo.save_pos(ds, st, ln)
        if starts[i]:
            repo.set_dataset_start(ds, starts[i])
        made.append(ds)
    c = create_app(repo=repo).test_client()
    _login(c, "o@t.example", "password-1")
    return c, repo, sess, st, lay, made, f"/app/s/{st['id']}"


def _visits(html):
    return int(re.search(r"(\d+)\s*</dd>\s*<dt>Visits", html).group(1)) if re.search(r"(\d+)\s*</dd>\s*<dt>Visits", html) else None


def test_pages_show_latest_by_default_one_recording_or_all_days_pooled():
    c, repo, sess, st, lay, made, base = _app()
    d = days()
    n = [single(df)["stats"].attrs["n_visits"] for df, _ in d]
    latest = c.get(base + "/").data.decode()
    assert "pooled together" not in latest and f">{n[2]}<" in latest.replace(" ", "")          # newest = Day 3
    one = c.get(base + f"/?rec={made[0]['id']}").data.decode()
    assert "Showing one recording, not the latest" in one and f">{n[0]}<" in one.replace(" ", "")
    allp = c.get(base + "/?rec=all").data.decode()
    assert "Showing all 3 recordings of this layout pooled together" in allp and f">{sum(n)}<" in allp.replace(" ", "")
    assert f">{n[2]}<" in c.get(base + "/?rec=not-an-id").data.decode().replace(" ", "")          # junk falls back to latest
    assert 'name="rec"' in latest and "All days pooled (3)" in latest


def test_the_api_calls_follow_the_chosen_recordings_and_the_zones_page_shows_pooled_hours():
    c, repo, sess, st, lay, made, base = _app()
    z_all = c.get(f"/api/s/{st['id']}/zones/A?rec=all").get_json()
    z_one = c.get(f"/api/s/{st['id']}/zones/A").get_json()
    assert z_all != z_one
    page = c.get(base + "/zones?rec=all").data.decode()
    assert "pooled over 3 days" in page and "11:00 to 11:30" in page and "12:30 to 13:00" in page
    js = open("static/app.js").read()
    assert "URLSearchParams(location.search).get(\"rec\")" in js


def test_undated_recordings_warn_and_switch_hours_off_in_the_app():
    c, *_rest = _app(starts=[STARTS[0], None, STARTS[2]])
    base = _rest[-1]
    page = c.get(base + "/zones?rec=all").data.decode()
    assert "no start time" in page and "every pooled recording needs a start time" in page


def test_one_recording_pooled_is_just_that_recording():
    c, repo, sess, st, lay, made, base = _app(n=1)
    html = c.get(base + "/?rec=all").data.decode()
    assert 'name="rec"' not in html                                                              # nothing to choose between
    assert f">{single(days()[0][0])['stats'].attrs['n_visits']}<" in html.replace(" ", "")


def test_the_experiment_can_use_all_days_for_both_layouts():
    c, repo, sess, st, lay, made, base = _app()
    spec = STORES[SK]
    lay_b = repo.upsert_layout(sess, st["id"], "B", "After", zones_of(SK, "B"))
    lay_old = spec["layouts"]["B"]["seed"]
    try:
        for i, s in enumerate((2, 12)):
            spec["layouts"]["B"]["seed"] = s
            df, ln, _t = simulate.generate_with_sales(SK)["B"]
            ds = repo.create_dataset(sess, lay_b["id"], f"B day {i + 1}", "upload")
            repo.save_tracks(ds, st, df[["track_id", "t", "x", "y"]])
            repo.save_pos(ds, st, ln)
            repo.set_dataset_start(ds, STARTS[i])
    finally:
        spec["layouts"]["B"]["seed"] = lay_old
    one = c.get(base + "/experiment").data.decode()
    pooled = c.get(base + "/experiment?rec=all").data.decode()
    width = lambda h: (lambda m: int(m.group(2)) - int(m.group(1)))(re.search(r"Likely range ([+-]?\d+)% to ([+-]?\d+)%", h))
    assert pooled != one
    assert width(pooled) < width(one)                       # more shoppers behind each layout: a tighter range on the same question


def test_settings_lists_recordings_and_deleting_one_removes_it_and_its_files():
    c, repo, sess, st, lay, made, base = _app()
    page = c.get(base + "/settings").data.decode()
    assert "Recordings (3)" in page and "Day 1" in page and "Sat 3 Oct 11:00" in page
    old = made[0]
    ds0 = repo.list_datasets(sess, [lay["id"]])
    victim = next(d for d in ds0 if d["label"] == "Day 1")
    files = [victim["storage_path"], repo._path("pos", st["id"], f"{victim['id']}.csv.gz")]
    assert all(os.path.exists(f) for f in files)
    tok = _csrf(c, base + "/settings")
    assert c.post(base + f"/recordings/{victim['id']}/delete", data=dict(csrf=tok)).status_code == 302
    assert not any(os.path.exists(f) for f in files)
    assert [d["label"] for d in repo.list_datasets(sess, [lay["id"]])] == ["Day 3", "Day 2"]
    assert "All days pooled (2)" in c.get(base + "/").data.decode()
    assert c.post(base + "/recordings/nope/delete", data=dict(csrf=tok)).status_code == 404


def test_only_owners_can_delete_recordings():
    from app import create_app
    c, repo, sess, st, lay, made, base = _app()
    repo.ensure_user("demo@t.example", "demo-demo-1", "Demo", "demo")
    other = create_app(repo=repo).test_client()
    _login(other, "demo@t.example", "demo-demo-1")
    tok = _csrf(other, "/app")
    r = other.post(base + f"/recordings/{made[0]['id']}/delete", data=dict(csrf=tok))
    assert r.status_code in (403, 404) and len(repo.list_datasets(sess, [lay["id"]])) == 3


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
