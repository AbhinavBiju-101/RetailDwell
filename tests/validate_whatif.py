"""Score the what-if model against simulator ground truth.  Run:  python -m tests.validate_whatif [out.json]
                                                         or:  python -m tests.validate_whatif swaps [out.json]  (zone swaps)

For every merchandise zone of every demo store's layout A, move the zone to open positions (two random ones, plus its
layout-B position when it moved there), re-simulate that layout, and compare the stop share the model predicted from A
with the share the simulation produced. Baseline: "nothing changes". Judged on mean absolute error in share points and
on whether the direction of a clear change (5 points or more) was called right. The simulator's own rules (reach falls
with distance from the walking route) are what the model is measured against, so this shows the method works where its
assumptions hold, not that it works in a real shop."""
import copy
import json
import sys
import os

import numpy as np

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import analytics as an
import simulate
import stores
import whatif


def actual_share(R, zone):
    P = whatif._prepare(R)
    return float(P["stops"].get(zone, np.zeros(P["n"], bool)).sum() / P["n"])


def run(seed=0, out=None):
    rng = np.random.default_rng(seed)
    rows = []
    for sk, spec in list(stores.STORES.items()):
        A = spec["layouts"]["A"]
        dfA, _ = simulate.simulate_layout(sk, "A")
        zA = {n: list(v) for n, v in A["zones"].items()}
        RA = an.analyse_layout(dfA, zA, spec["w"], spec["h"], 300)
        scans = {}
        for zone, z in zA.items():
            if z[4] != "merchandise":
                continue
            try:
                sc = whatif.scan(RA, zone)
            except whatif.WhatIfError:
                continue
            open_pts = [(x, y) for j, y in enumerate(sc["ys"]) for i, x in enumerate(sc["xs"]) if sc["z"][j][i] is not None]
            targets = [("random", open_pts[k]) for k in rng.choice(len(open_pts), 2, replace=False)]
            B = spec["layouts"].get("B")
            if B and list(B["zones"][zone][:4]) != list(z[:4]):
                b = B["zones"][zone]
                targets.append(("real B", ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)))
            for kind, (cx, cy) in targets:
                try:
                    wi = whatif.what_if(RA, zone, cx=cx, cy=cy, boot=50)
                except whatif.WhatIfError as e:
                    rows.append(dict(store=sk, zone=zone, kind=kind, skipped=str(e)))
                    print(f"{sk:18s} {zone:14s} {kind:7s} skipped: {e}", flush=True)
                    continue
                spec_t = copy.deepcopy(spec)
                zT = {n: tuple(v) for n, v in A["zones"].items()}
                zT[zone] = tuple(wi["new"]) + (z[4],)
                spec_t["layouts"]["T"] = dict(A, zones=zT, seed=A["seed"] + 1000 + len(rows))
                old = simulate.STORES
                simulate.STORES = {**old, sk: spec_t}
                try:
                    dfT, _ = simulate.simulate_layout(sk, "T")
                finally:
                    simulate.STORES = old
                RT = an.analyse_layout(dfT, {n: list(v) for n, v in zT.items()}, spec["w"], spec["h"], 300)
                act = actual_share(RT, zone)
                rows.append(dict(store=sk, zone=zone, kind=kind, now=wi["reach_now"], pred=wi["reach_pred"], actual=act,
                                 lo=wi["reach_now"] + wi["sampling_lo"], hi=wi["reach_now"] + wi["sampling_hi"]))
                print(f"{sk:18s} {zone:14s} {kind:7s} now {wi['reach_now']:.2f} pred {wi['reach_pred']:.2f} actual {act:.2f}", flush=True)
    ok = [r for r in rows if "pred" in r]
    mae_m = float(np.mean([abs(r["pred"] - r["actual"]) for r in ok]))
    mae_b = float(np.mean([abs(r["now"] - r["actual"]) for r in ok]))
    big = [r for r in ok if abs(r["actual"] - r["now"]) >= 0.05]
    dir_ok = sum(np.sign(r["pred"] - r["now"]) == np.sign(r["actual"] - r["now"]) for r in big)
    cover = float(np.mean([r["lo"] <= r["actual"] <= r["hi"] for r in ok]))
    err = np.abs([r["pred"] - r["actual"] for r in ok])
    res = dict(n=len(ok), mae_model=mae_m, mae_baseline=mae_b, n_clear=len(big), direction_right=int(dir_ok), sampling_range_covers=cover, err_median=float(np.median(err)), err_80=float(np.quantile(err, 0.8)), err_max=float(err.max()), rows=rows)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
    if out:
        json.dump(res, open(out, "w"), indent=1)
    return res


