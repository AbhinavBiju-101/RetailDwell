"""Recording start time and wall-clock labels. Run:  python -m tests.test_clock"""
import io
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import clock as wc
import simulate
import validation as val
from stores import STORES

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def test_start_times_are_read_in_the_store_timezone_unless_they_carry_an_offset():
    assert wc.parse_start("2026-10-10 14:00", "Asia/Kolkata", NOW) == datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
    assert wc.parse_start("2026-10-10T14:00", "UTC", NOW) == datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
    assert wc.parse_start("2026-10-10T14:00+05:30", "Europe/London", NOW) == datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
    assert wc.parse_start("2026-10-10T08:30:00Z", "Asia/Kolkata", NOW) == datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
    # daylight saving: London in summer is UTC+1
    assert wc.parse_start("2026-07-01 12:00", "Europe/London", NOW).hour == 11


def test_bad_start_times_are_refused_in_plain_words():
    for bad, word in (("yesterday", "not a date"), ("2026-10-20 10:00", "future"), ("1999-01-01 10:00", "plausible")):
        try:
            wc.parse_start(bad, "UTC", NOW)
            raise AssertionError("accepted " + bad)
        except ValueError as e:
            assert word in str(e), e
    try:
        wc.valid_timezone("Mars/Olympus")
        raise AssertionError("accepted")
    except ValueError:
        pass
    assert wc.valid_timezone("Asia/Kolkata") == "Asia/Kolkata"


def test_labels_use_clock_time_when_known_and_minutes_when_not():
    k = wc.Clock("2026-10-10T08:30:00+00:00", "Asia/Kolkata")          # Saturday 14:00 local
    assert k.window(5, 10) == "14:05 to 14:10" and k.span(5, 10) == "14:05 and 14:10 on Sat 10 Oct"
    assert k.start_label() == "Sat 10 Oct 14:00" and k.local_input() == "2026-10-10T14:00"
    u = wc.Clock(None, None)
    assert u.window(5, 10) == "5 to 10" and u.span(5, 10) == "minutes 5 and 10" and u.start_label() is None
    assert wc.Clock("garbage", "Not/AZone").known is False              # bad stored values never break a page
    assert wc.Clock("2026-10-10T08:30:00Z", "UTC").known is True


def test_timing_flags_speak_in_clock_time():
    tb = dict(windows=[dict(w=0, start_min=0, end_min=5, shoppers=40), dict(w=1, start_min=5, end_min=10, shoppers=40)],
              cells=[dict(zone="Produce", w=1, visitors=2, expected=12.0, shoppers=40, ratio=0.17, z=-4.0, flag="low")])
    plain = an.timing_flags(tb)[0]["message"]
    clocked = an.timing_flags(tb, span=wc.Clock("2026-10-10T08:30:00+00:00", "Asia/Kolkata").span)[0]["message"]
    assert "between minutes 5 and 10" in plain and "between 14:05 and 14:10 on Sat 10 Oct" in clocked


def test_till_timestamps_become_seconds_since_the_start():
    k = wc.Clock("2026-10-10T08:30:00+00:00", "Asia/Kolkata")
    assert k.seconds_since_start(["12.5", "300"]) == [12.5, 300.0]                  # already seconds: untouched
    assert k.seconds_since_start(["2026-10-10 14:01:30", "2026-10-10T08:35:00Z", "2026-10-10T14:10:00+05:30"]) == [90.0, 300.0, 600.0]
    assert k.seconds_since_start([str(1791621000 + 60)])[0] == 60.0 and \
        k.seconds_since_start([str((1791621000 + 60) * 1000)])[0] == 60.0            # epoch seconds and milliseconds
    for unknown in (wc.Clock(None, None),):
        try:
            unknown.seconds_since_start(["2026-10-10 14:01:30"])
            raise AssertionError("accepted a timestamp with no start")
        except ValueError as e:
            assert "start time" in str(e)
    try:
        k.seconds_since_start(["14:01"])
        raise AssertionError("accepted a bare time")
    except ValueError:
        pass


def test_sales_csv_with_real_timestamps_matches_the_seconds_version_and_drops_earlier_lines():
    k = wc.Clock("2026-10-10T08:30:00+00:00", "Asia/Kolkata")
    csv = b"txn_id,t,zone,amount\nA,2026-10-10 14:00:30,Produce,10\nB,2026-10-10 14:02:00,Produce,20\nOLD,2026-10-10 09:00:00,Produce,5\n"
    rep = {}
    df = val.pos_from_csv(csv, clock=k, report=rep)
    assert dict(zip(df.txn_id, df.t)) == {"A": 30.0, "B": 120.0} and rep["dropped_before_start"] == 1
    try:
        val.pos_from_csv(csv)                                                        # no start time known
        raise AssertionError("accepted")
    except val.ValidationError as e:
        assert "start time" in str(e)
    assert len(val.pos_from_csv(b"txn_id,t,zone,amount\nA,30,Produce,10\n")) == 1  # plain seconds still fine


def test_an_old_local_database_gains_the_new_columns_and_keeps_its_data():
    from repo_local import LocalRepo
    d = tempfile.mkdtemp()
    db = sqlite3.connect(os.path.join(d, "local.db"))
    db.executescript("create table stores (id text primary key, owner_id text, slug text unique, name text not null, kind text, blurb text, "
                     "width_m real not null, height_m real not null, window_s integer default 300, hero_zone text, plan_path text, "
                     "is_demo integer default 0, created_at text);"
                     "create table datasets (id text primary key, layout_id text not null, label text not null, source text not null, "
                     "storage_path text, n_points integer default 0, n_tracks integer default 0, observed_on text, created_at text, "
                     "updated_at text, unique (layout_id, label));"
                     "insert into stores (id, name, width_m, height_m) values ('s1', 'Old store', 10, 8);")
    db.commit(); db.close()
    repo = LocalRepo(d)
    cols = {r[1] for t in ("stores", "datasets") for r in repo.db.execute(f"pragma table_info({t})")}
    assert {"timezone", "started_at"} <= cols
    assert repo.db.execute("select name, timezone from stores").fetchone()[:] == ("Old store", "UTC")


