"""Several cameras, one floor: turn per-camera tracks into one track per person.

Each camera runs its own agent with its own calibration to the SAME floor coordinates, and tags its track ids with its
camera number in the upper bits (track_id >> CAMERA_SHIFT, see agent --camera). Camera 0 is what every existing agent
already does, so a store with one camera is untouched and this module returns it as it came.

Two things go wrong when cameras are simply added together, and this fixes both:
  * Duplicates. A person standing where two cameras overlap is tracked twice at the same moment, so they count as two
    visitors and two stops. Tracks from different cameras that stay together over the same seconds are one person.
  * Hand-offs. A person walking from one camera's view into the next is a track that ends and another that begins. A
    track that starts soon after another camera's track ended, near where it ended, is the same person.

Same-camera tracks are never joined here (the agent's own stitching does that) and two tracks from one camera that exist
at the same time are never put together, which is what keeps two people standing close from being merged.

What it cannot do: tell two different people apart when they are inside the calibration error of each other for several
seconds in an overlap, and join tracks across a gap in coverage longer than handoff_gap_s. The report returned with the
tracks says how many of each were done and how far apart the cameras place the same person, which is the number to watch
when checking the cameras' calibration against each other.
"""
import numpy as np
import pandas as pd

import thresholds
from config import SAMPLE_HZ

CAMERA_SHIFT = 48


def camera_of(track_ids):
    return np.asarray(track_ids, dtype="int64") >> CAMERA_SHIFT


def tag_camera(track_id, camera):
    return (int(camera) << CAMERA_SHIFT) | int(track_id)


def _report(df, cameras=1, enabled=True):
    n = int(df["track_id"].nunique()) if len(df) else 0
    return dict(cameras=cameras, enabled=enabled, tracks_in=n, tracks_out=n, overlap_merges=0, handoffs=0, overlap_error_m=None,
                overlap_pairs=0)