def run_swaps(seed=0, per_store=3, out=None):
    """Swap random pairs of shelf zones, re-simulate each swapped layout, and score both zones' predicted stop shares.
    (A variant that dropped the displaced zone's own stoppers from the passers at its spot was tried on fresh pairs and
    scored 40 points of error against 11, so it was removed.)"""
    """Swap random pairs of shelf zones, re-simulate, and score both zones' predicted stop shares."""
    rng = np.random.default_rng(seed)
    rows, k = [], 0
    for sk, spec in list(stores.STORES.items()):
        A = spec["layouts"]["A"]
        dfA, _ = simulate.simulate_layout(sk, "A")
        zA = {n: list(v) for n, v in A["zones"].items()}
        RA = an.analyse_layout(dfA, zA, spec["w"], spec["h"], 300)
        merch = [n for n, z in zA.items() if z[4] == "merchandise"]
        pairs = [(a, b) for i, a in enumerate(merch) for b in merch[i + 1:]]
        good = []
        for a, b in pairs:
            try:
                good.append((a, b, whatif.swap(RA, a, b, boot=30)))
            except whatif.WhatIfError as e:
                print(f"{sk:18s} {a} <-> {b} skipped: {e}", flush=True)
        for j in rng.permutation(len(good))[:per_store]:
            a, b, sw = good[j]
            k += 1
            spec_t = copy.deepcopy(spec)
            zT = {n: tuple(v) for n, v in A["zones"].items()}
            zT[a] = tuple(sw["a"]["new"]) + (zA[a][4],)
            zT[b] = tuple(sw["b"]["new"]) + (zA[b][4],)
            spec_t["layouts"]["T"] = dict(A, zones=zT, seed=A["seed"] + 2000 + k)
            old = simulate.STORES
            simulate.STORES = {**old, sk: spec_t}
            try:
                dfT, _ = simulate.simulate_layout(sk, "T")
            finally:
                simulate.STORES = old
            RT = an.analyse_layout(dfT, {n: list(v) for n, v in zT.items()}, spec["w"], spec["h"], 300)
            for zone, r in ((a, sw["a"]), (b, sw["b"])):
                act = actual_share(RT, zone)
                rows.append(dict(store=sk, pair=f"{a} <-> {b}", zone=zone, now=r["reach_now"], pred=r["reach_pred"], actual=act))
                print(f"{sk:18s} {a} <-> {b:14s} {zone:14s} now {r['reach_now']:.2f} pred {r['reach_pred']:.2f} actual {act:.2f}", flush=True)
    big = [r for r in rows if abs(r["actual"] - r["now"]) >= 0.05]
    res = dict(n=len(rows), n_clear=len(big), mae_baseline=float(np.mean([abs(r["now"] - r["actual"]) for r in rows])), rows=rows)
    err = np.abs([r["pred"] - r["actual"] for r in rows])
    res.update(mae_model=float(err.mean()), direction_right=int(sum(np.sign(r["pred"] - r["now"]) == np.sign(r["actual"] - r["now"]) for r in big)),
               err_median=float(np.median(err)), err_80=float(np.quantile(err, 0.8)), err_max=float(err.max()))
    print(json.dumps({k_: v for k_, v in res.items() if k_ != "rows"}, indent=1))
    if out:
        json.dump(res, open(out, "w"), indent=1)
    return res


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "swaps":
        run_swaps(seed=int(sys.argv[3]) if len(sys.argv) > 3 else 0, per_store=int(sys.argv[4]) if len(sys.argv) > 4 else 3,
                  out=sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        run(out=sys.argv[1] if len(sys.argv) > 1 else None)
