"""Synthetic store simulator, driven by the specs in stores.py.

Emits the same schema the real video pipeline will: one row per
(layout, track_id, t, x, y), plus the true mission (for checking the labels).
Includes directed shoppers, explorers, loiterers/waiters and a multi-server
checkout queue served in arrival order.

Usage: python simulate.py [out_dir]   -> out_dir/<store>/<layout>.csv.gz
"""
import math
import os
import sys

import numpy as np
import pandas as pd

from config import SAMPLE_HZ
from stores import BASKETS, CROWD, DEFAULT_MIX, DEFAULT_TAKE, STORES, main_path

DT = 1.0 / SAMPLE_HZ


def _dist_to_path(p, path):
    best = 1e9
    for (x0, y0), (x1, y1) in zip(path[:-1], path[1:]):
        vx, vy = x1 - x0, y1 - y0
        L2 = vx * vx + vy * vy
        t = max(0, min(1, ((p[0] - x0) * vx + (p[1] - y0) * vy) / L2)) if L2 else 0
        best = min(best, math.hypot(p[0] - (x0 + t * vx), p[1] - (y0 + t * vy)))
    return best


def _center(z):
    return ((z[0] + z[2]) / 2, (z[1] + z[3]) / 2)


def _visit_prob(name, zones, mission, spec, path):
    """Chance a shopper goes to a zone: base interest x visibility from the walking route."""
    w = spec["interest"][name]
    vis = math.exp(-_dist_to_path(_center(zones[name]), path) / 4.0)
    if mission == "directed":
        w *= spec.get("directed_mult", {}).get(name, 1.0)
        return min(0.95, w * (0.6 + 0.8 * vis))
    base, gain = spec.get("vis", (0.4, 1.6))
    return min(0.95, w * (base + gain * vis))


def _walk(points, t0, speed, rng, noise=0.12):
    """Straight-line walk through waypoints; returns list of (t, x, y)."""
    out, t = [], t0
    for (x0, y0), (x1, y1) in zip(points[:-1], points[1:]):
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / (speed * DT)))
        for i in range(1, n + 1):
            f = i / n
            out.append((t, x0 + f * (x1 - x0) + rng.normal(0, noise),
                        y0 + f * (y1 - y0) + rng.normal(0, noise)))
            t += DT
    return out, t


def _stand(x, y, t0, dur, rng, jitter=0.15, drift=0.0):
    out, t = [], t0
    for _ in range(max(1, int(dur / DT))):
        x += rng.normal(0, drift)
        y += rng.normal(0, drift)
        out.append((t, x + rng.normal(0, jitter), y + rng.normal(0, jitter)))
        t += DT
    return out, t


def _to_queue(pos, aisle, queue):
    if pos[1] > aisle + 0.5:
        return [pos, (pos[0], aisle), (queue[0] - 0.3, aisle), queue]
    return [pos, queue]


def _exit_route(pos, spec):
    ent, aisle = spec["entrance"], spec["aisle_y"]
    mid = (pos[0] if pos[0] > spec["w"] * 0.6 else ent[0], aisle - 1.0)
    return [pos, mid, (ent[0], 1.0), (ent[0], -0.5)]


