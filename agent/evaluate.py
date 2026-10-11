"""Score camera-agent output against ground-truth floor tracks, in the terms the product reports.

    python -m agent evaluate --truth truth.csv --est tracks.csv --zones zones.json --width 12 --height 8

Both files hold track_id, t, x, y with x and y in metres on the floor plane and t in seconds. Both are resampled
to the store's 2 Hz, matched frame by frame (Hungarian, within a gate), and then run through the same zone and
stop logic as the app, so the errors are the ones a retailer would see:

  tracking     recall, precision, position error, identity switches, fragments, ghost tracks
  zones        how often a matched point lands in the same zone as the truth
  per person   did the person reach / stop in each zone, and how long: truth vs agent, after matching
  as shown     stopped share per zone from the raw tracks, which is what an experiment compares. Fragmented or
               ghost tracks inflate the visit count here, so this is the number that decides if a result can be trusted

What it cannot tell you: whether the ground truth is right, or how the agent behaves on footage unlike the
data you scored. MMPTRACK ground truth is a staged lab scene (see docs/validation-data.md).
"""
import glob
import json
import os
import re

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

import analytics as an
from config import SAMPLE_HZ

COLS = ["track_id", "t", "x", "y"]
_BIG = 1e6


class EvalError(ValueError):
    """Bad input to the evaluation, with a message fit to show to the person running it."""


# ---------------------------------------------------------------- loading
def load_tracks(path):
    """Read track_id, t, x, y from a CSV or .csv.gz. Other columns are ignored."""
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise EvalError(f"Could not read {path}: {e}")
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise EvalError(f"{path} needs the columns track_id, t, x, y. Missing: {', '.join(missing)}.")
    df = df[COLS].apply(pd.to_numeric, errors="coerce").dropna()
    if df.empty:
        raise EvalError(f"{path} has no usable rows.")
    df["track_id"] = df["track_id"].astype(int)
    return df.reset_index(drop=True)


def load_mmptrack_truth(folder, voxel_m, origin=(0.0, 0.0), fps=15.0):
    """Ground truth from an MMPTRACK folder of topdown_<frame>.csv files (rows: id, x, y, z), as documented on
    the dataset page: person footpoints in a discretised world grid. x and y become metres as
    origin + grid_index * voxel_m, and t is (frame - first frame) / fps.

    UNVERIFIED against the real files: the page names a voxel size and space size in the calibration files but
    not their format, so voxel_m and origin must be read from those yourself, and the first run should be checked
    by plotting a few tracks over the scene. Swap or flip axes in a copy of the CSV if they do not line up."""
    files = sorted(glob.glob(os.path.join(folder, "topdown_*.csv")))
    if not files:
        raise EvalError(f"No topdown_<frame>.csv files in {folder}.")
    if voxel_m is None or voxel_m <= 0:
        raise EvalError("Give the voxel size in metres (--voxel-m), from the dataset's calibration file.")
    rows = []
    for f in files:
        frame = int(re.search(r"topdown_(\d+)\.csv$", os.path.basename(f)).group(1))
        try:
            d = pd.read_csv(f, header=None, names=["track_id", "gx", "gy", "z"])
        except pd.errors.EmptyDataError:
            continue
        d = d.apply(pd.to_numeric, errors="coerce").dropna(subset=["track_id", "gx", "gy"])
        for tid, gx, gy in zip(d.track_id, d.gx, d.gy):
            rows.append((int(tid), frame, gx, gy))
    if not rows:
        raise EvalError(f"The topdown files in {folder} hold no people.")
    df = pd.DataFrame(rows, columns=["track_id", "frame", "gx", "gy"])
    first = df.frame.min()
    return pd.DataFrame({"track_id": df.track_id, "t": (df.frame - first) / fps,
                         "x": origin[0] + df.gx * voxel_m, "y": origin[1] + df.gy * voxel_m})


