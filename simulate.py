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
from stores import DEFAULT_MIX, STORES, main_path

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
            buyers.append(dict(tid=tid, mission=mission, rg=rg, track=track, t=t, speed=speed))
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
        pos_events.append((layout_key, b["tid"], t))
        seg, t = _walk(_exit_route(spot, spec), t, b["speed"], rg); track += seg
        done.append((b["tid"], b["mission"], track))

    for tid, mission, track in done:
        for (tt, x, y) in track:
            rows.append((layout_key, tid, tt, x, y, mission))
    df = pd.DataFrame(rows, columns=["layout", "track_id", "t", "x", "y", "true_mission"])
    df["x"] = df["x"].clip(-1, spec["w"] + 1).round(2)
    df["y"] = df["y"].clip(-1, spec["h"] + 1).round(2)
    df["t"] = df["t"].round(2)
    pos_df = pd.DataFrame(pos_events, columns=["layout", "track_id", "t_checkout_end"])
    return df.sort_values(["track_id", "t"]).reset_index(drop=True), pos_df


def generate(store_key):
    """All layouts of one demo store: {layout_key: (tracks_df, pos_df)}."""
    return {k: simulate_layout(store_key, k) for k in STORES[store_key]["layouts"]}


def main(out_dir="data/demo"):
    os.makedirs(out_dir, exist_ok=True)
    for sk in STORES:
        os.makedirs(os.path.join(out_dir, sk), exist_ok=True)
        for lk, (df, pos) in generate(sk).items():
            df.to_csv(os.path.join(out_dir, sk, f"{lk}.csv.gz"), index=False)
            print(f"{sk}/{lk}: {len(df):,} rows, {df.track_id.nunique()} visits, {len(pos)} purchases")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/demo")