def simulate_layout(store_key, layout_key):
    spec = STORES[store_key]
    lay = spec["layouts"][layout_key]
    zones = lay["zones"]
    servers = lay.get("servers", spec.get("servers", 1))
    service = spec.get("service", (12, 28))
    path = main_path(spec)
    ent, aisle, reg, queue = spec["entrance"], spec["aisle_y"], spec["register"], spec["queue"]
    mix = spec.get("mix", DEFAULT_MIX)
    dwell = spec.get("dwell", {})
    boost = spec.get("buy_boost", {})
    rng = np.random.default_rng(lay["seed"])

    visits = []
    for _ in range(lay["visits"]):
        r = rng.random()
        mission = ("loiterer" if r < mix["loiterer"]
                   else "directed" if r < mix["loiterer"] + mix["directed"] else "exploratory")
        visits.append(dict(start=rng.uniform(0, lay["window_s"]), mission=mission,
                           rng=np.random.default_rng(rng.integers(1 << 31))))
    visits.sort(key=lambda d: d["start"])

    rows, buyers, pos_events = [], [], []
    done = []
    # ---- pass 1: everything up to the point of joining the queue
    for tid, v in enumerate(visits):
        rg, mission = v["rng"], v["mission"]
        track, t = _walk([(ent[0] - 0.5, -0.5), ent], v["start"], 1.2, rg)

        if mission == "loiterer":
            bx0, by0, bx1, by1 = spec["loiter_box"]
            lx, ly = rg.uniform(bx0, bx1), rg.uniform(by0, by1)
            seg, t = _walk([ent, (lx, ly)], t, 0.8, rg); track += seg
            seg, t = _stand(lx, ly, t, rg.uniform(60, 180), rg, 0.15, 0.04); track += seg
            seg, t = _walk([(lx, ly), (ent[0], -0.5)], t, 1.0, rg); track += seg
            done.append((tid, mission, track))
            continue

        speed = 1.4 if mission == "directed" else 0.8
        names = list(spec["interest"])
        targets = [n for n in names if rg.random() < _visit_prob(n, zones, mission, spec, path)]
        if mission == "directed":
            targets = targets[:2]
        elif not targets and rg.random() < 0.7:
            targets = [rg.choice(names)]
        # visit in order of distance from the entrance (greedy)
        cur, order, rem = ent, [], list(targets)
        while rem:
            nxt = min(rem, key=lambda n: math.hypot(*(np.array(_center(zones[n])) - cur)))
            order.append(nxt); rem.remove(nxt); cur = _center(zones[nxt])
        if mission == "exploratory" and spec.get("stop_cap"):
            lo, hi = spec["stop_cap"]
            order = order[:int(rg.integers(lo, hi + 1))]   # limited attention: early stops use up the budget

        pos = ent
        for name in order:
            z = zones[name]
            tx = rg.uniform(z[0] + 0.7, z[2] - 0.7)
            ty = rg.uniform(z[1] + 0.7, z[3] - 0.7)
            via = [pos, (pos[0], aisle), (tx, aisle), (tx, ty)] if pos[1] < aisle + 1.0 else [pos, (tx, ty)]
            seg, t = _walk(via, t, speed, rg); track += seg
            if mission == "directed":
                seg, t = _stand(tx, ty, t, rg.uniform(4, 12), rg, 0.12, 0.0)
            else:
                lo, hi = dwell.get(name, (10, 45))
                seg, t = _stand(tx, ty, t, rg.uniform(lo, hi), rg, 0.15, 0.06)
            track += seg
            pos = (tx, ty)

        rate_d, rate_e = spec.get("buy_rates", (0.75, 0.4))
        p_buy = (rate_d if mission == "directed" else rate_e) + sum(boost.get(n, 0.0) for n in order)
        if rg.random() < min(0.95, p_buy):
            seg, t = _walk(_to_queue(pos, aisle, queue), t, speed, rg); track += seg
            buyers.append(dict(tid=tid, mission=mission, rg=rg, track=track, t=t, speed=speed, order=list(order)))
        else:
            seg, t = _walk(_exit_route(pos, spec), t, speed, rg); track += seg
            done.append((tid, mission, track))

    # ---- pass 2: serve the queue in arrival order across `servers` counters
    free = [0.0] * servers
    for b in sorted(buyers, key=lambda d: d["t"]):
        rg, track, t = b["rg"], b["track"], b["t"]
        i = int(np.argmin(free))
        start = max(t, free[i])
        if start - t > 0:
            seg, t = _stand(queue[0] + rg.normal(0, 0.2), queue[1], t, start - t, rg, 0.2, 0.02); track += seg
        spot = (reg[0], reg[1] + 0.9 * i)
        seg, t = _walk([queue, spot], t, 0.8, rg); track += seg
        seg, t = _stand(spot[0], spot[1], t, rg.uniform(*service), rg, 0.1, 0.0); track += seg
        free[i] = t
        pos_events.append((layout_key, b["tid"], t, b["mission"], "|".join(b["order"])))
        seg, t = _walk(_exit_route(spot, spec), t, b["speed"], rg); track += seg
        done.append((b["tid"], b["mission"], track))

    for tid, mission, track in done:
        for (tt, x, y) in track:
            rows.append((layout_key, tid, tt, x, y, mission))
    df = pd.DataFrame(rows, columns=["layout", "track_id", "t", "x", "y", "true_mission"])
    df["x"] = df["x"].clip(-1, spec["w"] + 1).round(2)
    df["y"] = df["y"].clip(-1, spec["h"] + 1).round(2)
    df["t"] = df["t"].round(2)
    pos_df = pd.DataFrame(pos_events, columns=["layout", "track_id", "t_checkout_end", "mission", "zones"])
    return df.sort_values(["track_id", "t"]).reset_index(drop=True), pos_df


def generate(store_key):
    """All layouts of one demo store: {layout_key: (tracks_df, pos_df)}."""
    return {k: simulate_layout(store_key, k) for k in STORES[store_key]["layouts"]}