def _login(c, email, pw):
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    c.post("/login", data=dict(email=email, password=pw, csrf=tok))


def _csrf(c, path):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).data.decode()).group(1)


def test_app_flow_timezone_start_time_wall_clock_labels_and_timestamped_sales():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    spec = STORES["corner-pharmacy"]
    st = repo.create_store(sess, dict(name="Pharm", width_m=spec["w"], height_m=spec["h"], window_s=600, hero_zone="Wellness"))
    df, lines, truth = simulate.generate_with_sales("corner-pharmacy")["A"]
    lay = repo.upsert_layout(sess, st["id"], "A", "Before", {n: list(z) for n, z in spec["layouts"]["A"]["zones"].items()})
    ds = repo.create_dataset(sess, lay["id"], "Day", "upload")
    repo.save_tracks(ds, st, df[["track_id", "t", "x", "y"]])
    c = create_app(repo=repo).test_client()
    _login(c, "o@t.example", "password-1")
    base = f"/app/s/{st['id']}"

    before = c.get(base + "/zones").data.decode()
    assert "Times are local" not in before                                       # no start time yet: minutes, as before

    tok = _csrf(c, base + "/settings")
    vals = dict(csrf=tok, name="Pharm", kind="Retail", width_m=spec["w"], height_m=spec["h"], window_s=600, timezone="Asia/Kolkata")
    assert c.post(base + "/settings/store", data=vals).status_code == 302
    assert repo.get_store(sess, st["id"])["timezone"] == "Asia/Kolkata"
    bad = c.post(base + "/settings/store", data=dict(vals, timezone="Mars/Olympus"))
    assert bad.status_code == 302 and repo.get_store(sess, st["id"])["timezone"] == "Asia/Kolkata"

    r = c.post(base + "/layouts/A/start", data=dict(csrf=tok, started_at="2026-10-10T14:00"))
    assert r.status_code == 302
    ds = repo.list_datasets(sess, [lay["id"]])[0]
    assert ds["started_at"].startswith("2026-10-10T08:30:00")
    zones = c.get(base + "/zones").data.decode()
    assert "Times are local (Asia/Kolkata)" in zones
    assert "14:00 to 14:30" in zones and "14:30 to 15:00" in zones                  # 30-minute windows from the 14:00 start
    assert "Start time" in c.get(base + "/settings").data.decode() and "Sat 10 Oct 14:00" in c.get(base + "/settings").data.decode()
    assert c.post(base + "/layouts/A/start", data=dict(csrf=tok, started_at="next week")).status_code == 302
    assert repo.list_datasets(sess, [lay["id"]])[0]["started_at"].startswith("2026-10-10T08:30:00")   # bad input changed nothing

    # a till export with real local timestamps lines up exactly like the seconds version
    start = datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
    stamped = lines.assign(t=[(start + timedelta(seconds=float(t))).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S.%f")
                              for t in lines.t])
    r = c.post(base + "/layouts/A/sales", data=dict(csrf=tok, file=(io.BytesIO(stamped.to_csv(index=False).encode()), "s.csv")),
               content_type="multipart/form-data")
    assert r.status_code == 302
    kept = repo.load_pos(repo.list_datasets(sess, [lay["id"]])[0], st)
    assert abs(kept.t.min() - lines.t.min()) < 0.2 and abs(kept.t.max() - lines.t.max()) < 0.2
    assert "How sales were matched" in c.get(base + "/revenue").data.decode()

    # the agent sends a start time with its points: the first writer sets it and nothing moves it afterwards
    _, key = repo.create_api_key(sess, st["id"], "Cam")
    h = {"Authorization": f"Bearer {key}"}
    pts = [dict(track_id=1, t=1.0, x=2.0, y=2.0), dict(track_id=1, t=1.5, x=2.1, y=2.0)]
    api = create_app(repo=repo).test_client()
    assert api.post("/api/ingest/v1/tracks", headers=h, json=dict(layout="A", recording="Live", points=pts, started_at="2026-10-11T09:00:00+00:00")).status_code == 202
    assert api.post("/api/ingest/v1/tracks", headers=h, json=dict(layout="A", recording="Live", points=pts, started_at="2026-10-11T10:00:00+00:00")).status_code == 202
    live = repo.svc_get_or_create_dataset(lay["id"], "Live", "ingest")
    assert live["started_at"].startswith("2026-10-11T09:00:00")
    assert api.post("/api/ingest/v1/tracks", headers=h, json=dict(layout="A", recording="Live2", points=pts, started_at="nonsense")).status_code == 202   # points are kept


def test_the_agent_sends_a_start_time_for_live_sources_only():
    from agent.cli import _started_at
    mk = lambda **k: SimpleNamespace(**{"started_at": None, "source": "x.mp4", **k})
    assert _started_at(mk()) is None and _started_at(mk(source="rtsp://cam/1")) is not None and _started_at(mk(source="0")) is not None
    assert _started_at(mk(started_at="2026-10-10T14:00+05:30")) == "2026-10-10T08:30:00+00:00"
    for bad in ("2026-10-10T14:00", "soon"):
        try:
            _started_at(mk(started_at=bad))
            raise AssertionError("accepted " + bad)
        except SystemExit:
            pass


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
