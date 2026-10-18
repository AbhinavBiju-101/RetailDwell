"""Analytics core. Input: tracks DataFrame with columns
(layout, track_id, t, x, y). Output: zone stats, heat maps, mission labels,
checkout throughput, normalised before/after comparison, relative flags.
"""
import math
import numpy as np
import pandas as pd
import thresholds
from config import (TIME_WINDOW_S, TIME_MIN_SHOPPERS, TIME_MIN_EXPECTED, TIME_MIN_Z, DWELL_RADIUS_M, DWELL_MIN_S, MIN_PRESENCE_S, SAMPLE_HZ,
                    QUEUE_MARGIN_M)

DT = 1.0 / SAMPLE_HZ
DIRECTED_MAX_ENGAGED_S = 30.0   # total stopped time at merchandise for a "directed" visit
WAIT_WARMUP_MIN = 10            # ignore the first minutes: the store is still filling
WAIT_MIN_THROUGHPUT = 0.8       # served/min needed before Little's law is meaningful


# ---------- zone assignment ----------
def assign_zone(df, zones):
    """Zone name per row; merchandise zones take priority over circulation."""
    zone = np.full(len(df), "Circulation", dtype=object)
    order = sorted(zones.items(), key=lambda kv: kv[1][4] == "merchandise")  # merch last = wins
    for name, (x0, y0, x1, y1, _) in order:
        m = ((df["x"] >= x0) & (df["x"] <= x1) & (df["y"] >= y0) & (df["y"] <= y1)).to_numpy()
        zone[m] = name
    return zone


# ---------- staff and parties ----------
def _inside(tracks, zones, ztype):
    m = np.zeros(len(tracks), dtype=bool)
    for x0, y0, x1, y1, t in zones.values():
        if t == ztype:
            m |= ((tracks["x"] >= x0) & (tracks["x"] <= x1) & (tracks["y"] >= y0) & (tracks["y"] <= y1)).to_numpy()
    return m


def find_staff(tracks, zones, params=None):
    """Tracks that behave like staff: {track_id: reason}. Nobody is identified, only behaviour is read:
    time in a zone of type staff, a long stand inside the checkout zone (a cashier), or being on the floor far
    longer than a shopper (only judged when the recording is at least twice that long). Excluded tracks are
    reported in the app with the reason, so a wrong call can be seen."""
    if tracks.empty:
        return {}
    p = {**thresholds.defaults(), **(params or {})}
    tid = tracks["track_id"].to_numpy()
    sec = lambda mask: pd.Series(mask.astype(float) * DT).groupby(tid).sum()
    in_staff, in_till = sec(_inside(tracks, zones, "staff")), sec(_inside(tracks, zones, "checkout"))
    span = tracks.groupby("track_id")["t"].agg(lambda s: s.max() - s.min())
    long_ok = (tracks["t"].max() - tracks["t"].min()) >= 2 * p["staff_min_duration_s"]
    out = {}
    for t in span.index:
        if in_staff.get(t, 0) >= p["staff_zone_min_s"]:
            out[int(t)] = "spent time in a staff zone"
        elif in_till.get(t, 0) >= p["staff_checkout_min_s"]:
            out[int(t)] = f"stood at the register for {in_till[t] / 60:.0f} minutes"
        elif long_ok and span[t] >= p["staff_min_duration_s"]:
            out[int(t)] = f"on the floor for {span[t] / 60:.0f} minutes"
    return out