def load_zones(path):
    """{name: [x0, y0, x1, y1, type]} as a JSON file, the same shape the app stores for a layout."""
    try:
        with open(path) as f:
            raw = json.load(f)
        zones = {str(k): [float(v[0]), float(v[1]), float(v[2]), float(v[3]), str(v[4])] for k, v in raw.items()}
    except Exception:
        raise EvalError('Zones must be JSON like {"Shelf A": [1.8, 4.3, 4.2, 6.7, "merchandise"]}.')
    if not zones:
        raise EvalError("The zones file is empty.")
    return zones


# ---------------------------------------------------------------- preparation
def to_grid(df, hz):
    """One row per track per 1/hz second bin, taking the sample nearest the bin centre. Adds column k (bin)."""
    k = np.round(df["t"].to_numpy() * hz).astype(int)
    out = df.assign(k=k, off=np.abs(df["t"].to_numpy() - k / hz)).sort_values(["track_id", "k", "off"])
    out = out.drop_duplicates(["track_id", "k"]).drop(columns="off")
    out["t"] = out["k"] / hz
    return out.sort_values(["track_id", "t"]).reset_index(drop=True)


def match_frames(gt, est, gate_m):
    """Per-bin Hungarian match of agent points to true positions, within gate_m metres."""
    gk = {k: g for k, g in gt.groupby("k")}
    rows = []
    for k, e in est.groupby("k"):
        g = gk.get(k)
        if g is None:
            continue
        gx, gy, ex, ey = g.x.to_numpy(), g.y.to_numpy(), e.x.to_numpy(), e.y.to_numpy()
        cost = np.hypot(gx[:, None] - ex[None, :], gy[:, None] - ey[None, :])
        ri, ci = linear_sum_assignment(np.where(cost <= gate_m, cost, _BIG))
        for r, c in zip(ri, ci):
            if cost[r, c] <= gate_m:
                rows.append((k, int(g.track_id.iat[r]), int(e.track_id.iat[c]), float(cost[r, c]),
                             gx[r], gy[r], ex[c], ey[c]))
    return pd.DataFrame(rows, columns=["k", "gt_id", "est_id", "err_m", "gx", "gy", "ex", "ey"])


# ---------------------------------------------------------------- the scores
def _tracking(gt, est, m, hz):
    gt_bins = set(zip(gt.track_id, gt.k))
    hit = set(zip(m.gt_id, m.k))
    per_gt = pd.Series([a for a, _ in hit]).value_counts() if hit else pd.Series(dtype=int)
    gt_len = gt.groupby("track_id").size()
    covered = (per_gt / gt_len.reindex(per_gt.index)).reindex(gt_len.index).fillna(0.0)
    est_len = est.groupby("track_id").size()
    matched_per_est = m.groupby("est_id").size().reindex(est_len.index).fillna(0)
    ghost = int(((matched_per_est / est_len) < 0.5).sum())
    switches, frags = 0, []
    for _gid, g in m.sort_values("k").groupby("gt_id"):
        ids = g.est_id.tolist()
        switches += sum(a != b for a, b in zip(ids, ids[1:]))
        frags.append(len(set(ids)))
    person_min = len(gt_bins) / hz / 60.0
    return dict(
        gt_people=int(gt.track_id.nunique()), est_tracks=int(est.track_id.nunique()),
        recall=len(hit) / max(1, len(gt_bins)), precision=len(m) / max(1, len(est)),
        median_err_m=float(m.err_m.median()) if len(m) else float("nan"),
        p90_err_m=float(m.err_m.quantile(0.9)) if len(m) else float("nan"),
        id_switches=int(switches), id_switches_per_person_min=float(switches / person_min) if person_min else float("nan"),
        tracks_per_found_person=float(np.mean(frags)) if frags else float("nan"),
        missed_people=int((covered < 0.5).sum()), ghost_tracks=ghost)


def _mapping(m, est):
    """Which true person each agent track follows: the one it shares most matched points with. Ghost tracks
    (under half their points matched) and tracks with under 3 matches follow nobody."""
    est_len = est.groupby("track_id").size()
    out = {}
    for eid, g in m.groupby("est_id"):
        top = g.gt_id.value_counts()
        if len(g) >= 3 and len(g) / est_len[eid] >= 0.5:
            out[int(eid)] = int(top.index[0])
    return out


