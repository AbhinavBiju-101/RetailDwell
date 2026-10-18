"""RetailDwell - Flask app.

Public pages: landing (/), sign in, sign up.
Signed-in pages: store picker (/app) and, per store, overview, experiment, zones, flags, journeys,
and settings (floor plan, zone editor, recordings, API keys).
Cameras post anonymous positions to POST /api/ingest/v1/tracks with a store API key.

Run:  python app.py                     (local SQLite store if no Supabase keys are set)
Prod: gunicorn -w 1 --threads 4 "app:create_app()"
"""
import base64
import functools
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import OrderedDict, defaultdict, deque
from urllib.parse import urlencode

import numpy as np
from dotenv import load_dotenv
from flask import (Flask, abort, has_request_context, flash, g, jsonify, make_response, redirect, render_template,
                   request, session, url_for)

load_dotenv()

import analytics as an  # noqa: E402
import pos as sales  # noqa: E402
import clock as wallclock  # noqa: E402
import brief as brief_mod  # noqa: E402
import thresholds  # noqa: E402
import pooling  # noqa: E402
import fusion  # noqa: E402
import flow as zoneflow  # noqa: E402
import whatif as whatif_model  # noqa: E402
from config import TIME_WINDOW_S, FLOW_MIN_EDGE, FLOW_MIN_N, MIN_PRESENCE_S, WHATIF_CATCH_M, WHATIF_ERR_MEDIAN_PTS, WHATIF_ERR_80_PTS, WHATIF_SWAP_ERR_MEDIAN_PTS, WHATIF_SWAP_ERR_80_PTS  # noqa: E402
import validation as val  # noqa: E402
from repo import get_repo  # noqa: E402
from repo_base import AuthError, Forbidden, NotFound, RepoError, store_ref  # noqa: E402
from stores import STORES, ZONE_TYPES  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.environ.get("BRAND_NAME", "RetailDwell")
TAGLINE = os.environ.get("BRAND_TAGLINE", "Retail dwellscape intelligence")
log = logging.getLogger("dwell")
LOITERER = an.LOITERER
SEGMENTS = ["directed", "exploratory", LOITERER, "pass-through"]
NAV = [("overview", "Overview"), ("experiment", "Experiment"), ("zones", "Zones"),
       ("revenue", "Revenue"), ("flags", "Flags"), ("journeys", "Journeys"), ("whatif", "What-if"), ("brief", "Brief")]
CURRENCY = os.environ.get("CURRENCY_SYMBOL", "\u20b9")
TONE = {"Bottleneck": "bad", "Dead zone": "bad", "Visibility problem": "warn",
        "Quieter than usual": "warn", "Busier than usual": "info",
        "Pass-through": "info", "Destination": "info", "Hot zone": "good",
        "Healthy": "good", "OK": "good",
        "Offer problem": "warn", "Hidden earner": "info", "Data check": "warn", "Turn-back": "warn"}
TONE_RANK = {"bad": 0, "warn": 1, "info": 2, "good": 3}
PLAN_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))
MAX_PLAN_BYTES = 5 * 1024 * 1024