def fuse(df, params=None, hz=SAMPLE_HZ, clusters=None):
    """df: track_id, t, x, y. Returns (fused_df, report). If `clusters` is a dict it is filled with {fused id: member ids}."""
    p = {**thresholds.defaults(), **(params or {})}
    if df.empty:
        return df, _report(df)
    cams = set(camera_of(df["track_id"].unique()).tolist())
    rep = _report(df, cameras=len(cams), enabled=p["fusion_enabled"])
    if len(cams) < 2 or not p["fusion_enabled"]:
        return df, rep

    d = df[["track_id", "t", "x", "y"]].copy()
    d["k"] = np.round(d["t"].to_numpy() * hz).astype(int)
    d["off"] = np.abs(d["t"].to_numpy() - d["k"].to_numpy() / hz)
    d = d.sort_values(["track_id", "k", "off"]).drop_duplicates(["track_id", "k"])
    T, cam = {}, {}
    for tid, g in d.groupby("track_id"):
        T[int(tid)] = (g["k"].to_numpy(), g["x"].to_numpy(), g["y"].to_numpy())
        cam[int(tid)] = int(camera_of([tid])[0])
    first = {t: v[0][0] for t, v in T.items()}
    last = {t: v[0][-1] for t, v in T.items()}

    parent = {t: t for t in T}
    spans = {t: {cam[t]: [(first[t], last[t])]} for t in T}        # per cluster: camera -> intervals it already holds
    ends = {t: (last[t], T[t][1][-1], T[t][2][-1]) for t in T}     # per cluster: (last bin, x, y) of its latest point
    starts = {t: (first[t], T[t][1][0], T[t][2][0]) for t in T}

    def root(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    def conflict(ra, rb):
        """Joining must not put two tracks of one camera at the same moment into one person."""
        for c, iv in spans[ra].items():
            for (a0, a1) in iv:
                for (b0, b1) in spans[rb].get(c, []):
                    if min(a1, b1) - max(a0, b0) >= 1:
                        return True
        return False

    def union(ra, rb):
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        parent[hi] = lo
        for c, iv in spans[hi].items():
            spans[lo].setdefault(c, []).extend(iv)
        if ends[hi][0] > ends[lo][0]:
            ends[lo] = ends[hi]
        if starts[hi][0] < starts[lo][0]:
            starts[lo] = starts[hi]
        return lo

    # 1. Duplicates: different cameras, together over the same seconds.
    min_bins = max(1, int(round(p["fuse_min_overlap_s"] * hz)))
    order = sorted(T, key=lambda t: first[t])
    box = {t: (T[t][1].min(), T[t][1].max(), T[t][2].min(), T[t][2].max()) for t in T}
    accepted = []
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            if first[b] > last[a]:
                break
            if cam[a] == cam[b] or min(last[a], last[b]) - first[b] + 1 < min_bins:
                continue
            ba, bb, m = box[a], box[b], p["fuse_dist_m"]
            if ba[0] - m > bb[1] or bb[0] - m > ba[1] or ba[2] - m > bb[3] or bb[2] - m > ba[3]:
                continue
            _, ia, ib = np.intersect1d(T[a][0], T[b][0], assume_unique=True, return_indices=True)
            if len(ia) < min_bins:
                continue
            dist = np.hypot(T[a][1][ia] - T[b][1][ib], T[a][2][ia] - T[b][2][ib])
            if (dist <= p["fuse_dist_m"]).mean() >= p["fuse_frac"]:
                accepted.append((float(np.median(dist)), a, b))
    errs = []
    for score, a, b in sorted(accepted):
        ra, rb = root(a), root(b)
        if ra != rb and not conflict(ra, rb):
            union(ra, rb)
            rep["overlap_merges"] += 1
            errs.append(score)
    rep["overlap_pairs"] = len(accepted)
    rep["overlap_error_m"] = float(np.median(errs)) if errs else None

    # 2. Hand-offs: a cluster that ends, another camera's cluster that begins soon after, near where it ended.
    if p["handoff_gap_s"] > 0:
        roots = sorted({root(t) for t in T}, key=lambda r: starts[r][0])
        cand = []
        for ra in roots:
            ka, xa, ya = ends[ra]
            for rb in roots:
                kb, xb, yb = starts[rb]
                gap = (kb - ka) / hz
                if rb == ra or gap < 0 or gap > p["handoff_gap_s"]:
                    continue
                last_cams = {c for c, iv in spans[ra].items() if any(i1 == ka for _, i1 in iv)}
                first_cams = {c for c, iv in spans[rb].items() if any(i0 == kb for i0, _ in iv)}
                if not last_cams or not first_cams or last_cams & first_cams:
                    continue                                    # same camera: that is the agent's stitching, not ours
                dist = float(np.hypot(xb - xa, yb - ya))
                if dist <= p["handoff_base_m"] + p["handoff_speed_mps"] * gap:
                    cand.append((dist, ra, rb))
        used_out, used_in = set(), set()
        for dist, ra, rb in sorted(cand):
            if ra in used_out or rb in used_in:
                continue
            xa, xb = root(ra), root(rb)
            if xa == xb or conflict(xa, xb):
                continue
            used_out.add(ra)
            used_in.add(rb)
            union(xa, xb)
            rep["handoffs"] += 1

    # 3. One track per person: members' positions averaged over the bins they share.
    new_id = {t: root(t) for t in T}
    multi = {r for r in set(new_id.values()) if sum(1 for t in T if new_id[t] == r) > 1}
    d["pid"] = d["track_id"].map(new_id)
    single = d[~d["pid"].isin(multi)]
    merged = (d[d["pid"].isin(multi)].groupby(["pid", "k"], as_index=False).agg(x=("x", "mean"), y=("y", "mean")))
    out = pd.concat([
        pd.DataFrame(dict(track_id=single["pid"].to_numpy(), t=single["k"].to_numpy() / hz, x=single["x"].to_numpy(), y=single["y"].to_numpy())),
        pd.DataFrame(dict(track_id=merged["pid"].to_numpy(), t=merged["k"].to_numpy() / hz, x=merged["x"].to_numpy(), y=merged["y"].to_numpy()))],
        ignore_index=True).sort_values(["track_id", "t"]).reset_index(drop=True)
    rep["tracks_out"] = int(out["track_id"].nunique())
    if clusters is not None:
        for t, r in new_id.items():
            clusters.setdefault(r, []).append(t)
    return out, rep