def baskets(store_key, layout_key, pos_df):
    """Mock POS sales for one layout, from the simulator's own record of who paid and where they stopped.

    Returns (lines, truth): lines has txn_id, t, zone, amount (what a till export would hold, with the POS clock
    offset and jitter applied); truth maps txn_id to the true track_id (-1 for a sale nobody was tracked for),
    so the matching can be scored. Uses its own random stream, so the tracks are unchanged by it."""
    spec, cfg = STORES[store_key], BASKETS[store_key]
    lay = spec["layouts"][layout_key]
    rng = np.random.default_rng(lay["seed"] * 7919 + 13)
    zones = [n for n, z in lay["zones"].items() if z[4] == "merchandise"]
    price, take = cfg["price"], cfg.get("take", {})
    skew, noise = cfg.get("skew_s", 0.0), cfg.get("noise_s", 3.0)

    def item(zone):
        p = price[zone] * rng.lognormal(0, 0.3)
        units = 1 + (rng.random() < 0.35 and price[zone] < 500)
        return int(round(p * units / 5.0) * 5)

    sales = []                                       # (t, track_id, {zone: amount})
    for r in pos_df.itertuples(index=False):
        seen = [z for z in r.zones.split("|") if z and z in zones]
        basket = {}
        for z in seen:
            tk = take.get(z, DEFAULT_TAKE)
            if isinstance(tk, tuple):                 # (explorer, directed): high-ticket goods are not a quick grab
                tk = tk[1] if r.mission == "directed" else tk[0]
            elif r.mission == "directed":
                tk = tk + (1 - tk) * 0.35
            if rng.random() < tk:
                basket[z] = item(z)
        for z, (prob, pr) in cfg.get("extra", {}).items():
            if rng.random() < prob:
                basket[z] = int(round(pr * rng.lognormal(0, 0.3) / 5.0) * 5)
        if rng.random() < 0.04 and zones:            # something picked up elsewhere: the tracker never saw them there
            z = zones[int(rng.integers(len(zones)))]
            basket.setdefault(z, item(z))
        if not basket:
            z = seen[0] if seen else zones[int(rng.integers(len(zones)))]
            basket[z] = item(z)
        t = max(0.0, r.t_checkout_end + skew + rng.normal(0, noise))
        sales.append((t, int(r.track_id), basket))
    for _ in range(rng.binomial(len(pos_df), 0.03)):  # sales with no tracked shopper (staff, a missed detection)
        z = zones[int(rng.integers(len(zones)))]
        sales.append((float(rng.uniform(0, lay["window_s"])), -1, {z: item(z)}))
    sales.sort(key=lambda s: s[0])
    lines, truth = [], []
    for i, (t, tid, basket) in enumerate(sales):
        txn = f"T{i + 1:04d}"
        truth.append((txn, tid))
        for z, amt in basket.items():
            lines.append((txn, round(t, 1), z, amt))
    return (pd.DataFrame(lines, columns=["txn_id", "t", "zone", "amount"]),
            pd.DataFrame(truth, columns=["txn_id", "track_id"]))


def crowd(store_key, layout_key, df):
    """Adds companions and staff to a simulated layout. Returns (all_tracks, truth) where truth is
    dict(parties={companion_id: leader_id}, staff=[ids], originals=[ids]). The original shoppers are untouched; a store
    with no CROWD entry gets the tracks back unchanged. Own random stream, so nothing else in the demo moves."""
    cfg = CROWD.get(store_key)
    originals = sorted(int(t) for t in df.track_id.unique())
    if not cfg:
        return df, dict(parties={}, staff=[], originals=originals)
    spec = STORES[store_key]
    lay = spec["layouts"][layout_key]
    rng = np.random.default_rng(lay["seed"] * 104729 + 7)
    W, H, win = spec["w"], spec["h"], lay["window_s"]
    nid, parts, parties, staff = int(df.track_id.max()), [], {}, []
    for lid in originals:
        if rng.random() >= cfg["party_rate"]:
            continue
        g = df[df.track_id == lid]
        tt = g.t.to_numpy()
        for _ in range(1 + int(rng.random() < 0.2)):
            nid += 1
            mag, ang, spin = rng.uniform(0.6, 1.1), rng.uniform(0, 2 * np.pi), rng.uniform(-0.3, 0.3)
            x = g.x.to_numpy() + mag * np.cos(ang + spin * (tt - tt[0])) + rng.normal(0, 0.08, len(tt))
            y = g.y.to_numpy() + mag * np.sin(ang + spin * (tt - tt[0])) + rng.normal(0, 0.08, len(tt))
            parts.append(pd.DataFrame(dict(track_id=nid, t=tt, x=np.clip(x, 0.2, W - 0.2), y=np.clip(y, 0.2, H - 0.2))))
            parties[nid] = lid
    times = np.arange(0, win, 0.5)
    till = next((z for z in lay["zones"].values() if z[4] == "checkout"), None)
    if cfg.get("staff", 0) >= 1 and till:                       # the cashier: behind the register all day
        nid += 1
        staff.append(nid)
        cx, cy = (till[0] + till[2]) / 2, till[3] - 0.4
        parts.append(pd.DataFrame(dict(track_id=nid, t=times, x=cx + rng.normal(0, 0.05, len(times)), y=cy + rng.normal(0, 0.05, len(times)))))
    merch = [z for z in lay["zones"].values() if z[4] == "merchandise"]
    if cfg.get("staff", 0) >= 2 and merch:                      # the stocker: walks between shelves, pausing at each
        nid += 1
        staff.append(nid)
        x, y, t, pts, k = (merch[0][0] + merch[0][2]) / 2, (merch[0][1] + merch[0][3]) / 2, 0.0, [], 0
        while t < win:
            z = merch[k % len(merch)]
            gx, gy = rng.uniform(z[0] + 0.3, z[2] - 0.3), rng.uniform(z[1] + 0.3, z[3] - 0.3)
            steps = max(int(np.hypot(gx - x, gy - y) / 0.9 / 0.5), 1)
            for i in range(1, steps + 1):                       # walk
                pts.append((t, x + (gx - x) * i / steps, y + (gy - y) * i / steps))
                t += 0.5
            x, y = gx, gy
            for _ in range(int(rng.uniform(20, 60) / 0.5)):     # pause
                pts.append((t, x + rng.normal(0, 0.04), y + rng.normal(0, 0.04)))
                t += 0.5
            k += 1
        a = np.array([p for p in pts if p[0] < win])
        parts.append(pd.DataFrame(dict(track_id=nid, t=a[:, 0], x=a[:, 1], y=a[:, 2])))
    out = pd.concat([df] + parts, ignore_index=True) if parts else df
    return out, dict(parties=parties, staff=staff, originals=originals)