def _zone_agreement(m, zones):
    if m.empty:
        return dict(point_agreement=float("nan"), top_confusions=[])
    zg = an.assign_zone(pd.DataFrame({"x": m.gx, "y": m.gy}), zones)
    ze = an.assign_zone(pd.DataFrame({"x": m.ex, "y": m.ey}), zones)
    bad = pd.Series(list(zip(zg[zg != ze], ze[zg != ze]))).value_counts().head(3) if (zg != ze).any() else []
    return dict(point_agreement=float((zg == ze).mean()),
                top_confusions=[dict(truth=a, agent=b, points=int(n)) for (a, b), n in (bad.items() if len(bad) else [])])


def _sets(presence, episodes, merch, persons):
    reached = {(t, z) for t, z in zip(presence.track_id, presence.zone) if z in merch and t in persons}
    e = episodes[episodes.zone.isin(merch) & episodes.track_id.isin(persons)]
    return reached, {(t, z) for t, z in zip(e.track_id, e.zone)}, e.groupby(["track_id", "zone"]).dur_s.sum()


def _prf(truth, found):
    both = len(truth & found)
    return dict(recall=both / len(truth) if truth else float("nan"), precision=both / len(found) if found else float("nan"),
                truth=len(truth), agent=len(found))


def _person_zone(gt_ev, est_ev, merch, persons):
    Rg, Sg, Dg = _sets(gt_ev[1], gt_ev[2], merch, persons)
    Re, Se, De = _sets(est_ev[1], est_ev[2], merch, persons)
    keys = sorted(Sg | Se)
    dg = Dg.reindex(pd.MultiIndex.from_tuples(keys), fill_value=0.0) if keys else pd.Series(dtype=float)
    de = De.reindex(pd.MultiIndex.from_tuples(keys), fill_value=0.0) if keys else pd.Series(dtype=float)
    err = (de - dg).abs()
    per_zone = []
    for z in sorted(merch):
        sg = {p for p, zz in Sg if zz == z}
        se = {p for p, zz in Se if zz == z}
        tg = float(Dg[Dg.index.get_level_values(1) == z].sum()) if len(Dg) else 0.0
        te = float(De[De.index.get_level_values(1) == z].sum()) if len(De) else 0.0
        per_zone.append(dict(zone=z, stoppers_truth=len(sg), stoppers_agent=len(se), stoppers_both=len(sg & se),
                             dwell_truth_s=tg, dwell_agent_s=te, dwell_bias=(te - tg) / tg if tg else float("nan")))
    return dict(reach=_prf(Rg, Re), stop=_prf(Sg, Se),
                dwell_mae_s=float(err.mean()) if len(err) else float("nan"),
                dwell_bias=float((de.sum() - dg.sum()) / dg.sum()) if dg.sum() else float("nan"),
                persons=len(persons), per_zone=per_zone)


def _as_shown(gt_ev, raw_ev, merch, n_gt, n_est):
    """Stopped share per zone from the raw tracks, the way the app computes it (visits with a stop / all visits)."""
    eg, ee = gt_ev[2], raw_ev[2]
    rows = []
    for z in sorted(merch):
        a = eg[eg.zone == z].track_id.nunique() / n_gt if n_gt else float("nan")
        b = ee[ee.zone == z].track_id.nunique() / n_est if n_est else float("nan")
        rows.append(dict(zone=z, stopped_share_truth=a, stopped_share_agent=b, error_pp=(b - a) * 100))
    errs = [abs(r["error_pp"]) for r in rows if r["error_pp"] == r["error_pp"]]
    return dict(visits_ratio=n_est / n_gt if n_gt else float("nan"), per_zone=rows,
                stopped_share_mae_pp=float(np.mean(errs)) if errs else float("nan"))


