"""Zone flow: which zone shoppers go to next, where they turn back, where a visit ends.

Input is the analysis result `R` from `analytics.analyse_layout` (tracks already carry a zone name). Each
shopper becomes the ordered list of named zones they spent at least `MIN_PRESENCE_S` in; circulation space
is skipped, the entrance at either end is dropped and repeats are collapsed, so a path reads "Produce, Offers, Tills". Only counts leave this
module: routes taken by fewer than `FLOW_MIN_EDGE` shoppers are never drawn or named.
"""
import math
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from config import (DWELL_MIN_S, FLOW_MAX_STEPS, FLOW_MIN_EDGE, FLOW_MIN_N, FLOW_MIN_Z, FLOW_MIN_ZONES, FLOW_TOP_ZONES,
                    FLOW_TURN_MIN_RATIO, FLOW_TURN_MIN_SHARE, MIN_PRESENCE_S, SAMPLE_HZ)

DT = 1.0 / SAMPLE_HZ
LOITERER = "loiterer / waiter"
SEGMENTS = ["shoppers", "directed", "exploratory", "pass-through", LOITERER]
OTHER, LEAVES, MORE = "Other zones", "Leaves the store", "Goes on"


def ids_stopping_in(R, zone):
    """Track ids that spent MIN_PRESENCE_S or more in one go inside `zone`."""
    df = R["df"]
    if not len(df):
        return set()
    df = df[["track_id", "t", "zone"]].sort_values(["track_id", "t"], kind="stable")
    inside = df.zone == zone
    run = ((df.track_id != df.track_id.shift()) | (inside != inside.shift())).cumsum()
    g = df[inside].groupby(run[inside]).agg(track_id=("track_id", "first"), n=("t", "size"))
    return set(g[g.n * DT >= MIN_PRESENCE_S].track_id.astype(int))


def zone_sequences(R, segment="shoppers", ids_only=None):
    """{track_id: [zone, ...]} for the chosen visit type. `shoppers` means everyone except loiterers.
    `ids_only` narrows to those track ids (the Journeys filters)."""
    lab = R["labels"]
    if segment == "shoppers":
        ids = set(lab[lab.mission != LOITERER].track_id)
    elif segment in SEGMENTS:
        ids = set(lab[lab.mission == segment].track_id)
    else:
        raise ValueError(f"unknown visit type: {segment}")
    if ids_only is not None:
        ids &= set(ids_only)
    df = R["df"]
    if not len(df) or not ids:
        return {}, len(ids)
    df = df[df.track_id.isin(ids)][["track_id", "t", "zone"]]
    df = df.sort_values(["track_id", "t"], kind="stable")
    new_run = (df.track_id != df.track_id.shift()) | (df.zone != df.zone.shift())
    run = new_run.cumsum()
    runs = df.groupby(run).agg(track_id=("track_id", "first"), zone=("zone", "first"), n=("t", "size"))
    runs = runs[(runs.n * DT >= MIN_PRESENCE_S) & (runs.zone != "Circulation")]
    keep = (runs.track_id != runs.track_id.shift()) | (runs.zone != runs.zone.shift())
    runs = runs[keep]
    ztype = {k: v[4] for k, v in R["zones"].items()}
    seqs = {}
    for t, g in runs.groupby("track_id"):
        seq = g.zone.tolist()
        while seq and ztype.get(seq[0]) == "entrance":      # people come in and go out through the entrance: it is the
            seq.pop(0)                                       # start and end of a path, not a stop on it
        while seq and ztype.get(seq[-1]) == "entrance":
            seq.pop()
        if seq:
            seqs[int(t)] = seq
    return seqs, len(ids)


def _collapse(seq):
    out = []
    for z in seq:
        if not out or out[-1] != z:
            out.append(z)
    return out


def _sankey(seqs, reach, max_steps=FLOW_MAX_STEPS, top=FLOW_TOP_ZONES, min_edge=FLOW_MIN_EDGE):
    keep = {z for z, _ in reach.most_common(top)}
    links = Counter()
    for seq in seqs:
        seq = _collapse([z if z in keep else OTHER for z in seq])
        n = min(len(seq), max_steps)
        for k in range(n - 1):
            links[((k, seq[k]), (k + 1, seq[k + 1]))] += 1
        links[((n - 1, seq[n - 1]), ("end", LEAVES if len(seq) <= max_steps else MORE))] += 1
    total = sum(links.values())
    shown = {e: v for e, v in links.items() if v >= min_edge}
    nodes, index = [], {}

    def node(key):
        if key not in index:
            index[key] = len(nodes)
            nodes.append(dict(label=key[1], step=key[0] if key[0] != "end" else max_steps))
        return index[key]

    out = []
    for (a, b), v in sorted(shown.items(), key=lambda kv: (str(kv[0][0][0]), -kv[1])):
        out.append(dict(source=node(a), target=node(b), value=int(v)))
    return dict(nodes=nodes, links=out, shown_share=(sum(shown.values()) / total) if total else 0.0, steps=max_steps)


