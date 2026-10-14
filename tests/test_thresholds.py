"""Per-store detection settings. Run:  python -m tests.test_thresholds"""
import os
import re
import tempfile
from types import SimpleNamespace

import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import config as C
import pos
import simulate
import thresholds as T
from stores import STORES
from tests.test_crowd import ZONES, _pair, _path, still, walk
from tests.test_pos import _csrf, _login


# ------------------------------------------------------------------ the table itself
def test_every_default_comes_from_config_and_sits_inside_its_own_range():
    d = T.defaults()
    assert d["staff_zone_min_s"] == C.STAFF_ZONE_MIN_S and d["party_dist_m"] == C.PARTY_DIST_M and d["stitch_max_gap_s"] == C.STITCH_MAX_GAP_S
    assert d["pos_tol_s"] == C.POS_TOL_S and d["pos_min_conf"] == C.POS_MIN_CONF
    assert len({s.key for s in T.SPECS}) == len(T.SPECS)
    for s in T.SPECS:
        if s.kind != "bool":
            assert s.lo <= s.default <= s.hi, s.key
        assert s.help and s.label and s.applies and s.group in T.GROUPS
    assert T.resolve({}) == d and T.resolve(None) == d


def test_the_agent_gets_exactly_the_stitching_settings():
    got = T.agent_settings({"stitch_max_gap_s": 15, "staff_zone_min_s": 99})
    assert set(got) == {"stitch", "stitch_max_gap_s", "stitch_base_m", "stitch_speed_mps", "stitch_min_gap_s", "stitch_edge_m"}
    assert got["stitch_max_gap_s"] == 15


def test_saving_keeps_only_changes_and_refuses_bad_values_naming_the_field():
    assert T.parse_form({}) == {} and T.parse_form({"t_party_dist_m": "", "t_stitch": "1"}) == {}
    assert T.parse_form({"t_party_dist_m": "2", "t_staff_filter": "0", "t_party_max_size": "4"}) == {"party_dist_m": 2.0, "staff_filter": False, "party_max_size": 4}
    for form, phrase in (({"t_party_dist_m": "9"}, "How close together must be between 0.5 and 3"),
                         ({"t_party_max_size": "3.5"}, "must be a whole number"),
                         ({"t_staff_zone_min_s": "abc"}, "must be a number"),
                         ({"t_pos_min_conf": "0.9"}, "Lowest confidence kept must be between"),
                         ({"t_stitch": "maybe"}, "must be on or off"),
                         ({"t_pos_tol_s": "nan"}, "must be a number")):
        try:
            T.parse_form(form)
            raise AssertionError("accepted " + str(form))
        except ValueError as e:
            assert phrase in str(e), (form, str(e))


def test_a_bad_stored_value_is_ignored_when_reading_never_an_error():
    eff = T.resolve({"party_dist_m": 99, "staff_zone_min_s": "x", "nonsense": 1, "stitch": "off", "party_max_size": 4})
    assert eff["party_dist_m"] == C.PARTY_DIST_M and eff["staff_zone_min_s"] == C.STAFF_ZONE_MIN_S
    assert eff["stitch"] is False and eff["party_max_size"] == 4 and "nonsense" not in eff


def test_describe_marks_what_differs_from_the_default():
    groups = dict(T.describe({"party_dist_m": 2.0}))
    row = next(r for r in groups["Parties"] if r["spec"].key == "party_dist_m")
    assert row["value"] == 2.0 and row["is_default"] is False
    assert all(r["is_default"] for r in groups["Staff"])


# ------------------------------------------------------------------ each setting changes behaviour the right way
def test_staff_rules_follow_the_settings():
    cashier = still(2, 100, 1000, (10, 2))                                     # 900 s at the register
    t = pd.concat([cashier, walk(5, 500, 700, (6, 7.9), (10, 2))])
    assert 2 in an.find_staff(t, ZONES)                                        # default 600 s
    assert an.find_staff(t, ZONES, {"staff_checkout_min_s": 1200}) == {}       # a longer bar: not staff
    shop = still(3, 0, 120, (10, 2))
    assert 3 in an.find_staff(shop, ZONES, {"staff_checkout_min_s": 120})      # a lower bar catches a normal shopper