def evaluate_tracks(gt, est, zones=None, hz=SAMPLE_HZ, gate_m=1.0, time_offset_s=0.0):
    """Score `est` against `gt` (both DataFrames of track_id, t, x, y). `time_offset_s` is added to the agent's
    times first, to correct a known clock gap. Returns a dict; see the module docstring for what is in it."""
    if gt.empty or est.empty:
        raise EvalError("Both the truth and the agent output need at least one point.")
    est = est.assign(t=est["t"] + time_offset_s)
    gt, est = to_grid(gt[COLS], hz), to_grid(est[COLS], hz)
    overlap = min(gt.t.max(), est.t.max()) - max(gt.t.min(), est.t.min())
    if overlap <= 0:
        raise EvalError("The truth and the agent output do not overlap in time. Check the clocks and --time-offset.")
    m = match_frames(gt, est, gate_m)
    out = dict(hz=hz, gate_m=gate_m, time_offset_s=time_offset_s, warnings=[], tracking=_tracking(gt, est, m, hz))
    if out["tracking"]["gt_people"] < 20:
        out["warnings"].append(f"Only {out['tracking']['gt_people']} people in the truth: shares and rates are noisy.")
    if out["tracking"]["recall"] < 0.2:
        out["warnings"].append("Very few points matched. Check units (metres), axes, and the time offset before reading anything else.")
    if zones:
        merch = {n for n, z in zones.items() if z[4] == "merchandise"}
        out["zones"] = _zone_agreement(m, zones)
        mapping = _mapping(m, est)
        found = set(mapping.values())
        est_rel = est[est.track_id.isin(mapping)].assign(track_id=lambda d: d.track_id.map(mapping))
        est_rel = est_rel.drop_duplicates(["track_id", "k"])
        gt_ev, rel_ev = an.build_events(gt[COLS], zones), an.build_events(est_rel[COLS], zones)
        out["person_zone"] = _person_zone(gt_ev, rel_ev, merch, found)
        raw_ev = an.build_events(est[COLS], zones)
        out["as_shown"] = _as_shown(gt_ev, raw_ev, merch, out["tracking"]["gt_people"], out["tracking"]["est_tracks"])
    return out


# ---------------------------------------------------------------- report
def format_report(r):
    t = r["tracking"]
    L = ["TRACKING",
         f"  {t['gt_people']} true people, {t['est_tracks']} agent tracks. Recall {t['recall']:.0%}, precision {t['precision']:.0%}.",
         f"  Position error: median {t['median_err_m']:.2f} m, 90th percentile {t['p90_err_m']:.2f} m (matches within {r['gate_m']:g} m).",
         f"  Identity switches {t['id_switches']} ({t['id_switches_per_person_min']:.2f} per person-minute); "
         f"{t['tracks_per_found_person']:.2f} agent tracks per person found; {t['missed_people']} people missed; {t['ghost_tracks']} ghost tracks."]
    if "zones" in r:
        z, p, s = r["zones"], r["person_zone"], r["as_shown"]
        L += ["ZONES", f"  A matched point lands in the same zone as the truth {z['point_agreement']:.0%} of the time."]
        for c in z["top_confusions"]:
            L.append(f"    most common mix-up: truth {c['truth']} read as {c['agent']} ({c['points']} points)")
        L += [f"PER PERSON (after matching, {p['persons']} people found)",
              f"  Reached a zone: recall {p['reach']['recall']:.0%}, precision {p['reach']['precision']:.0%}.",
              f"  Stopped in a zone: recall {p['stop']['recall']:.0%}, precision {p['stop']['precision']:.0%} "
              f"({p['stop']['truth']} true stops, {p['stop']['agent']} found or invented).",
              f"  Dwell per person per zone: mean error {p['dwell_mae_s']:.1f} s, total {p['dwell_bias']:+.0%} vs truth.",
              "AS THE APP SHOWS IT (raw tracks)",
              f"  Visit count is {s['visits_ratio']:.2f}x the true number of people. Stopped share per zone is off by "
              f"{s['stopped_share_mae_pp']:.1f} points on average:"]
        for row in s["per_zone"]:
            L.append(f"    {row['zone']:<18} truth {row['stopped_share_truth']:.0%}  agent {row['stopped_share_agent']:.0%}  ({row['error_pp']:+.1f} pts)")
    for w in r["warnings"]:
        L.append(f"WARNING: {w}")
    return "\n".join(L)
