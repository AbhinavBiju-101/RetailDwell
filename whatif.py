"""What-if: where would this zone get more stops if it stood somewhere else?

A first-order estimate from the tracks already recorded, not a simulation of shoppers:

    stops at the new spot = (share who stop now / share who pass now) x share who would pass the new spot

"Pass" means a shopper spent at least MIN_PRESENCE_S within WHATIF_CATCH_M of the zone's edge (stopping in the zone counts).
The first ratio is the zone's capture rate, kept as it is. The second is measured on the same tracks at the new position.
It ignores that moving a zone changes where people walk, that a zone in a new place may be noticed differently, and what
happens to the zone that used to be next to it. `sampling_lo` and `sampling_hi` come from resampling visits: they show how much the
recorded visits can say, not how wrong the model is (in tests they covered the true value 23% of the time). The page quotes the
measured test error instead (config WHATIF_ERR_*). Loiterers are left out; staff already are.
"""
import numpy as np
import pandas as pd

import flow
from config import (MIN_PRESENCE_S, SAMPLE_HZ, WHATIF_BOOT, WHATIF_CATCH_M, WHATIF_MIN_EXPOSED, WHATIF_STEP_M)

NEED_PTS = int(np.ceil(MIN_PRESENCE_S * SAMPLE_HZ))


class WhatIfError(ValueError):
    """A request the model cannot answer; the message is meant for the person."""


def _prepare(R):
    """Shopper visits as arrays: x, y and the visit index of every point, plus which visits stop in each zone."""
    seqs, n = flow.zone_sequences(R, "shoppers")
    lab = R["labels"]
    ids = np.sort(lab[lab.mission != flow.LOITERER].track_id.to_numpy())
    if len(ids) < WHATIF_MIN_EXPOSED:
        raise WhatIfError("Too few shopper visits in this recording to estimate anything.")
    pos = {t: i for i, t in enumerate(ids)}
    df = R["df"]
    df = df[df.track_id.isin(set(ids))]
    vi = df.track_id.map(pos).to_numpy()
    stops = {}
    for tid, seq in seqs.items():
        for z in set(seq):
            stops.setdefault(z, np.zeros(len(ids), bool))[pos[tid]] = True
    return dict(n=len(ids), x=df.x.to_numpy(), y=df.y.to_numpy(), vi=vi, stops=stops)


def _passed(P, rect, margin=WHATIF_CATCH_M):
    x0, y0, x1, y1 = rect
    m = (P["x"] >= x0 - margin) & (P["x"] <= x1 + margin) & (P["y"] >= y0 - margin) & (P["y"] <= y1 + margin)
    return np.bincount(P["vi"][m], minlength=P["n"]) >= NEED_PTS


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _merch(R, zone):
    z = R["zones"].get(zone)
    if z is None or z[4] != "merchandise":
        raise WhatIfError("Pick a merchandise zone to move.")
    return z


def _check_new(R, zone, new, vacating=()):
    """The new rectangle must sit on the floor and clear every zone that is staying put."""
    if new[0] < -1e-9 or new[1] < -1e-9 or new[2] > R["w"] + 1e-9 or new[3] > R["h"] + 1e-9:
        raise WhatIfError("That position falls outside the floor.")
    for name, o in R["zones"].items():
        if name != zone and name not in vacating and _overlaps(new, o[:4]):
            raise WhatIfError(f"That position overlaps {name}. Move it to open floor.")


def _moved(R, zone, cx=None, cy=None, dx=None, dy=None):
    z = _merch(R, zone)
    w, h = z[2] - z[0], z[3] - z[1]
    if cx is not None and cy is not None:
        nx0, ny0 = cx - w / 2, cy - h / 2
    else:
        nx0, ny0 = z[0] + (dx or 0.0), z[1] + (dy or 0.0)
    new = (nx0, ny0, nx0 + w, ny0 + h)
    _check_new(R, zone, new)
    return tuple(z[:4]), new