def test_turning_staff_filter_off_keeps_staff_in_the_figures():
    shop = pd.concat([walk(100 + i, 60 * i, 60 * i + 90, (6, 7.9), (10, 2)) for i in range(6)])
    t = pd.concat([shop, still(1, 0, 900, (10.5, 3.5))])
    on = an.analyse_layout(t, ZONES, 12, 8, 300)
    off = an.analyse_layout(t, ZONES, 12, 8, 300, params={"staff_filter": False})
    assert 1 not in set(on["labels"].track_id) and 1 in set(off["labels"].track_id) and off["staff"] == {}


def test_party_settings_change_who_is_a_party():
    t = pd.concat([_pair(1, 2), _path(3, 0, gap=6.0)])                         # 1 and 2 are 0.9 m apart
    assert an.find_parties(t)["of"][1] == an.find_parties(t)["of"][2]
    assert an.find_parties(t, {"party_dist_m": 0.6})["of"][1] != an.find_parties(t, {"party_dist_m": 0.6})["of"][2]
    assert an.find_parties(t, {"party_min_s": 200})["of"][1] != an.find_parties(t, {"party_min_s": 200})["of"][2]
    R = an.analyse_layout(t, ZONES, 14, 8, 300, params={"parties_enabled": False})
    assert R["parties"]["sizes"] == {} and R["params"]["parties_enabled"] is False


def test_sales_settings_change_how_many_sales_are_matched():
    spec = STORES["corner-pharmacy"]
    df, lines, _ = simulate.generate_with_sales("corner-pharmacy")["B"]
    zones = {n: list(z) for n, z in spec["layouts"]["B"]["zones"].items()}
    def match(params):
        R = an.analyse_layout(df[["track_id", "t", "x", "y"]], zones, spec["w"], spec["h"], spec["window_s"], params=params)
        return pos.analyse_pos(R, lines)["match"]
    base, narrow, strict = match(None), match({"pos_tol_s": 6}), match({"pos_min_conf": 0.55})
    assert base["tol_s"] == 30 and narrow["tol_s"] == 6
    # a narrower window drops sales that really happened further from the shopper's exit, but leaves fewer rivals per sale
    assert narrow["n_unmatched_txn"] > base["n_unmatched_txn"] and narrow["n_ambiguous"] < base["n_ambiguous"]
    # a stricter confidence bar moves matches from usable to ambiguous without changing what is unmatched
    assert strict["n_usable"] < base["n_usable"] and strict["n_ambiguous"] > base["n_ambiguous"]
    assert strict["n_unmatched_txn"] == base["n_unmatched_txn"]


# ------------------------------------------------------------------ the pages and the agent
def _pharmacy_app():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    spec = STORES["corner-pharmacy"]
    st = repo.create_store(sess, dict(name="P", width_m=spec["w"], height_m=spec["h"], window_s=600, hero_zone="Wellness"))
    df, lines, _ = simulate.generate_with_sales("corner-pharmacy")["A"]
    lay = repo.upsert_layout(sess, st["id"], "A", "Before", {n: list(z) for n, z in spec["layouts"]["A"]["zones"].items()})
    ds = repo.create_dataset(sess, lay["id"], "Day", "upload")
    repo.save_tracks(ds, st, df[["track_id", "t", "x", "y"]])
    repo.save_pos(ds, st, lines)
    c = create_app(repo=repo).test_client()
    _login(c, "o@t.example", "password-1")
    return c, repo, sess, st, f"/app/s/{st['id']}"