def cameras(store_key, people, n_cams=2, overlap_m=3.0, noise_m=0.15, bias_m=0.15, dropout=0.08, seed=5):
    """What several cameras would report for people who walked the simulated floor, as an agent per camera would send it.

    The floor is split along its length into n_cams views that overlap by overlap_m. Each camera sees a person only inside its
    view, places them with its own constant calibration error (bias_m) plus noise_m of jitter, misses a share of frames
    (dropout), and gives a new track id whenever it loses the person for more than 1.5 s or the person re-enters its view.
    Ids carry the camera number as agents do. Returns (tracks, truth) where truth maps each camera track id to the person."""
    from fusion import tag_camera
    spec = STORES[store_key]
    W = spec["w"]
    rng = np.random.default_rng(seed)
    bias = rng.normal(0, bias_m, (n_cams, 2))
    rows, truth, counter = [], {}, 0
    for pid, g in people.sort_values(["track_id", "t"]).groupby("track_id"):
        t, x, y = g.t.to_numpy(), g.x.to_numpy(), g.y.to_numpy()
        for c in range(n_cams):
            lo, hi = c * W / n_cams - overlap_m / 2, (c + 1) * W / n_cams + overlap_m / 2
            seen = (x >= lo) & (x <= hi) & (rng.random(len(t)) >= dropout)
            cur, last = None, -9.0
            for i in np.where(seen)[0]:
                if cur is None or t[i] - last > 1.5:
                    counter += 1
                    cur = tag_camera(counter, c)
                    truth[cur] = int(pid)
                rows.append((cur, t[i], x[i] + bias[c, 0] + rng.normal(0, noise_m), y[i] + bias[c, 1] + rng.normal(0, noise_m)))
                last = t[i]
    return pd.DataFrame(rows, columns=["track_id", "t", "x", "y"]), truth


def generate_with_sales(store_key):
    """Like generate(), plus mock sales and any companions and staff: {layout_key: (tracks_df, lines_df, truth_df)}."""
    out = {}
    for k, (df, pos_df) in generate(store_key).items():
        lines, truth = baskets(store_key, k, pos_df)
        out[k] = (crowd(store_key, k, df)[0], lines, truth)
    return out


def main(out_dir="data/demo"):
    os.makedirs(out_dir, exist_ok=True)
    for sk in STORES:
        os.makedirs(os.path.join(out_dir, sk), exist_ok=True)
        for lk, (df, lines, _truth) in generate_with_sales(sk).items():
            df.to_csv(os.path.join(out_dir, sk, f"{lk}.csv.gz"), index=False)
            lines.to_csv(os.path.join(out_dir, sk, f"{lk}.sales.csv.gz"), index=False)
            print(f"{sk}/{lk}: {len(df):,} rows, {df.track_id.nunique()} visits, {lines.txn_id.nunique()} sales")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/demo")