def _estimate(P, zone, old, new, seed=0, boot=WHATIF_BOOT):
    stop = P["stops"].get(zone, np.zeros(P["n"], bool))
    eo, en = _passed(P, old), _passed(P, new)
    if eo.sum() < WHATIF_MIN_EXPOSED:
        raise WhatIfError(f"Only {int(eo.sum())} shoppers passed {zone}, so its capture rate cannot be estimated (needs {WHATIF_MIN_EXPOSED}).")
    n = P["n"]

    def pred(idx):
        e = eo[idx].sum()
        return min(1.0, (stop[idx].sum() / e) * en[idx].sum() / n) if e else np.nan

    now, est = stop.sum() / n, pred(np.arange(n))
    rng = np.random.default_rng(seed)
    d = []
    for _ in range(boot):
        idx = rng.integers(0, n, n)
        p = pred(idx)
        if not np.isnan(p):
            d.append(p - stop[idx].sum() / n)
    lo, hi = (float(np.percentile(d, 10)), float(np.percentile(d, 90))) if d else (np.nan, np.nan)
    return dict(zone=zone, old=[round(float(v), 2) for v in old], new=[round(float(v), 2) for v in new], n_visits=int(n),
                passers_now=int(eo.sum()), passers_new=int(en.sum()), capture=float(stop.sum() / eo.sum()),
                reach_now=float(now), reach_pred=float(est), change=float(est - now), sampling_lo=lo, sampling_hi=hi)


def what_if(R, zone, cx=None, cy=None, dx=None, dy=None, seed=0, boot=WHATIF_BOOT):
    """Predicted stops for `zone` if it moved (centre cx, cy or shift dx, dy). Raises WhatIfError with a reason."""
    old, new = _moved(R, zone, cx, cy, dx, dy)
    return _estimate(_prepare(R), zone, old, new, seed, boot)


def swap(R, a, b, seed=0, boot=WHATIF_BOOT):
    """Each zone takes the other's place (centre on centre, each keeping its own size, nudged in from a wall if needed). Returns the two estimates, with no net total."""
    if a == b:
        raise WhatIfError("Pick two different zones to swap.")
    za, zb = _merch(R, a), _merch(R, b)

    def centred(z, other):
        """Centre on the other zone's centre; a zone that would hang over a wall is nudged back onto the floor."""
        w, h = z[2] - z[0], z[3] - z[1]
        cx, cy = (other[0] + other[2]) / 2, (other[1] + other[3]) / 2
        x0 = min(max(cx - w / 2, 0.0), max(R["w"] - w, 0.0))
        y0 = min(max(cy - h / 2, 0.0), max(R["h"] - h, 0.0))
        return (x0, y0, x0 + w, y0 + h)

    na, nb = centred(za, zb), centred(zb, za)
    for z, n in ((a, na), (b, nb)):
        _check_new(R, z, n, vacating=(a, b))
    if _overlaps(na, nb):
        raise WhatIfError(f"{a} and {b} would overlap after the swap, because their sizes differ too much.")
    P = _prepare(R)
    ra, rb = _estimate(P, a, tuple(za[:4]), na, seed, boot), _estimate(P, b, tuple(zb[:4]), nb, seed, boot)
    # No total is returned on purpose: in tests the net change of a swap was predicted no better than "nothing changes"
    # (see docs/context.md), so only each zone's own estimate is offered.
    return dict(a=ra, b=rb)


def scan(R, zone, step=WHATIF_STEP_M):
    """Predicted share of shoppers stopping at `zone` for every open position on the floor (centre on a grid)."""
    z = _merch(R, zone)
    P = _prepare(R)
    stop = P["stops"].get(zone, np.zeros(P["n"], bool))
    eo = _passed(P, z[:4])
    if eo.sum() < WHATIF_MIN_EXPOSED:
        raise WhatIfError(f"Only {int(eo.sum())} shoppers passed {zone}, so its capture rate cannot be estimated (needs {WHATIF_MIN_EXPOSED}).")
    cap, w, h = stop.sum() / eo.sum(), z[2] - z[0], z[3] - z[1]
    xs = np.arange(w / 2, R["w"] - w / 2 + 1e-9, step)
    ys = np.arange(h / 2, R["h"] - h / 2 + 1e-9, step)
    grid = np.full((len(ys), len(xs)), np.nan)
    others = [o[:4] for k, o in R["zones"].items() if k != zone]
    for j, cy in enumerate(ys):
        for i, cx in enumerate(xs):
            r = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            if not any(_overlaps(r, o) for o in others):
                grid[j, i] = min(1.0, cap * _passed(P, r).sum() / P["n"])
    return dict(zone=zone, xs=xs.tolist(), ys=ys.tolist(), z=[[None if np.isnan(v) else float(v) for v in row] for row in grid],
                reach_now=float(stop.sum() / P["n"]), capture=float(cap), size=[w, h])