def clean_json(o):
    """Replace NaN and numpy values so the result is valid JSON."""
    if isinstance(o, dict):
        return {k: clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (math.isnan(o) or math.isinf(o)) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def fusion_summary(reps):
    """One report for several recordings: counts add up; the calibration gap is the median across days."""
    errs = [r["overlap_error_m"] for r in reps if r["overlap_error_m"] is not None]
    return dict(cameras=max(r["cameras"] for r in reps), enabled=all(r["enabled"] for r in reps),
                tracks_in=sum(r["tracks_in"] for r in reps), tracks_out=sum(r["tracks_out"] for r in reps),
                overlap_merges=sum(r["overlap_merges"] for r in reps), handoffs=sum(r["handoffs"] for r in reps),
                overlap_pairs=sum(r["overlap_pairs"] for r in reps), overlap_error_m=float(np.median(errs)) if errs else None)


def journey_filters(R):
    """What the Journeys page can narrow to: zones to have stopped in, and time windows (not for pooled days without clock times)."""
    pooled = R.get("pooled")
    view = timing_view(R["timing"], R.get("clock"), pooled)
    windows = []
    if view.get("windows") and (not pooled or pooled.get("clock")):
        windows = [dict(w=int(x["w"]), label=v["label"], shoppers=int(x["shoppers"]))
                   for x, v in zip(R["timing"]["windows"], view["windows"])]
    sizes = (R.get("parties") or {}).get("sizes", {})
    in_party = [t for t, pid in (R.get("parties") or {}).get("of", {}).items() if sizes.get(pid, 1) >= 2]
    return dict(zones=sorted(R["zones"]), windows=windows, parties=dict(n_parties=sum(1 for n in sizes.values() if n >= 2), n_visits=len(in_party)))


def journey_ids(R, visited, w, party=None):
    """Track ids matching the Journeys filters, or None when no filter is set. Raises ValueError for a value that is not on offer."""
    ids = None
    if party not in (None, "", "alone", "party"):
        raise ValueError("unknown party filter")
    if visited:
        if visited not in R["zones"]:
            raise ValueError("unknown zone")
        ids = zoneflow.ids_stopping_in(R, visited)
    if party:
        pa = R.get("parties") or {}
        members = {int(t) for t, pid in pa.get("of", {}).items() if pa.get("sizes", {}).get(pid, 1) >= 2}
        grp = members if party == "party" else {int(t) for t in R["labels"].track_id} - members
        ids = grp if ids is None else ids & grp
    if w is not None:
        if w not in {x["w"] for x in journey_filters(R)["windows"]}:
            raise ValueError("unknown time window")
        slots = an.visit_slots(R["df"], R["timing"]["window_s"], R.get("slot_fn"))
        in_w = set(slots[slots == w].index.astype(int))
        ids = in_w if ids is None else ids & in_w
    return ids


def checkout_where(R):
    """How the checkout flag says when: clock time when it is known, otherwise minutes since the recording started."""
    P, clk = R.get("pooled"), R.get("clock")
    if P and P.get("clock"):
        return lambda m: f"{pooling.hhmm(m)} (on one of {P['n']} days)"
    if P:
        return lambda m: f"a time on one of {P['n']} days"
    if clk and clk.known:
        return lambda m: clk.local(m).strftime("%H:%M")
    return None


def span_fn(R):
    P, clk = R.get("pooled"), R.get("clock")
    if P and P.get("clock"):
        return lambda a, b: f"{pooling.hhmm(a)} and {pooling.hhmm(b)}, pooled over {P['n']} days"
    return clk.span if clk else None


def timing_view(tb, clk=None, pooled=None):
    """Zone x window grid for the zones page: text and flag per cell, so colour is never the only signal."""
    if not tb["windows"]:
        return dict(reason=tb["reason"], windows=[], rows=[])
    by = {(c["zone"], c["w"]): c for c in tb["cells"]}
    zones = list(dict.fromkeys(c["zone"] for c in tb["cells"]))
    rows = []
    for z in zones:
        cells = []
        for w in tb["windows"]:
            c = by[(z, w["w"])]
            pct = None if c["ratio"] is None else round((c["ratio"] - 1) * 100)
            cells.append(dict(text="-" if pct is None else f"{pct:+d}%", flag=c["flag"],
                              title=f"{c['visitors']} reached it, about {c['expected']:.0f} expected from {c['shoppers']} shoppers"))
        rows.append(dict(zone=z, cells=cells))
    if pooled and pooled.get("clock"):
        return dict(reason=None, clock=True, tz=None, note=f"Hours of the day, pooled over {pooled['n']} days: each column gathers the shoppers from every day in that hour.",
                    windows=[dict(label=f"{pooling.hhmm(w['start_min'])} to {pooling.hhmm(w['end_min'])}", shoppers=w["shoppers"])
                             for w in tb["windows"]], rows=rows)
    known = bool(clk and clk.known)
    return dict(reason=None, clock=known, note=None, tz=clk.tz_name if known else None,
                windows=[dict(label=(clk.window(w["start_min"], w["end_min"]) if known else f"{w['start_min']} to {w['end_min']} min"),
                              shoppers=w["shoppers"]) for w in tb["windows"]], rows=rows)


def parse_start_or_400(text, store):
    try:
        return wallclock.iso(wallclock.parse_start(text, store.get("timezone") or wallclock.DEFAULT_TZ))
    except ValueError as e:
        raise val.ValidationError(str(e))


def money(v, digits=0):
    return "n/a" if v is None or v != v else f"{CURRENCY}{v:,.{digits}f}"


def records(df):
    return json.loads(df.to_json(orient="records"))


def center(z):
    return ((z[0] + z[2]) / 2, (z[1] + z[3]) / 2)


def zone_geo(zones):
    return {n: dict(x0=z[0], y0=z[1], x1=z[2], y1=z[3], type=z[4]) for n, z in zones.items()}


def has_merch(layout):
    return any(z[4] == "merchandise" for z in layout["zones"].values())


def findings(R):
    out = [dict(kind=f.kind, tone=TONE.get(f.kind, "info"), text=f.message)
           for _, f in R["flags"].iterrows() if f.kind != "Healthy"]
    out += [dict(kind=f["kind"], tone=TONE[f["kind"]], text=f["message"]) for f in an.timing_flags(R["timing"], span=span_fn(R))]
    chk = an.flag_checkout(R["checkout"], where=checkout_where(R))
    if chk:
        out.append(dict(kind=chk["kind"], tone=TONE[chk["kind"]], text=chk["message"]))
    out += [dict(kind=f["kind"], tone=TONE.get(f["kind"], "info"), text=f["text"])
            for f in (R.get("pos") or {}).get("items", [])]
    out += [dict(kind=f["kind"], tone=TONE["Turn-back"], text=f["text"], zone=f["zone"])
            for f in (R.get("flow") or {}).get("findings", [])]
    out.sort(key=lambda f: TONE_RANK[f["tone"]])
    return out


class Cache:
    """Small in-memory LRU. One computation per key even when several requests ask at once."""

    def __init__(self, cap):
        self.cap, self.data, self.lock, self.locks = cap, OrderedDict(), threading.Lock(), {}

    def get(self, key, fn):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
            klock = self.locks.setdefault(key, threading.Lock())
        with klock:
            with self.lock:
                if key in self.data:
                    return self.data[key]
            value = fn()
            with self.lock:
                self.data[key] = value
                while len(self.data) > self.cap:
                    self.data.popitem(last=False)
                self.locks.pop(key, None)
            return value


    def drop(self, key):
        with self.lock:
            self.data.pop(key, None)


class Throttle:
    """Allow `n` events per `window` seconds for a key (used for sign-in attempts)."""

    def __init__(self, n, window):
        self.n, self.window, self.hits, self.lock = n, window, defaultdict(deque), threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


def create_app(repo=None, brief_service=None):
    app = Flask(__name__)
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:
        secret = secrets.token_hex(32)
        log.warning("SECRET_KEY is not set. Using a random key, so everyone is signed out on restart.")
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=56 * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
        TEMPLATES_AUTO_RELOAD=False,
    )
    repo = repo or get_repo(os.path.join(BASE, "data", "local"))
    app.config["REPO"] = repo
    analyses, comparisons = Cache(24), Cache(256)
    login_throttle = Throttle(10, 600)
    pos_lock = threading.Lock()
    brief_svc = brief_service or brief_mod.BriefService()
    app.jinja_env.filters["money"] = money
    demo_email = os.environ.get("DEMO_EMAIL", "").strip()
    demo_password = os.environ.get("DEMO_PASSWORD", "")

    # Local development: create a demo account and demo stores on first start.
    if repo.mode == "local":
        if not demo_email or not demo_password:
            demo_email, demo_password = "demo@dwellmapper.local", "demo-demo-1"
            os.environ["DEMO_EMAIL"], os.environ["DEMO_PASSWORD"] = demo_email, demo_password
        if os.environ.get("AUTO_SEED", "1") == "1":
            import seed_demo
            seed_demo.ensure_demo_user(repo, log=log.info)
            if not repo.has_demo_stores():
                log.info("Creating demo stores (about a minute, first start only).")
                seed_demo.seed(repo, log=log.info)

    # ------------------------------------------------------------------ session, CSRF, headers
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def safe_next(target):
        if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
            return target
        return None

    @app.before_request
    def load_user():
        g.user = None
        u = session.get("u")
        if not u:
            return
        try:
            fresh = repo.fresh(u)
        except AuthError:
            session.pop("u", None)
            return
        if fresh is not u and fresh != u:
            session["u"] = fresh
        g.user = session["u"]

    @app.before_request
    def check_csrf():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/api/ingest/"):
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "The form expired. Reload the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if g.get("user"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def login_required(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                if request.path.startswith("/api/"):
                    return jsonify(error="Sign in required."), 401
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return fn(*a, **kw)
        return wrapper

    def writer_required(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if g.user["role"] == "demo":
                abort(403, "The demo account is read-only.")
            return fn(*a, **kw)
        return wrapper

    @app.errorhandler(RepoError)
    def repo_error(e):
        code = 404 if isinstance(e, NotFound) else 403 if isinstance(e, Forbidden) else 400
        if request.path.startswith("/api/"):
            return jsonify(error=str(e)), code
        return render_template("error.html", title="Problem", code=code, message=str(e)), code

    @app.errorhandler(val.ValidationError)
    def validation_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error=str(e)), 400
        flash(str(e), "bad")
        return redirect(request.referrer or url_for("stores"))

    for code, text in ((400, "That request could not be used."), (403, "You do not have access to that."),
                       (404, "That page does not exist."), (413, "That file is too large.")):
        def make(code=code, text=text):
            def handler(e):
                msg = getattr(e, "description", None)
                msg = msg if msg and msg != "Bad Request" and len(str(msg)) < 200 and code in (400, 403) else text
                if request.path.startswith("/api/"):
                    return jsonify(error=msg), code
                return render_template("error.html", title="Problem", code=code, message=msg), code
            return handler
        app.register_error_handler(code, make())

    @app.errorhandler(500)
    def server_error(e):
        log.exception("Unhandled error")
        if request.path.startswith("/api/"):
            return jsonify(error="Something went wrong on our side."), 500
        return render_template("error.html", title="Problem", code=500,
                               message="Something went wrong on our side. Try again in a moment."), 500

    @app.context_processor
    def inject():
        return dict(brand=BRAND, tagline=TAGLINE, user=g.get("user"), csrf_token=csrf_token, nav=NAV, endpoint=request.endpoint,
                    can_edit=bool(g.get("user")) and g.user["role"] != "demo",
                    demo_one_click=bool(os.environ.get("DEMO_ONE_CLICK") == "1" and demo_email and demo_password),
                    google_enabled=google_enabled(), backend=repo.mode)

    # ------------------------------------------------------------------ store context
    def load_ctx(ref, need_edit=False, sess=None):
        sess = sess or g.user
        store = repo.get_store(sess, ref)
        if not store:
            abort(404)
        if need_edit and (g.user["role"] == "demo" or store.get("is_demo") or store["owner_id"] != g.user["user_id"]):
            abort(403, "You cannot change this store.")
        layouts = repo.list_layouts(sess, store["id"])
        datasets = repo.list_datasets(sess, [l["id"] for l in layouts])
        latest, every = {}, {}
        for d in datasets:                      # newest first
            latest.setdefault(d["layout_id"], d)
            every.setdefault(d["layout_id"], []).append(d)
        for l in layouts:
            l["dataset"] = latest.get(l["id"])
            l["datasets"] = every.get(l["id"], [])
            l["ready"] = bool(l["dataset"] and l["dataset"]["n_points"] and has_merch(l))
        return store, layouts

    def layout_by_key(layouts, key):
        for l in layouts:
            if l["key"] == key:
                return l
        return None

    def scope_of(layout):
        """Which recordings to analyse: 'latest', 'all' (pooled), or one recording's id. From ?rec=."""
        if not has_request_context():
            return "latest"
        rec = request.args.get("rec") or "latest"
        if rec == "all":
            return "all"
        if rec != "latest" and any(d["id"] == rec and d["n_points"] for d in layout.get("datasets", [])):
            return rec
        return "latest"

    def exp_scope():
        return "all" if has_request_context() and request.args.get("rec") == "all" else "latest"

    def analyse(store, layout, scope=None):
        scope = scope or scope_of(layout)
        ready = [d for d in layout.get("datasets", []) if d["n_points"]]
        pooled_ds = list(reversed(ready[:pooling.MAX_POOL])) if scope == "all" and len(ready) >= 2 else None   # oldest first
        if pooled_ds:
            ds_key = tuple((d["id"], d["n_points"], d.get("updated_at")) for d in pooled_ds)
        else:
            ds = next((d for d in ready if d["id"] == scope), None) or layout["dataset"]
            ds_key = (ds["id"], ds["n_points"], ds.get("updated_at"))
        zones = layout["zones"]
        window = int(store.get("window_s") or 300)
        zkey = hash(json.dumps(zones, sort_keys=True))
        params = thresholds.resolve(store.get("settings"))
        key = (ds_key, zkey, store["width_m"], store["height_m"], window, json.dumps(params, sort_keys=True), store.get("timezone"))
        zdict = {n: list(z) for n, z in zones.items()}

        def build():
            if pooled_ds:
                recs, freps = [], []
                for d in pooled_ds:
                    try:
                        lines = repo.load_pos(d, store)
                    except Exception:
                        log.exception("Sales data could not be read for dataset %s", d["id"])
                        lines = None
                    tr, frep = fusion.fuse(repo.load_tracks(d, store), params)        # per recording, before the days are pooled
                    freps.append(frep)
                    recs.append(dict(label=d["label"], tracks=tr, lines=lines, started_at=d.get("started_at")))
                P = pooling.pool(recs, TIME_WINDOW_S, store.get("timezone"))
                R = an.analyse_layout(P["tracks"], zdict, float(store["width_m"]), float(store["height_m"]), window, params=params,
                                      pooled=dict(n=P["n"], slot_fn=P["slot_fn"], minute_of_day_fn=P["minute_of_day_fn"]))
                R["pooled"] = dict(n=P["n"], labels=P["labels"], undated=P["undated"], clock=P["slot_fn"] is not None,
                                   capped=len(ready) > pooling.MAX_POOL, of=len(ready))
                R["slot_fn"] = P["slot_fn"]
                R["clock"] = wallclock.Clock(None, store.get("timezone"))
                R["fusion"] = fusion_summary(freps)
                lines_all = P["lines"]
            else:
                tracks, frep = fusion.fuse(repo.load_tracks(ds, store), params)
                R = an.analyse_layout(tracks, zdict, float(store["width_m"]), float(store["height_m"]), window, params=params)
                R["fusion"] = frep
                R["clock"] = wallclock.Clock.for_recording(ds, store)
                R["recording"] = ds["label"]
                lines_all = None
            try:                                  # zone flow is a side view: a failure here must not break the other pages
                R["flow"] = zoneflow.analyse_flow(R)
            except Exception:
                log.exception("Zone flow could not be worked out for layout %s", layout["key"])
                R["flow"] = None
            R["pos"], R["pos_error"] = None, False
            try:                                  # sales are optional: a failure here must not break the other pages
                R["pos"] = sales.analyse_pos(R, lines_all if pooled_ds else repo.load_pos(ds, store))
            except Exception:
                log.exception("Sales data could not be read for layout %s", layout["key"])
                R["pos_error"] = True
            return R
        R = analyses.get(key, build)
        if R.get("pos_error"):
            analyses.drop(key)                    # try again next time rather than hiding the sales for good
        return R, key

    def pick_layout(store, layouts, key):
        """The layout named by ?layout=, else the first one with data."""
        l = layout_by_key(layouts, (key or "").upper()) if key else None
        if not l:
            l = next((x for x in layouts if x["ready"]), layouts[0] if layouts else None)
        return l

    def page(template, store, layouts, layout=None, **kw):
        ready = [d for d in (layout or {}).get("datasets", []) if d["n_points"]]
        return render_template(template, store=store, ref=store_ref(store), layouts=layouts,
                               layout=layout, ready_layouts=[l for l in layouts if l["ready"]],
                               rec=scope_of(layout) if layout else "latest", rec_list=ready, pool_n=min(len(ready), pooling.MAX_POOL), **kw)

    # ------------------------------------------------------------------ public pages and auth
    @app.get("/")
    def landing():
        return render_template("landing.html", title="Know where shoppers stop")

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", storage=repo.mode)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("stores"))
        nxt = safe_next(request.values.get("next"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            if not login_throttle.allow(request.remote_addr or "?"):
                flash("Too many attempts. Wait a few minutes and try again.", "bad")
            else:
                try:
                    sess = repo.sign_in(val.clean_email(email), request.form.get("password", ""))
                    return start_session(sess, nxt)
                except (AuthError, val.ValidationError) as e:
                    flash(str(e) if isinstance(e, AuthError) else "Enter a valid email address.", "bad")
        return render_template("login.html", title="Sign in", next=nxt)

    def start_session(sess, nxt=None):
        csrf = session.get("csrf")
        session.clear()
        session.permanent = True
        session["u"] = sess
        session["csrf"] = csrf or secrets.token_urlsafe(24)
        return redirect(nxt or url_for("stores"))

    def google_enabled():
        return bool(repo.supports_oauth and os.environ.get("GOOGLE_SIGNIN") == "1")

    def app_url():
        return (os.environ.get("APP_URL") or request.host_url).rstrip("/")

    @app.get("/auth/google")
    def auth_google():
        """Start sign-in with Google through Supabase (PKCE). The verifier lives only in this browser's session, so a
        callback link that did not start here cannot be completed."""
        if not google_enabled():
            abort(404)
        if not login_throttle.allow((request.remote_addr or "?") + ":oauth"):
            flash("Too many attempts. Wait a few minutes and try again.", "bad")
            return redirect(url_for("login"))
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        session["pkce"] = dict(v=verifier, next=safe_next(request.args.get("next")))
        return redirect(repo.oauth_start("google", app_url() + url_for("auth_callback"), challenge))

    @app.get("/auth/callback")
    def auth_callback():
        if not google_enabled():
            abort(404)
        state = session.pop("pkce", None)
        code = request.args.get("code", "")
        if request.args.get("error") or not code:
            flash("Google sign-in was cancelled or did not complete. Try again, or use email and password.", "bad")
            return redirect(url_for("login"))
        if not state:
            flash("That sign-in link has expired or was not started here. Start again from the sign-in page.", "bad")
            return redirect(url_for("login"))
        try:
            sess = repo.oauth_finish(code, state["v"])
        except AuthError:
            flash("Google sign-in could not be completed. Try again, or use email and password.", "bad")
            return redirect(url_for("login"))
        return start_session(sess, state.get("next"))

    @app.post("/demo")
    def demo():
        if not (os.environ.get("DEMO_ONE_CLICK") == "1" and demo_email and demo_password):
            abort(404)
        if not login_throttle.allow((request.remote_addr or "?") + ":demo"):
            abort(400, "Too many attempts. Wait a few minutes and try again.")
        return start_session(repo.sign_in(demo_email, demo_password))

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if g.user:
            return redirect(url_for("stores"))
        if request.method == "POST":
            try:
                email = val.clean_email(request.form.get("email", ""))
                name = val.clean_text(request.form.get("name", ""), "name", 60)
                val.check_password(request.form.get("password", ""))
                if not login_throttle.allow((request.remote_addr or "?") + ":signup"):
                    raise AuthError("Too many attempts. Wait a few minutes and try again.")
                res = repo.sign_up(email, request.form.get("password", ""), name)
                if res.get("needs_confirm") or not res.get("session"):
                    flash("Account created. Confirm your email with the link we sent, then sign in.", "good")
                    return redirect(url_for("login"))
                return start_session(res["session"])
            except (AuthError, val.ValidationError) as e:
                flash(str(e), "bad")
        return render_template("signup.html", title="Create account")

    @app.post("/logout")
    def logout():
        if g.user:
            try:
                repo.sign_out(g.user)
            except RepoError:
                pass
        session.clear()
        return redirect(url_for("landing"))

    # ------------------------------------------------------------------ store picker and creation
    @app.get("/app")
    @login_required
    def stores():
        rows = repo.list_stores(g.user)
        for s in rows:
            s["ref"] = store_ref(s)
        return render_template("stores.html", title="Your stores", stores=rows)

    @app.route("/app/stores/new", methods=["GET", "POST"])
    @login_required
    @writer_required
    def store_new():
        if request.method == "POST":
            w = val.clean_number(request.form.get("width_m"), "width", 3, 200)
            h = val.clean_number(request.form.get("height_m"), "depth", 3, 200)
            s = repo.create_store(g.user, dict(
                name=val.clean_text(request.form.get("name", ""), "store name", 60),
                kind=val.clean_text(request.form.get("kind", "") or "Retail", "store type", 30),
                width_m=w, height_m=h, window_s=300))
            repo.upsert_layout(g.user, s["id"], "A", "Current layout", {})
            flash("Store created. Add a floor plan and draw the zones next.", "good")
            return redirect(url_for("zone_editor", ref=store_ref(s), key="A"))
        return render_template("store_new.html", title="Add a store")

    # ------------------------------------------------------------------ per-store pages
    @app.get("/app/s/<ref>/")
    @login_required
    def overview(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="Overview", active="overview")
        R, _ = analyse(store, layout)
        st, lab = R["stats"], R["labels"]
        merch = st[st.type == "merchandise"]
        mix = lab.mission.value_counts()
        chk = an.flag_checkout(R["checkout"], where=checkout_where(R))
        ws = an.wait_summary(R["checkout"])
        kpi = dict(
            visits=int(st.attrs["n_visits"]), shoppers=int(st.attrs["n_shoppers"]),
            avg_visit_min=float(lab.visit_s.mean() / 60), loiterer_pct=float((lab.mission == LOITERER).mean()),
            peak_wait=float(chk["est_wait_min"]) if chk else None,
            median_wait=ws["median"] if ws else None,
            busiest=merch.loc[merch.visit_share.idxmax()].zone, busiest_share=float(merch.visit_share.max()),
            quietest=merch.loc[merch.visit_share.idxmin()].zone, quietest_share=float(merch.visit_share.min()),
            rev=(R["pos"]["kpi"]["revenue_per_shopper"] if R.get("pos") else None))
        segs = [dict(name=s, n=int(mix.get(s, 0)), pct=float(mix.get(s, 0) / max(len(lab), 1))) for s in SEGMENTS]
        sizes = (R.get("parties") or {}).get("sizes", {})
        fz = R.get("fusion") or {}
        cams = None
        if fz.get("cameras", 1) > 1:
            limit = (R.get("params") or {}).get("fuse_dist_m", 0.8)
            err = fz.get("overlap_error_m")
            cams = dict(n=fz["cameras"], enabled=fz["enabled"], merges=fz["overlap_merges"], handoffs=fz["handoffs"], err=err,
                        tracks_in=fz["tracks_in"], tracks_out=fz["tracks_out"], limit=limit, close=bool(err is not None and err > 0.6 * limit))
        crowd = dict(cameras=cams, staff=sorted(set((R.get("staff") or {}).values())), n_staff=len(R.get("staff") or {}),
                     n_parties=sum(1 for v in sizes.values() if v > 1), in_parties=sum(v for v in sizes.values() if v > 1))
        return page("overview.html", store, layouts, layout, title="Overview", active="overview",
                    kpi=kpi, segs=segs, items=findings(R), crowd=crowd)

    @app.get("/app/s/<ref>/experiment")
    @login_required
    def experiment(ref):
        store, layouts = load_ctx(ref)
        ready = [l for l in layouts if l["ready"]]
        if len(ready) < 2:
            return page("empty.html", store, layouts, None, title="Experiment", active="experiment",
                        need_two=True)
        la = layout_by_key(ready, (request.args.get("a") or "").upper()) or ready[0]
        lb = layout_by_key(ready, (request.args.get("b") or "").upper())
        if not lb or lb is la:
            lb = next(l for l in ready if l is not la)
        common = [n for n, z in la["zones"].items() if z[4] == "merchandise"
                  and n in lb["zones"] and lb["zones"][n][4] == "merchandise"]
        if not common:
            return page("empty.html", store, layouts, None, title="Experiment", active="experiment",
                        no_common=True, pair=(la, lb))
        zone = request.args.get("zone") or store.get("hero_zone")
        if zone not in common:
            zone = common[0]
        Ra, ka = analyse(store, la, exp_scope())
        Rb, kb = analyse(store, lb, exp_scope())
        stop = comparisons.get((ka, kb, zone, "stop"), lambda: an.compare_layouts(Ra, Rb, zone, "stop", n_boot=1500))
        reach = comparisons.get((ka, kb, zone, "reach"), lambda: an.compare_layouts(Ra, Rb, zone, "reach", n_boot=1500))
        ca, cb = center(la["zones"][zone]), center(lb["zones"][zone])
        moved = math.hypot(ca[0] - cb[0], ca[1] - cb[1])
        wa, wb = an.wait_summary(Ra["checkout"]), an.wait_summary(Rb["checkout"])

        def verdict(c):
            if math.isnan(c["lift"]) or math.isnan(c["ci_low"]):
                return ("flat", "Not enough data to say.")
            if c["ci_low"] > 0:
                return ("up", "The change likely increased it.")
            if c["ci_high"] < 0:
                return ("down", "The change likely reduced it.")
            return ("flat", "No clear effect. The range includes zero.")
        rev_all = comparisons.get((ka, kb, "rev"), lambda: sales.compare_revenue(Ra, Rb, None))
        rev_zone = comparisons.get((ka, kb, zone, "rev"), lambda: sales.compare_revenue(Ra, Rb, zone))
        sa = Ra["stats"][Ra["stats"].zone == zone].iloc[0]
        sb = Rb["stats"][Rb["stats"].zone == zone].iloc[0]
        return page("experiment.html", store, layouts, None, title="Experiment", active="experiment",
                    la=la, lb=lb, zone=zone, zones=common, stop=stop, reach=reach,
                    vstop=verdict(stop), vreach=verdict(reach), moved=moved, sa=sa, sb=sb,
                    shoppers_a=int(Ra["stats"].attrs["n_shoppers"]), shoppers_b=int(Rb["stats"].attrs["n_shoppers"]),
                    wait_a=wa, wait_b=wb, show_switch=False, rev_all=rev_all, rev_zone=rev_zone,
                    vrev=verdict(rev_all) if rev_all else None, vrevz=verdict(rev_zone) if rev_zone else None,
                    chart=clean_json(dict(
                        stop=dict(lift=stop["lift"], lo=stop["ci_low"], hi=stop["ci_high"]),
                        reach=dict(lift=reach["lift"], lo=reach["ci_low"], hi=reach["ci_high"]),
                        zone=zone, a=la["key"], b=lb["key"], wait_a=wa, wait_b=wb)))

    @app.get("/app/s/<ref>/zones")
    @login_required
    def zones(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="Zones", active="zones")
        R, _ = analyse(store, layout)
        return page("zones.html", store, layouts, layout, title="Zones", active="zones",
                    rows=records(R["stats"]), n_shoppers=int(R["stats"].attrs["n_shoppers"]),
                    timing=timing_view(R["timing"], R.get("clock"), R.get("pooled")))

    @app.get("/app/s/<ref>/revenue")
    @login_required
    def revenue(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="Revenue", active="revenue")
        R, _ = analyse(store, layout)
        P = R.get("pos")
        chart = None
        if P:
            z = [r for r in P["zones"]]
            chart = clean_json(dict(zones=[r["zone"] for r in z],
                                    reached=[round(r["share_reached"] * 100, 1) for r in z],
                                    stopped=[round(r["share_stopped"] * 100, 1) for r in z],
                                    bought=[round(r["share_bought"] * 100, 1) for r in z]))
        items = [dict(kind=f["kind"], tone=TONE.get(f["kind"], "info"), text=f["text"]) for f in P["items"]] if P else []
        return page("revenue.html", store, layouts, layout, title="Revenue", active="revenue", P=P, items=items,
                    pos_error=bool(R.get("pos_error")), chart=chart,
                    unmapped_revenue=(float(P["lines"][P["lines"].zone.isin(P["unmapped"])].amount.sum()) if P else 0.0))

    def experiment_facts(store, layouts):
        """The before/after result for the hero zone, in the same terms as the Experiment page, or None."""
        ready = [l for l in layouts if l["ready"]]
        zone = store.get("hero_zone")
        if len(ready) < 2 or not zone:
            return None
        la, lb = ready[0], ready[1]
        if not all(zone in l["zones"] and l["zones"][zone][4] == "merchandise" for l in (la, lb)):
            return None
        Ra, ka = analyse(store, la, exp_scope())
        Rb, kb = analyse(store, lb, exp_scope())
        stop = comparisons.get((ka, kb, zone, "stop"), lambda: an.compare_layouts(Ra, Rb, zone, "stop", n_boot=1500))
        rev = comparisons.get((ka, kb, zone, "rev"), lambda: sales.compare_revenue(Ra, Rb, zone))
        pct = lambda v: None if v is None or v != v else int(round(v * 100))
        verdict_of = lambda c: ("not enough data to say" if math.isnan(c["lift"]) or math.isnan(c["ci_low"]) else
                                "the change likely increased it" if c["ci_low"] > 0 else
                                "the change likely reduced it" if c["ci_high"] < 0 else "no clear effect (the range includes zero)")
        out = dict(zone=zone, before=la["label"], after=lb["label"], shoppers_before=int(Ra["stats"].attrs["n_shoppers"]),
                   shoppers_after=int(Rb["stats"].attrs["n_shoppers"]),
                   stopped_share_change_pct=pct(stop["lift"]), stopped_share_range_pct=[pct(stop["ci_low"]), pct(stop["ci_high"])],
                   stopped_share_verdict=verdict_of(stop))
        if rev:
            out.update(zone_revenue_per_shopper_change_pct=pct(rev["lift"]), zone_revenue_range_pct=[pct(rev["ci_low"]), pct(rev["ci_high"])],
                       zone_revenue_verdict=verdict_of(rev))
        return out

    def brief_facts(store, layouts, layout, R):
        items = findings(R)
        chk = an.wait_summary(R["checkout"])
        return brief_mod.build_facts(store, layout["label"], R, items, wait=chk, experiment=experiment_facts(store, layouts), currency=CURRENCY)

    @app.get("/app/s/<ref>/brief")
    @login_required
    def brief(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="Brief", active="brief")
        R, _ = analyse(store, layout)
        facts = brief_facts(store, layouts, layout, R)
        result = brief_svc.cached(store["id"], facts) or brief_svc.template(
            facts, note=None if brief_svc.available() else "Set GEMINI_API_KEY on the server to have Gemini write this summary.")
        warn = session.pop("brief_warning", None)
        if warn:
            result = dict(result, warning=warn)
        return page("brief.html", store, layouts, layout, title="Brief", active="brief", result=result, facts=facts,
                    ai_available=brief_svc.available(), remaining=brief_svc.remaining(store["id"]),
                    limit=brief_svc.cfg()["daily_limit"], model=brief_svc.cfg()["model"], is_demo=bool(store.get("is_demo")))

    @app.post("/app/s/<ref>/brief/write")
    @login_required
    @writer_required
    def brief_write(ref):
        store, layouts = load_ctx(ref, need_edit=True)
        layout = pick_layout(store, layouts, request.form.get("layout"))
        if not layout or not layout["ready"]:
            abort(404)
        R, _ = analyse(store, layout)
        facts = brief_facts(store, layouts, layout, R)
        result = brief_svc.write(store["id"], facts, force=bool(request.form.get("again")), is_demo=bool(store.get("is_demo")))
        if result["source"] != "gemini":
            session["brief_warning"] = result.get("warning") or result.get("note")
        return redirect(url_for("brief", ref=ref, layout=layout["key"]))

    @app.get("/app/s/<ref>/flags")
    @login_required
    def flags(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="Flags", active="flags")
        R, _ = analyse(store, layout)
        return page("flags.html", store, layouts, layout, title="Flags", active="flags", items=findings(R),
                    healthy=[f.message for _, f in R["flags"].iterrows() if f.kind == "Healthy"],
                    has_checkout=len(R["checkout"]) > 0)

    @app.get("/app/s/<ref>/journeys")
    @login_required
    def journeys(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="Journeys", active="journeys")
        return page("journeys.html", store, layouts, layout, title="Journeys", active="journeys", segments=SEGMENTS,
                    flow_min_edge=FLOW_MIN_EDGE, flow_min_n=FLOW_MIN_N)

    @app.get("/app/s/<ref>/plan")
    @login_required
    def plan_image(ref):
        store, _ = load_ctx(ref)
        got = repo.get_plan(store)
        if not got:
            abort(404)
        resp = make_response(got[0])
        resp.headers["Content-Type"] = got[1]
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp

    # ------------------------------------------------------------------ data API (as the signed-in user)
    def api_layout(ref, key):
        store, layouts = load_ctx(ref)
        layout = layout_by_key(layouts, (key or "").upper())
        if not layout or not layout["ready"]:
            abort(404)
        R, _ = analyse(store, layout)
        return store, layout, R

    @app.get("/api/s/<ref>/heatmap/<key>")
    @login_required
    def api_heatmap(ref, key):
        store, layout, R = api_layout(ref, key)
        w, h = float(store["width_m"]), float(store["height_m"])
        H, xe, ye = an.heatmap(R["df"], w, h)
        n = max(int(R["stats"].attrs["n_visits"]), 1)
        per_visit = request.args.get("per_visit") == "1"
        if per_visit:
            H = H / n
        return jsonify(clean_json(dict(
            z=np.round(H, 3).tolist(), x=np.round((xe[:-1] + xe[1:]) / 2, 2).tolist(),
            y=np.round((ye[:-1] + ye[1:]) / 2, 2).tolist(),
            unit="seconds per visit" if per_visit else "person-seconds", n_visits=n,
            zones=zone_geo(layout["zones"]), store=dict(w=w, h=h))))

    @app.get("/api/s/<ref>/revenue-map/<key>")
    @login_required
    def api_revenue_map(ref, key):
        store, layout, R = api_layout(ref, key)
        P = R.get("pos")
        if not P:
            abort(404)
        w, h = float(store["width_m"]), float(store["height_m"])
        Z, xs, ys = sales.revenue_map(R, P, w, h)
        return jsonify(clean_json(dict(
            z=np.round(Z, 1).tolist(), x=np.round(xs, 2).tolist(), y=np.round(ys, 2).tolist(),
            unit="spend per shopper", currency=CURRENCY, average=P["kpi"]["revenue_per_shopper"],
            zones=zone_geo(layout["zones"]), store=dict(w=w, h=h))))

    @app.get("/api/s/<ref>/zones/<key>")
    @login_required
    def api_zones(ref, key):
        store, layout, R = api_layout(ref, key)
        return jsonify(clean_json(dict(zones=zone_geo(layout["zones"]), stats=records(R["stats"]))))

    @app.get("/api/s/<ref>/checkout/<key>")
    @login_required
    def api_checkout(ref, key):
        store, layout, R = api_layout(ref, key)
        cm = R["checkout"]
        rows = records(cm[cm.valid]) if len(cm) else []
        return jsonify(clean_json(dict(windows=rows, threshold_min=4, window_s=int(store.get("window_s") or 300))))

    @app.get("/api/s/<ref>/journeys/<key>")
    @login_required
    def api_journeys(ref, key):
        store, layout, R = api_layout(ref, key)
        seg = request.args.get("segment", "all")
        n = max(1, min(request.args.get("n", 15, type=int), 40))
        seed = request.args.get("seed", 1, type=int)
        lab = R["labels"]
        if seg != "all":
            if seg not in SEGMENTS:
                abort(400)
            lab = lab[lab.mission == seg]
        try:
            only = journey_ids(R, request.args.get("visited") or None, request.args.get("w", type=int), request.args.get("party") or None)
        except ValueError:
            abort(400)
        total = int(len(lab))
        if only is not None:
            lab = lab[lab.track_id.isin(only)]
        pick = lab.sample(min(n, len(lab)), random_state=seed) if len(lab) else lab
        groups = dict(tuple(R["df"][R["df"].track_id.isin(pick.track_id)].groupby("track_id")))
        out = []
        for _, row in pick.iterrows():
            gr = groups[row.track_id].sort_values("t")
            out.append(dict(id=int(row.track_id), mission=row.mission, confidence=float(row.confidence),
                            x=gr.x.rolling(5, min_periods=1, center=True).mean().round(2).tolist(),
                            y=gr.y.rolling(5, min_periods=1, center=True).mean().round(2).tolist()))
        return jsonify(clean_json(dict(paths=out, available=int(len(lab)), of_segment=total, filters=journey_filters(R), zones=zone_geo(layout["zones"]),
                                       store=dict(w=float(store["width_m"]), h=float(store["height_m"])))))

    @app.get("/app/s/<ref>/whatif")
    @login_required
    def whatif(ref):
        store, layouts = load_ctx(ref)
        layout = pick_layout(store, layouts, request.args.get("layout"))
        if not layout or not layout["ready"]:
            return page("empty.html", store, layouts, layout, title="What-if", active="whatif")
        merch = [n for n, z in layout["zones"].items() if z[4] == "merchandise"]
        return page("whatif.html", store, layouts, layout, title="What-if", active="whatif", merch=merch,
                    presence_s=int(MIN_PRESENCE_S), catch_m=WHATIF_CATCH_M,
                    err_median=WHATIF_ERR_MEDIAN_PTS, err_80=WHATIF_ERR_80_PTS,
                    swap_err_median=WHATIF_SWAP_ERR_MEDIAN_PTS, swap_err_80=WHATIF_SWAP_ERR_80_PTS)

    @app.get("/api/s/<ref>/whatif/<key>/scan")
    @login_required
    def api_whatif_scan(ref, key):
        store, layout, R = api_layout(ref, key)
        try:
            out = whatif_model.scan(R, request.args.get("zone", ""))
        except whatif_model.WhatIfError as e:
            return jsonify(error=str(e)), 422
        out["zones"] = zone_geo(layout["zones"])
        out["store"] = dict(w=float(store["width_m"]), h=float(store["height_m"]))
        return jsonify(clean_json(out))

    @app.get("/api/s/<ref>/whatif/<key>/swap")
    @login_required
    def api_whatif_swap(ref, key):
        store, layout, R = api_layout(ref, key)
        try:
            out = whatif_model.swap(R, request.args.get("a", ""), request.args.get("b", ""))
        except whatif_model.WhatIfError as e:
            return jsonify(error=str(e)), 422
        return jsonify(clean_json(out))

    @app.get("/api/s/<ref>/whatif/<key>")
    @login_required
    def api_whatif(ref, key):
        store, layout, R = api_layout(ref, key)
        try:
            cx, cy = request.args.get("cx", type=float), request.args.get("cy", type=float)
            if cx is None or cy is None:
                abort(400)
            out = whatif_model.what_if(R, request.args.get("zone", ""), cx=cx, cy=cy)
        except whatif_model.WhatIfError as e:
            return jsonify(error=str(e)), 422
        return jsonify(clean_json(out))

    @app.get("/api/s/<ref>/flow/<key>")
    @login_required
    def api_flow(ref, key):
        store, layout, R = api_layout(ref, key)
        seg = request.args.get("segment", "shoppers")
        if seg not in zoneflow.SEGMENTS:
            abort(400)
        try:
            only = journey_ids(R, request.args.get("visited") or None, request.args.get("w", type=int), request.args.get("party") or None)
        except ValueError:
            abort(400)
        F = R.get("flow") if (seg == "shoppers" and only is None) else None
        if F is None:
            F = zoneflow.analyse_flow(R, seg, only)
        return jsonify(clean_json(F))

    # ------------------------------------------------------------------ settings
    @app.get("/app/s/<ref>/settings")
    @login_required
    @writer_required
    def settings(ref):
        store, layouts = load_ctx(ref, need_edit=True)
        keys = repo.list_api_keys(g.user, store["id"])
        new_key = session.pop("new_key", None)
        starts = {l["key"]: wallclock.Clock.for_recording(l["dataset"], store) for l in layouts}
        rec_starts = {d["id"]: wallclock.Clock.for_recording(d, store).start_label() for l in layouts for d in l["datasets"]}
        return page("settings.html", store, layouts, None, title="Settings", active="settings",
                    keys=keys, new_key=new_key, host=request.host_url.rstrip("/"), starts=starts, rec_starts=rec_starts,
                    detection=thresholds.describe(store.get("settings")), changed=len(store.get("settings") or {}),
                    zone_names=wallclock.COMMON_ZONES, tz=store.get("timezone") or wallclock.DEFAULT_TZ)

    @app.get("/app/s/<ref>/agent")
    @login_required
    @writer_required
    def agent_setup(ref):
        store, layouts = load_ctx(ref, need_edit=True)
        keys = [k for k in repo.list_api_keys(g.user, store["id"]) if not k.get("revoked_at")]
        sel = layout_by_key(layouts, (request.args.get("layout") or "").upper()) or (layouts[0] if layouts else None)
        return page("agent.html", store, layouts, sel, title="Camera agent", active="agent",
                    keys=keys, host=request.host_url.rstrip("/"))

    @app.post("/app/s/<ref>/settings/store")
    @login_required
    @writer_required
    def settings_store(ref):
        store, layouts = load_ctx(ref, need_edit=True)
        vals = dict(
            name=val.clean_text(request.form.get("name", ""), "store name", 60),
            kind=val.clean_text(request.form.get("kind", "") or "Retail", "store type", 30),
            width_m=val.clean_number(request.form.get("width_m"), "width", 3, 200),
            height_m=val.clean_number(request.form.get("height_m"), "depth", 3, 200),
            window_s=int(val.clean_number(request.form.get("window_s"), "queue window", 60, 1800)))
        tz = (request.form.get("timezone") or "").strip()
        if tz and tz != (store.get("timezone") or wallclock.DEFAULT_TZ):   # only touch the column when it changes
            try:
                vals["timezone"] = wallclock.valid_timezone(tz)
            except ValueError as e:
                raise val.ValidationError(str(e))
        for l in layouts:   # shrinking the floor must not strand existing zones
            val.clean_zones(l["zones"], vals["width_m"], vals["height_m"])
        repo.update_store(g.user, store["id"], vals)
        flash("Store details saved.", "good")
        return redirect(url_for("settings", ref=ref))

    @app.post("/app/s/<ref>/settings/plan")
    @login_required
    @writer_required
    def settings_plan(ref):
        store, _ = load_ctx(ref, need_edit=True)
        f = request.files.get("plan")
        data = f.read(MAX_PLAN_BYTES + 1) if f else b""
        if not data:
            raise val.ValidationError("Choose an image file first.")
        if len(data) > MAX_PLAN_BYTES:
            raise val.ValidationError("The floor plan image must be under 5 MB.")
        ctype = next((t for magic, t in PLAN_TYPES if data.startswith(magic)), None)
        if not ctype:
            raise val.ValidationError("Use a PNG or JPEG image of the floor plan.")
        repo.save_plan(store, data, ctype)
        flash("Floor plan uploaded.", "good")
        return redirect(request.referrer or url_for("settings", ref=ref))

    @app.post("/app/s/<ref>/settings/delete")
    @login_required
    @writer_required
    def settings_delete(ref):
        store, _ = load_ctx(ref, need_edit=True)
        if request.form.get("confirm", "").strip() != store["name"]:
            raise val.ValidationError("Type the store name exactly to delete it.")
        repo.delete_store(g.user, store["id"])
        flash("Store deleted.", "good")
        return redirect(url_for("stores"))

    @app.post("/app/s/<ref>/layouts/new")
    @login_required
    @writer_required
    def layout_new(ref):
        store, layouts = load_ctx(ref, need_edit=True)
        key = val.clean_layout_key(request.form.get("key"))
        if layout_by_key(layouts, key):
            raise val.ValidationError(f"Layout {key} already exists.")
        label = val.clean_text(request.form.get("label", "") or f"Layout {key}", "layout name", 40)
        src = layout_by_key(layouts, request.form.get("copy_from", ""))
        repo.upsert_layout(g.user, store["id"], key, label, dict(src["zones"]) if src else {})
        return redirect(url_for("zone_editor", ref=ref, key=key))

    @app.post("/app/s/<ref>/layouts/<key>/delete")
    @login_required
    @writer_required
    def layout_delete(ref, key):
        store, layouts = load_ctx(ref, need_edit=True)
        l = layout_by_key(layouts, key)
        if not l:
            abort(404)
        repo.delete_layout(g.user, store["id"], l["id"])
        flash(f"Layout {key} deleted.", "good")
        return redirect(url_for("settings", ref=ref))

    @app.route("/app/s/<ref>/layouts/<key>/zones", methods=["GET", "POST"])
    @login_required
    @writer_required
    def zone_editor(ref, key):
        store, layouts = load_ctx(ref, need_edit=True)
        layout = layout_by_key(layouts, key)
        if not layout:
            abort(404)
        w, h = float(store["width_m"]), float(store["height_m"])
        if request.method == "POST":
            try:
                raw = json.loads(request.form.get("zones", "{}"))
            except ValueError:
                raise val.ValidationError("The zone list could not be read. Reload the page and try again.")
            zones_in = {}
            for row in raw if isinstance(raw, list) else []:
                name = val.clean_text(str(row.get("name", "")), "zone name", 40)
                if name in zones_in:
                    raise val.ValidationError(f'Two zones are called "{name}". Names must be different.')
                zones_in[name] = [row.get("x0"), row.get("y0"), row.get("x1"), row.get("y1"), row.get("type")]
            cleaned = val.clean_zones(zones_in, w, h)
            label = val.clean_text(request.form.get("label", "") or layout["label"], "layout name", 40)
            repo.upsert_layout(g.user, store["id"], key, label, cleaned)
            flash("Zones saved.", "good")
            return redirect(url_for("zone_editor", ref=ref, key=key))
        rows = [dict(name=n, x0=z[0], y0=z[1], x1=z[2], y1=z[3], type=z[4]) for n, z in layout["zones"].items()]
        return page("zone_editor.html", store, layouts, layout, title="Zone editor", active="settings",
                    rows=rows, zone_types=ZONE_TYPES, has_plan=bool(store.get("plan_path")))

    @app.post("/app/s/<ref>/layouts/<key>/upload")
    @login_required
    @writer_required
    def upload_recording(ref, key):
        store, layouts = load_ctx(ref, need_edit=True)
        layout = layout_by_key(layouts, key)
        if not layout:
            abort(404)
        f = request.files.get("file")
        data = f.read(val.MAX_UPLOAD_BYTES + 1) if f else b""
        if not data:
            raise val.ValidationError("Choose a CSV file first.")
        df = val.tracks_from_csv(data, float(store["width_m"]), float(store["height_m"]))
        label = val.clean_text(request.form.get("label", "") or f"Upload {time.strftime('%Y-%m-%d')}", "recording name", 60)
        ds = repo.create_dataset(g.user, layout["id"], label, "upload")
        repo.save_tracks(ds, store, df)
        started = (request.form.get("started_at") or "").strip()
        if started:
            repo.set_dataset_start(ds, parse_start_or_400(started, store))
        flash(f"Recording saved: {len(df):,} points from {df.track_id.nunique():,} people. Only track_id, t, x and y were kept.", "good")
        return redirect(url_for("overview", ref=ref, layout=key))

    @app.post("/app/s/<ref>/recordings/<dataset_id>/delete")
    @login_required
    @writer_required
    def delete_recording(ref, dataset_id):
        store, layouts = load_ctx(ref, need_edit=True)
        for l in layouts:
            for d in l["datasets"]:
                if d["id"] == dataset_id:
                    repo.delete_dataset(d, store)
                    flash(f"Recording \"{d['label']}\" and its sales file were deleted.", "good")
                    return redirect(url_for("settings", ref=ref) + "#s-lay")
        abort(404)

    @app.post("/app/s/<ref>/settings/detection")
    @login_required
    @writer_required
    def settings_detection(ref):
        store, _ = load_ctx(ref, need_edit=True)
        try:
            new = {} if request.form.get("reset") else thresholds.parse_form(request.form)
        except ValueError as e:
            raise val.ValidationError(str(e))
        if new != (store.get("settings") or {}):                      # only touch the column when something changed
            try:
                repo.update_store(g.user, store["id"], {"settings": new})
            except RepoError as e:
                raise val.ValidationError(f"The settings could not be saved. If you use Supabase, run supabase/migrations/20261012000000_settings.sql first. ({e})")
        flash("Detection settings reset to the defaults." if request.form.get("reset") else
              "Detection settings saved. Staff, party and sales settings apply to existing recordings now; stitching applies the next time a camera agent starts.", "good")
        return redirect(url_for("settings", ref=ref) + "#detection")

    @app.get("/api/ingest/v1/config")
    def ingest_config():
        """What a camera agent should use: the stitching settings for this store."""
        auth = request.headers.get("Authorization", "")
        found = repo.resolve_api_key(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
        if not found:
            return jsonify(error="That API key is not valid or has been revoked."), 401
        store = repo.svc_get_store(found["store_id"])
        if not store:
            return jsonify(error="That key cannot be used."), 403
        return jsonify(settings=thresholds.agent_settings(store.get("settings")))

    @app.post("/app/s/<ref>/layouts/<key>/start")
    @login_required
    @writer_required
    def set_recording_start(ref, key):
        store, layouts = load_ctx(ref, need_edit=True)
        layout = layout_by_key(layouts, key)
        if not layout or not layout["dataset"]:
            abort(404)
        text = (request.form.get("started_at") or "").strip()
        repo.set_dataset_start(layout["dataset"], parse_start_or_400(text, store) if text else None)
        flash("Recording start time saved." if text else "Recording start time cleared.", "good")
        return redirect(url_for("settings", ref=ref) + "#s-lay")

    @app.post("/app/s/<ref>/layouts/<key>/sales")
    @login_required
    @writer_required
    def upload_sales(ref, key):
        store, layouts = load_ctx(ref, need_edit=True)
        layout = layout_by_key(layouts, key)
        if not layout:
            abort(404)
        ds = layout["dataset"]
        if not ds or not ds["n_points"]:
            raise val.ValidationError(f"Add a recording for layout {key} first. Sales are matched to the people in it.")
        f = request.files.get("file")
        data = f.read(val.MAX_UPLOAD_BYTES + 1) if f else b""
        if not data:
            raise val.ValidationError("Choose a CSV file first.")
        report = {}
        df = val.pos_from_csv(data, clock=wallclock.Clock.for_recording(ds, store), report=report)
        repo.save_pos(ds, store, df)
        flash(f"Sales saved: {df.txn_id.nunique():,} sales from {len(df):,} lines. Only txn_id, t, zone and amount were kept."
              + (f" {report['dropped_before_start']} lines from before the recording started were left out." if report.get("dropped_before_start") else ""), "good")
        return redirect(url_for("revenue", ref=ref, layout=key))

    @app.post("/app/s/<ref>/keys/create")
    @login_required
    @writer_required
    def key_create(ref):
        store, _ = load_ctx(ref, need_edit=True)
        label = val.clean_text(request.form.get("label", "") or "Camera agent", "key name", 40)
        _rec, plain = repo.create_api_key(g.user, store["id"], label)
        session["new_key"] = plain      # shown once on the next page, never stored in plain text
        return redirect(url_for("settings", ref=ref) + "#keys")

    @app.post("/app/s/<ref>/keys/<key_id>/revoke")
    @login_required
    @writer_required
    def key_revoke(ref, key_id):
        store, _ = load_ctx(ref, need_edit=True)
        repo.revoke_api_key(g.user, store["id"], key_id)
        flash("Key revoked. Agents using it can no longer send data.", "good")
        return redirect(url_for("settings", ref=ref) + "#keys")

    # ------------------------------------------------------------------ ingest API (camera agents)
    @app.post("/api/ingest/v1/tracks")
    def ingest():
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return jsonify(error="Send the store API key as 'Authorization: Bearer dtm_...'."), 401
        found = repo.resolve_api_key(auth[7:].strip())
        if not found:
            return jsonify(error="That API key is not valid or has been revoked."), 401
        store = repo.svc_get_store(found["store_id"])
        if not store or store.get("is_demo"):
            return jsonify(error="That key cannot receive data."), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Send a JSON body: {layout, recording, points}."), 400
        try:
            key = val.clean_layout_key(body.get("layout"))
            recording = val.clean_text(str(body.get("recording") or "Live"), "recording", 60)
            df = val.points_from_json(body.get("points"), float(store["width_m"]), float(store["height_m"]))
        except val.ValidationError as e:
            return jsonify(error=str(e)), 400
        layout = repo.svc_layout_by_key(store["id"], key)
        if not layout:
            return jsonify(error=f"This store has no layout {key}. Create it in the app first."), 404
        ds = repo.svc_get_or_create_dataset(layout["id"], recording, "ingest")
        repo.append_points(ds, df)
        if body.get("started_at") and not ds.get("started_at"):          # the first writer sets it; later batches never move it
            try:
                repo.set_dataset_start(ds, wallclock.iso(wallclock.parse_start(body["started_at"], "UTC")))
            except (ValueError, RepoError):
                pass                                                     # a bad start time must not lose the points
        return jsonify(accepted=int(len(df)), layout=key, recording=recording), 202

    @app.post("/api/ingest/v1/sales")
    def ingest_sales():
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return jsonify(error="Send the store API key as 'Authorization: Bearer dtm_...'."), 401
        found = repo.resolve_api_key(auth[7:].strip())
        if not found:
            return jsonify(error="That API key is not valid or has been revoked."), 401
        store = repo.svc_get_store(found["store_id"])
        if not store or store.get("is_demo"):
            return jsonify(error="That key cannot receive data."), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Send a JSON body: {layout, recording, sales}."), 400
        try:
            key = val.clean_layout_key(body.get("layout"))
            recording = val.clean_text(str(body.get("recording") or "Live"), "recording", 60)
            layout = repo.svc_layout_by_key(store["id"], key)
            if not layout:
                return jsonify(error=f"This store has no layout {key}. Create it in the app first."), 404
            ds = repo.svc_get_or_create_dataset(layout["id"], recording, "ingest")
            df = val.pos_from_json(body.get("sales"), clock=wallclock.Clock.for_recording(ds, store))
        except val.ValidationError as e:
            return jsonify(error=str(e)), 400
        with pos_lock:                            # read-merge-write: one writer at a time in this process
            repo.append_pos(ds, store, df)
        return jsonify(accepted=int(len(df)), layout=key, recording=recording), 202

    def warm():
        """Analyse the demo stores in the background so the first visit is instant."""
        try:
            sess = repo.sign_in(demo_email, demo_password)
            for s in repo.list_stores(sess):
                store, layouts = load_ctx(store_ref(s), sess=sess)
                for l in layouts:
                    if l["ready"]:
                        analyse(store, l)
            log.info("Demo analysis warmed.")
        except Exception:
            log.exception("Warm-up skipped")

    if demo_email and demo_password and os.environ.get("WARM", "1") == "1":
        threading.Thread(target=warm, daemon=True).start()

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