def analyse_flow(R, segment="shoppers", ids_only=None):
    """Counts of where shoppers go. Returns rows per zone, the Sankey, turn-back findings and a note."""
    seqs, n_visits = zone_sequences(R, segment, ids_only)
    zones = R["zones"]
    ztype = {k: v[4] for k, v in zones.items()}
    paths = [s for s in seqs.values() if s]
    res = dict(segment=segment, n_visits=int(n_visits), n_paths=len(paths), rows=[], sankey=None, findings=[], note=None)
    if len(paths) < FLOW_MIN_EDGE:
        res["note"] = ("Fewer than %d visits in this group stopped in a named zone, so no routes are shown." % FLOW_MIN_EDGE)
        return res

    reach, first, last = Counter(), Counter(), Counter()
    nxt, prev = defaultdict(Counter), defaultdict(Counter)
    arrive, turn = Counter(), Counter()
    turn_from = defaultdict(Counter)
    for seq in paths:
        for z in set(seq):
            reach[z] += 1
        first[seq[0]] += 1
        last[seq[-1]] += 1
        for a, b in zip(seq, seq[1:]):
            nxt[a][b] += 1
            prev[b][a] += 1
        for i in range(1, len(seq)):
            if ztype.get(seq[i - 1]) != "merchandise":       # coming back from a seat, a till or the entrance is not turning away
                continue
            arrive[seq[i]] += 1
            if i + 1 < len(seq) and seq[i + 1] == seq[i - 1]:
                turn[seq[i]] += 1
                turn_from[seq[i]][seq[i - 1]] += 1

    rows = []
    for z, n in reach.most_common():
        go = nxt[z]
        n_go = sum(go.values())
        best = go.most_common(1)[0] if go else None
        show_next = best is not None and best[1] >= FLOW_MIN_EDGE
        rows.append(dict(
            zone=z, type=ztype.get(z, "merchandise"), reached=int(n), share=n / len(paths),
            first_pct=first[z] / n, leaves_pct=last[z] / n,
            next_zone=best[0] if show_next else None, next_pct=(best[1] / n_go) if show_next else None,
            arrivals=int(arrive[z]), turn_pct=(turn[z] / arrive[z]) if arrive[z] >= FLOW_MIN_N else None))
    res["rows"] = rows
    res["sankey"] = _sankey(paths, reach)

    # turn-backs: merchandise zones only (a till or the entrance is supposed to be the end of the line)
    judge = [r for r in rows if r["type"] == "merchandise" and r["turn_pct"] is not None]
    if len(judge) >= FLOW_MIN_ZONES:
        med = float(np.median([r["turn_pct"] for r in judge]))
        p0 = min(max(med, 0.05), 0.95)
        res["turn_median"] = med
        for r in sorted(judge, key=lambda r: -r["turn_pct"]):
            se = math.sqrt(p0 * (1 - p0) / r["arrivals"])
            z = (r["turn_pct"] - med) / se
            if r["turn_pct"] >= FLOW_TURN_MIN_SHARE and r["turn_pct"] >= FLOW_TURN_MIN_RATIO * med and z >= FLOW_MIN_Z:
                src, cnt = turn_from[r["zone"]].most_common(1)[0]
                res["findings"].append(dict(
                    kind="Turn-back", tone="warn", zone=r["zone"],
                    text=(f"{r['zone']}: {r['turn_pct']:.0%} of the {r['arrivals']} arrivals from another shelf zone went straight back to it "
                          f"(store median {med:.0%}), most often {src}. People walk in and turn round. "
                          "If the zone is a dead end that is expected, so put the draw item at the far end of the loop. "
                          "If shoppers could walk on through, something is turning them back: check the sightline beyond it.")))
    else:
        res["turn_median"] = None
    if res["sankey"]["shown_share"] < 0.6:
        res["note"] = ("Most routes were taken by fewer than %d shoppers and are left out of the diagram. "
                       "The table still counts every shopper." % FLOW_MIN_EDGE)
    return res