def test_settings_page_saves_applies_at_once_resets_and_refuses_bad_input():
    c, repo, sess, st, base = _pharmacy_app()
    page = c.get(base + "/settings").data.decode()
    assert "Detection settings" in page and "Track stitching (camera agent)" in page and "Applies the next time a camera agent starts" in page
    ov = c.get(base + "/").data.decode()
    assert "left out as staff" in ov and "moved in" in ov
    tok = _csrf(c, base + "/settings")
    assert c.post(base + "/settings/detection", data=dict(csrf=tok, t_staff_filter="0", t_parties_enabled="0")).status_code == 302
    assert repo.get_store(sess, st["id"])["settings"] == {"staff_filter": False, "parties_enabled": False}
    ov2 = c.get(base + "/").data.decode()
    assert "left out as staff" not in ov2 and "moved in" not in ov2                   # applied to the existing recording at once
    assert "2 changed from the defaults" in c.get(base + "/settings").data.decode()
    c.post(base + "/settings/detection", data=dict(csrf=tok, t_party_dist_m="50"))
    assert repo.get_store(sess, st["id"])["settings"] == {"staff_filter": False, "parties_enabled": False}   # refused: unchanged
    assert "must be between 0.5 and 3" in c.get(base + "/settings").data.decode()
    c.post(base + "/settings/detection", data=dict(csrf=tok, reset="1"))
    assert repo.get_store(sess, st["id"])["settings"] == {} and "left out as staff" in c.get(base + "/").data.decode()


def test_the_agent_endpoint_needs_a_key_and_returns_this_stores_stitching_settings():
    c, repo, sess, st, base = _pharmacy_app()
    from app import create_app
    api = create_app(repo=repo).test_client()
    assert api.get("/api/ingest/v1/config").status_code == 401
    _, key = repo.create_api_key(sess, st["id"], "Cam")
    h = {"Authorization": f"Bearer {key}"}
    assert api.get("/api/ingest/v1/config", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/api/ingest/v1/config", headers=h).get_json()["settings"] == T.agent_settings({})
    repo.update_store(sess, st["id"], {"settings": {"stitch_max_gap_s": 15.0, "party_dist_m": 2.0}})
    got = api.get("/api/ingest/v1/config", headers=h).get_json()["settings"]
    assert got["stitch_max_gap_s"] == 15.0 and "party_dist_m" not in got                # the agent is told nothing else


class FakeHttp:
    def __init__(self, status=200, body=None, boom=None):
        self.status, self.body, self.boom, self.calls = status, body, boom, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        if self.boom:
            raise self.boom
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)


def test_the_agent_uses_server_settings_and_falls_back_to_defaults_when_it_cannot():
    from agent.cli import fetch_server_settings
    from agent.pipeline import Pipeline
    h = FakeHttp(200, dict(settings=dict(stitch_max_gap_s=15.0, stitch=True, surprise=1)))
    got = fetch_server_settings("https://app.example/", "KEY", http=h)
    assert got == {"stitch_max_gap_s": 15.0, "stitch": True}                           # unknown keys are dropped
    assert h.calls[0][0] == "https://app.example/api/ingest/v1/config" and h.calls[0][1]["Authorization"] == "Bearer KEY"
    assert fetch_server_settings("u", "k", http=FakeHttp(500)) == {} and fetch_server_settings("u", "k", http=FakeHttp(401)) == {}
    assert fetch_server_settings("u", "k", http=FakeHttp(boom=ConnectionError("down"))) == {}
    p = Pipeline(SimpleNamespace(floor_size=(12.0, 8.0)), SimpleNamespace(gives_ids=True), SimpleNamespace(add=lambda *a: None, count=0, close=lambda: None),
                 **T.agent_settings({"stitch_max_gap_s": 15.0, "stitch_base_m": 1.0}))
    assert p.stitch_max_gap == 15.0 and p.stitch_base == 1.0 and p.stitch is True


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