def find_parties(df, params=None):
    """People who move together (family, friends) from how close their paths stay: within PARTY_DIST_M for at
    least PARTY_MIN_S and PARTY_MIN_FRAC of the shorter visit, with some of that time walking, so two strangers
    queueing side by side are not joined. They must also appear and disappear together (within PARTY_ENTRY_GAP_S):
    people in a queue stay close for minutes but arrive at different times and leave one service apart. Clusters
    bigger than PARTY_MAX_SIZE are left alone.
    Returns dict(of={track_id: party_id}, sizes={party_id: n}); a party id is its smallest track id."""
    p = {**thresholds.defaults(), **(params or {})}
    d = df[["track_id", "t", "x", "y"]].copy()
    d["k"] = (d["t"] * SAMPLE_HZ).round().astype(int)
    d = d.sort_values(["track_id", "k"]).drop_duplicates(["track_id", "k"])      # one point per track per half-second
    gap = d.groupby("track_id")["k"].diff().fillna(1).clip(lower=1) / SAMPLE_HZ
    d["moving"] = (np.hypot(d.groupby("track_id")["x"].diff(), d.groupby("track_id")["y"].diff()) / gap > 0.3).to_numpy()
    n_bins = d.groupby("track_id").size()
    first, last = d.groupby("track_id")["t"].min(), d.groupby("track_id")["t"].max()
    order = first.sort_values()
    cand, ids_o, t_o = set(), order.index.to_numpy(), order.to_numpy()
    for i in range(len(ids_o)):                                   # pairs that arrive together and leave together
        j = i + 1
        while j < len(ids_o) and t_o[j] - t_o[i] <= p["party_entry_gap_s"]:
            if abs(last[ids_o[i]] - last[ids_o[j]]) <= p["party_entry_gap_s"]:
                cand.add((int(min(ids_o[i], ids_o[j])), int(max(ids_o[i], ids_o[j]))))
            j += 1
    by = {int(t): (g["k"].to_numpy(), g["x"].to_numpy(), g["y"].to_numpy(), g["moving"].to_numpy()) for t, g in d.groupby("track_id")}
    co, co_move = {}, {}
    for a, b in cand:                                             # score each candidate pair on the bins both are present
        ka, xa, ya, ma = by[a]
        kb, xb, yb, mb = by[b]
        _, ia, ib = np.intersect1d(ka, kb, assume_unique=True, return_indices=True)
        if len(ia) == 0:
            continue
        close = np.hypot(xa[ia] - xb[ib], ya[ia] - yb[ib]) <= p["party_dist_m"]
        co[(a, b)] = int(close.sum())
        co_move[(a, b)] = int((close & (ma[ia] | mb[ib])).sum())
    parent = {int(t): int(t) for t in n_bins.index}
    size = {t: 1 for t in parent}

    def root(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t
    for (a, b), c in sorted(co.items(), key=lambda kv: -kv[1]):
        if c * DT >= p["party_min_s"] and c >= p["party_min_frac"] * min(n_bins[a], n_bins[b]) and co_move.get((a, b), 0) * DT >= p["party_min_moving_s"]:
            ra, rb = root(a), root(b)
            if ra != rb and size[ra] + size[rb] <= p["party_max_size"]:
                lo, hi = min(ra, rb), max(ra, rb)
                parent[hi] = lo
                size[lo] += size[hi]
    of = {t: root(t) for t in parent}
    sizes = {}
    for t, r in of.items():
        sizes[r] = sizes.get(r, 0) + 1
    return dict(of=of, sizes=sizes)


# ---------- dwell by displacement ----------
def _stop_spans(g):
    """Stays within DWELL_RADIUS_M of an anchor for >= DWELL_MIN_S.
    Returns list of (anchor_x, anchor_y, t_first, t_last) in seconds."""
    xs, ys, ts = g["x"].to_numpy(), g["y"].to_numpy(), g["t"].to_numpy()
    spans, i, n = [], 0, len(xs)
    while i < n:
        j = i
        while j + 1 < n and math.hypot(xs[j + 1] - xs[i], ys[j + 1] - ys[i]) <= DWELL_RADIUS_M:
            j += 1
        if ts[j] - ts[i] + DT >= DWELL_MIN_S:
            spans.append((xs[i:j + 1].mean(), ys[i:j + 1].mean(), ts[i], ts[j]))
            i = j + 1
        else:
            i += 1
    return spans


def stop_episodes(g):
    """Stops as (anchor_x, anchor_y, duration_s)."""
    return [(ax, ay, t1 - t0 + DT) for ax, ay, t0, t1 in _stop_spans(g)]


def build_events(df, zones):
    """Per-track zone presence and engaged-dwell episodes."""
    df = df.sort_values(["track_id", "t"]).copy()
    df["zone"] = assign_zone(df, zones)
    ztype = {k: v[4] for k, v in zones.items()}
    ztype["Circulation"] = "circulation"

    presence = (df.groupby(["track_id", "zone"]).size() * DT).rename("present_s").reset_index()
    presence = presence[presence.present_s >= MIN_PRESENCE_S]

    ep_rows = []
    for tid, g in df.groupby("track_id"):
        for (ax, ay, dur) in stop_episodes(g):
            z = assign_zone(pd.DataFrame({"x": [ax], "y": [ay]}), zones)[0]
            ep_rows.append((tid, z, dur))
    episodes = pd.DataFrame(ep_rows, columns=["track_id", "zone", "dur_s"])
    episodes["zone_type"] = episodes["zone"].map(ztype)
    return df, presence, episodes


# ---------- mission & loiterer labels (soft, rule based) ----------
def label_visits(df, zones, presence, episodes):
    ztype = {k: v[4] for k, v in zones.items()}
    rows = []
    merch_visits = presence[presence.zone.map(ztype) == "merchandise"].groupby("track_id").size()
    for tid, g in df.groupby("track_id"):
        dt = g["t"].diff().fillna(DT).clip(lower=DT)
        step = np.hypot(g["x"].diff().fillna(0), g["y"].diff().fillna(0))
        speed = (step / dt).rolling(6, min_periods=1).mean()
        moving = speed > 0.5
        walk_speed = speed[moving].mean() if moving.any() else 0.0
        dur = g["t"].max() - g["t"].min() + DT
        n_merch = int(merch_visits.get(tid, 0))
        ep = episodes[episodes.track_id == tid]
        engaged = ep[ep.zone_type == "merchandise"].dur_s.sum()
        idle_nonmerch = ep[ep.zone_type.isin(["entrance", "circulation", "seating"])].dur_s.sum()
        n_stops = int((ep.zone_type == "merchandise").sum())
        if n_merch == 0 and idle_nonmerch / max(dur, 1) > 0.5:
            label, conf = "loiterer / waiter", min(0.95, 0.5 + idle_nonmerch / max(dur, 1) / 2)
        elif n_merch == 0:
            label, conf = "pass-through", 0.6
        elif n_stops <= 2 and engaged < DIRECTED_MAX_ENGAGED_S:
            # few, short stops at merchandise: grabbed what they came for
            label, conf = "directed", round(min(0.9, 0.6 + 0.3 * (1 - engaged / DIRECTED_MAX_ENGAGED_S)), 2)
        else:
            label, conf = "exploratory", round(min(0.9, 0.6 + 0.3 * min(1, engaged / 90)), 2)
        rows.append((tid, label, round(conf, 2), n_merch, engaged, dur, walk_speed))
    return pd.DataFrame(rows, columns=["track_id", "mission", "confidence", "n_merch_zones",
                                       "engaged_s", "visit_s", "walk_speed"])


# ---------- zone stats ----------
def zone_stats(zones, presence, episodes, labels):
    shoppers = labels[labels.mission != "loiterer / waiter"].track_id
    n_total, n_shop = labels.track_id.nunique(), shoppers.nunique()
    rows = []
    for name, (x0, y0, x1, y1, typ) in zones.items():
        p = presence[presence.zone == name]
        e = episodes[(episodes.zone == name)]
        e_sh = e[e.track_id.isin(shoppers)]
        vis_all = p.track_id.nunique()
        vis_sh = p[p.track_id.isin(shoppers)].track_id.nunique()
        rows.append(dict(
            zone=name, type=typ, area_m2=(x1 - x0) * (y1 - y0),
            visits=vis_all, visits_shoppers=vis_sh,
            visit_share=vis_sh / max(n_shop, 1),
            presence_dwell_s=p.present_s.mean() if len(p) else 0.0,
            engaged_dwell_s=(e_sh.dur_s.sum() / vis_sh) if vis_sh else 0.0,
            stoppers=e_sh.track_id.nunique(),
            stop_rate=(e_sh.track_id.nunique() / vis_sh) if vis_sh else 0.0,
        ))
    out = pd.DataFrame(rows)
    out.attrs["n_visits"], out.attrs["n_shoppers"] = n_total, n_shop
    return out


# ---------- heat map ----------
def heatmap(df, w, h, weight_by_time=True):
    """Time spent per 0.5 m cell over a w x h metre floor (rows = y)."""
    bins = (max(int(round(w * 2)), 1), max(int(round(h * 2)), 1))
    H, xe, ye = np.histogram2d(df["x"], df["y"], bins=bins, range=[[0, w], [0, h]])
    H = H * DT if weight_by_time else H
    return H.T, xe, ye  # rows = y


# ---------- checkout throughput & wait (Little's law) ----------
CHECKOUT_COLS = ["window_start_min", "occupancy", "throughput_per_min", "est_wait_min", "valid"]


def checkout_metrics(df, zones, window_s=300, w=None, h=None):
    """Occupancy and throughput of the checkout area per window, plus a Little's-law wait."""
    cz = [z for z in zones.values() if z[4] == "checkout"]
    if not cz or df.empty:
        return pd.DataFrame(columns=CHECKOUT_COLS)
    z = cz[0]
    inside = df[(df.x >= z[0]) & (df.x <= z[2]) & (df.y >= z[1]) & (df.y <= z[3])]
    # the queue forms just outside the register zone: count the whole approach area
    q = df[(df.x >= z[0] - QUEUE_MARGIN_M) & (df.x <= z[2]) & (df.y >= z[1]) & (df.y <= z[3] + 0.5)]
    last_exit = inside.groupby("track_id")["t"].max()  # last seen in the register zone
    tmax = df["t"].max()
    bins = np.arange(0, tmax + window_s, window_s)
    rows = []
    for i in range(len(bins) - 1):
        a, b = bins[i], bins[i + 1]
        qa = q[(q.t >= a) & (q.t < b)]
        occ = (qa.groupby(pd.cut(qa.t, np.arange(a, b + DT, 10)), observed=False).track_id.nunique().mean()
               if len(qa) else 0)
        thr = ((last_exit >= a) & (last_exit < b)).sum() / (window_s / 60)
        wait = (occ / thr) if thr > 0 else np.nan  # minutes (L / lambda)
        valid = (a / 60 >= WAIT_WARMUP_MIN) and (thr >= WAIT_MIN_THROUGHPUT) and (b <= tmax + DT)
        rows.append(dict(window_start_min=a / 60, occupancy=occ, throughput_per_min=thr,
                         est_wait_min=wait, valid=valid))
    return pd.DataFrame(rows, columns=CHECKOUT_COLS)


def wait_summary(cm):
    """Median and peak estimated wait over the valid windows (None if there are none)."""
    ok = cm[cm.valid & cm.est_wait_min.notna()] if len(cm) else cm
    if ok is None or len(ok) == 0:
        return None
    return dict(median=float(ok.est_wait_min.median()), peak=float(ok.est_wait_min.max()),
                windows=int(len(ok)))


# ---------- before / after with normalisation ----------
LOITERER = "loiterer / waiter"


def _zone_hits(R, zone, metric):
    """0/1 per shopper: did they reach the zone (time inside it) or stop in it (5+ s within 1 m)?"""
    lab = R["labels"]
    ids = np.array(sorted(lab[lab.mission != LOITERER].track_id))
    src = R["presence"] if metric == "reach" else R["episodes"]
    hit = set(src[src.zone == zone].track_id)
    return np.array([i in hit for i in ids], dtype=float)


def compare_layouts(Ra, Rb, zone, metric="reach", n_boot=2000, seed=0):
    """Compares the share of shoppers who reach (or stop at) `zone` before and after, so a busier
    day does not look like a better layout. Loiterers are excluded. Bootstrap CI on relative lift."""
    rng = np.random.default_rng(seed)
    fa, fb = _zone_hits(Ra, zone, metric), _zone_hits(Rb, zone, metric)
    pa, pb = fa.mean(), fb.mean()
    lifts = []
    for _ in range(n_boot):
        ra = rng.choice(fa, len(fa)).mean()
        rb = rng.choice(fb, len(fb)).mean()
        if ra > 0:
            lifts.append(rb / ra - 1)
    lo, hi = np.percentile(lifts, [2.5, 97.5]) if lifts else (np.nan, np.nan)
    raw_a, raw_b = int(fa.sum()), int(fb.sum())
    return dict(zone=zone, metric=metric, share_a=float(pa), share_b=float(pb),
                lift=float(pb / pa - 1) if pa else float("nan"),
                ci_low=float(lo), ci_high=float(hi), raw_visits_a=raw_a, raw_visits_b=raw_b,
                raw_lift=float(raw_b / raw_a - 1) if raw_a else float("nan"),
                n_a=len(fa), n_b=len(fb))


# ---------- relative flagging with plain-language explanation ----------
def relative_flags(stats, labels):
    """Fallback chain step 2: compare each merchandise zone to the store-wide
    median (percentile fallback). Zone-own baselines come later with history."""
    m = stats[stats.type == "merchandise"].copy()
    med_v, med_d = m.visit_share.median(), m.engaged_dwell_s.median()
    flags = []
    for _, r in m.iterrows():
        v_ratio = r.visit_share / med_v if med_v else 1
        d_ratio = r.engaged_dwell_s / med_d if med_d else 1
        low_v, high_v = v_ratio < 0.7, v_ratio > 1.3
        low_d, high_d = d_ratio < 0.7, d_ratio > 1.3
        if low_v and low_d:
            kind = "Dead zone"
            msg = (f"{r.zone}: visits {abs(1-v_ratio):.0%} below the store median and dwell "
                   f"{abs(1-d_ratio):.0%} below. Few people come and those who do leave quickly. "
                   f"Relocate it, or redesign the display and signage.")
        elif low_v:
            kind = "Visibility problem"
            msg = (f"{r.zone}: visits {abs(1-v_ratio):.0%} below the store median, dwell normal. "
                   f"People are not seeing it. Move signage to sightline height or closer to the main path.")
        elif high_v and low_d:
            kind = "Pass-through"
            msg = (f"{r.zone}: visits {v_ratio-1:.0%} above median but dwell {abs(1-d_ratio):.0%} below. "
                   f"Shoppers cross it without stopping. Good spot for impulse items and promos.")
        elif high_v and high_d:
            kind = "Hot zone"
            msg = f"{r.zone}: high visits and high dwell. Engaged zone, protect its footprint."
        elif not low_v and high_d:
            kind = "Destination"
            msg = (f"{r.zone}: average visits but dwell {d_ratio-1:.0%} above median. "
                   f"People come specifically for it. Consider seating, demos or staff.")
        else:
            kind, msg = "Healthy", f"{r.zone}: within the normal range."
        flags.append(dict(zone=r.zone, kind=kind, visit_ratio=v_ratio, dwell_ratio=d_ratio,
                          message=msg))
    return pd.DataFrame(flags)


def flag_checkout(cm, where=None):
    where = where or (lambda m: f"minute {m:.0f}")
    ok = cm[cm.valid & cm.est_wait_min.notna()]
    if ok.empty:
        return None
    peak = ok.loc[ok.est_wait_min.idxmax()]
    sev = "Bottleneck" if peak.est_wait_min >= 4 else "OK"
    return dict(kind=sev, window_start_min=peak.window_start_min,
                est_wait_min=peak.est_wait_min, occupancy=peak.occupancy,
                throughput=peak.throughput_per_min,
                message=(f"Checkout: about {peak.est_wait_min:.1f} min estimated wait around "
                         f"{where(peak.window_start_min)} ({peak.occupancy:.1f} people, "
                         f"{peak.throughput_per_min:.1f} served/min). Indicative only; Little's law "
                         f"assumes steady state."))


# ---------- each zone against its own usual share, by time window ----------
def visit_slots(df, window_s=TIME_WINDOW_S, slot_fn=None):
    """Time window of each visit (by its first sample), numbered the same way as `time_baselines` numbers its windows."""
    t0 = df.groupby("track_id").t.min()
    w = slot_fn(t0.to_numpy()) if slot_fn else (t0 // window_s).astype(int).to_numpy()
    return pd.Series(np.asarray(w), index=t0.index)


def time_baselines(df, labels, zones, window_s=TIME_WINDOW_S, slot_fn=None):
    """Share of shoppers reaching each merchandise zone per time window, against the zone's own average.

    Normalised by the shoppers present in the window, so a busy hour does not look like a good hour for every zone.
    A window is flagged only when it is both 30%+ away from the zone's norm and unlikely to be chance
    (normal approximation to a binomial, |z| >= TIME_MIN_Z).
    Returns dict(window_s, windows, cells, reason): `reason` is set (and cells empty) when there is too little data.
    t is seconds since the recording started; windows are labelled with wall-clock time by the app when the
    recording has a start time (clock.py). With `slot_fn` (pooled days) windows are the clock hours of the day and
    gather shoppers from every pooled day; start_min and end_min are then minutes since midnight.
    """
    shoppers = labels[labels.mission != "loiterer / waiter"].track_id
    d = df[df.track_id.isin(shoppers)].copy()
    out = dict(window_s=window_s, windows=[], cells=[], reason=None)
    if d.empty:
        out["reason"] = "No shopper tracks yet."
        return out
    d["w"] = slot_fn(d.t.to_numpy()) if slot_fn else (d.t // window_s).astype(int)
    n_w = d.groupby("w").track_id.nunique()
    valid = n_w[n_w >= TIME_MIN_SHOPPERS].index.tolist()
    if len(valid) < 2:
        out["reason"] = (f"Needs at least two {window_s // 60}-minute windows with {TIME_MIN_SHOPPERS}+ shoppers each; "
                         f"this recording has {len(valid)}.")
        return out
    pres = (d.groupby(["w", "zone", "track_id"]).size() * DT).rename("present_s").reset_index()
    pres = pres[pres.present_s >= MIN_PRESENCE_S]
    visitors = pres.groupby(["zone", "w"]).track_id.nunique()
    out["windows"] = [dict(w=int(w), start_min=int(w * window_s // 60), end_min=int((w + 1) * window_s // 60),
                           shoppers=int(n_w[w])) for w in valid]
    total_n = float(n_w[valid].sum())
    for name, z in zones.items():
        if z[4] != "merchandise":
            continue
        v = {w: int(visitors.get((name, w), 0)) for w in valid}
        p = sum(v.values()) / total_n
        for w in valid:
            n = float(n_w[w])
            exp = p * n
            sd = math.sqrt(n * p * (1 - p))
            ratio = v[w] / exp if exp > 0 else float("nan")
            zsc = (v[w] - exp) / sd if sd > 0 else 0.0
            flag = None
            if exp >= TIME_MIN_EXPECTED and abs(zsc) >= TIME_MIN_Z:
                flag = "low" if (ratio < 0.7 and zsc < 0) else "high" if (ratio > 1.3 and zsc > 0) else None
            out["cells"].append(dict(zone=name, w=int(w), visitors=v[w], expected=round(exp, 1), shoppers=int(n),
                                     ratio=None if ratio != ratio else round(ratio, 3), z=round(zsc, 2), flag=flag))
    return out


def timing_flags(tb, limit=6, span=None):
    """Plain-language findings for the unusual zone-windows, strongest first."""
    rows = []
    span = span or (lambda a, b: f"minutes {a} and {b}")      # a Clock supplies wall-clock wording
    mins = {w["w"]: (w["start_min"], w["end_min"]) for w in tb.get("windows", [])}
    for c in sorted((c for c in tb.get("cells", []) if c["flag"]), key=lambda c: -abs(c["z"]))[:limit]:
        a, b = mins[c["w"]]
        pct = abs(1 - c["ratio"])
        if c["flag"] == "low":
            rows.append(dict(kind="Quieter than usual", zone=c["zone"], w=c["w"], message=(
                f"{c['zone']}: {pct:.0%} fewer shoppers than its own usual share between {span(a, b)} "
                f"({c['visitors']} reached it, about {c['expected']:.0f} expected). Look for a cause at that time: "
                f"stock out, a blocked route, a promotion elsewhere or fewer staff nearby.")))
        else:
            rows.append(dict(kind="Busier than usual", zone=c["zone"], w=c["w"], message=(
                f"{c['zone']}: {pct:.0%} more shoppers than its own usual share between {span(a, b)} "
                f"({c['visitors']} reached it, about {c['expected']:.0f} expected). If that was a promotion or "
                f"an event, it worked. If not, find out what drew people there.")))
    return rows


# ---------- one-call API used by the dashboard ----------
def analyse_layout(tracks, zones, w, h, window_s=300, exclude_staff=True, params=None, pooled=None):
    """Full analysis of one recording. `zones` is {name: (x0, y0, x1, y1, type)}.
    Tracks that look like staff are left out of every figure and listed under R["staff"]."""
    p = {**thresholds.defaults(), **(params or {})}
    staff = find_staff(tracks, zones, p) if exclude_staff and p["staff_filter"] else {}
    if staff:
        tracks = tracks[~tracks["track_id"].isin(list(staff))]
    df, presence, episodes = build_events(tracks, zones)
    labels = label_visits(df, zones, presence, episodes)
    stats = zone_stats(zones, presence, episodes, labels)
    pooled = pooled or {}
    if pooled.get("n"):
        if pooled.get("slot_fn"):
            timing = time_baselines(df, labels, zones, slot_fn=pooled["slot_fn"])
        else:
            timing = dict(window_s=TIME_WINDOW_S, windows=[], cells=[],
                          reason="To line the hours up across days, every pooled recording needs a start time. Set them in Settings.")
    else:
        timing = time_baselines(df, labels, zones)
    checkout = checkout_metrics(df, zones, window_s)
    if pooled.get("minute_of_day_fn") and len(checkout):          # pooled checkout windows: show them by clock time, days overlaid
        checkout = checkout.assign(window_start_min=pooled["minute_of_day_fn"](checkout["window_start_min"].to_numpy() * 60.0))
    return dict(zones=zones, w=w, h=h, df=df, presence=presence, episodes=episodes, labels=labels,
                stats=stats, flags=relative_flags(stats, labels), timing=timing,
                checkout=checkout, pooled=pooled or None, staff=staff, parties=find_parties(df, p) if len(df) and p["parties_enabled"] else dict(of={}, sizes={}), params=p)
