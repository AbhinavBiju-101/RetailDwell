"""Synthetic test scene: people walking and pausing on a known floor, rendered through a known camera.

Gives video plus exact ground truth, so the agent's geometry, tracking and pipeline can be measured.
This tests our code, NOT detection quality on real footage (people are drawn as plain shapes).
"""
import cv2
import numpy as np
import pandas as pd

from .floor import Floor

FLOOR_W, FLOOR_H = 12.0, 8.0
FRAME = (960, 540)


def camera():
    """A camera on the near wall looking across the floor (trapezoid in the image)."""
    return Floor(
        image_pts=[[120, 500], [840, 500], [700, 120], [260, 120], [480, 500], [480, 120]],
        floor_pts=[[0, 0], [FLOOR_W, 0], [FLOOR_W, FLOOR_H], [0, FLOOR_H], [FLOOR_W / 2, 0], [FLOOR_W / 2, FLOOR_H]],
        frame_size=FRAME, floor_size=(FLOOR_W, FLOOR_H))


def _people(rng, n, seconds, lead_in=3.0):
    stops = [(3, 5.5), (6, 6.5), (9, 5.5), (4, 3), (8.5, 3)]
    people = []
    for pid in range(1, n + 1):
        t = lead_in + rng.uniform(0, max(1.0, seconds - 40))
        pos = np.array([rng.uniform(1.5, 2.5), 0.4])
        way = [(pos.copy(), t)]
        speed = rng.uniform(0.9, 1.3)
        for _ in range(rng.integers(2, 4)):
            dest = np.array(stops[rng.integers(len(stops))]) + rng.normal(0, 0.3, 2)
            t += np.linalg.norm(dest - pos) / speed
            way.append((dest, t))
            t += rng.uniform(6, 14) if rng.random() < 0.7 else rng.uniform(0.5, 2)      # dwell, or pass by
            way.append((dest, t))
            pos = dest
        end = np.array([rng.uniform(1.5, 2.5), 0.4])
        t += np.linalg.norm(end - pos) / speed
        way.append((end, t))
        people.append((pid, way, tuple(int(c) for c in rng.integers(60, 230, 3))))
    return people


def _at(way, t):
    if t < way[0][1] or t > way[-1][1]:
        return None
    for (p0, t0), (p1, t1) in zip(way, way[1:]):
        if t0 <= t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return p0 + f * (p1 - p0)
    return None


def render(path, seconds=120, fps=8, n_people=14, seed=7):
    """Write a video to `path`; return (floor, ground_truth DataFrame[track_id,t,x,y])."""
    rng = np.random.default_rng(seed)
    floor = camera()
    W, H = FRAME
    bg = cv2.GaussianBlur(rng.integers(90, 150, (H, W, 3), dtype=np.uint8), (0, 0), 6)
    for gx in range(0, int(FLOOR_W) + 1, 2):                    # floor grid, so the background has texture
        a, b = floor.to_image([gx, gx], [0, FLOOR_H]).astype(int)
        cv2.line(bg, tuple(a), tuple(b), (170, 170, 170), 1)
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, FRAME)
    people, gt = _people(rng, n_people, seconds), []
    for i in range(int(seconds * fps)):
        t = i / fps
        frame = bg.copy()
        drawn = []
        for pid, way, colour in people:
            p = _at(way, t)
            if p is None:
                continue
            gt.append((pid, round(t, 3), float(p[0]), float(p[1])))
            fx, fy = floor.to_image(p[0], p[1])[0]
            sx = np.hypot(*(floor.to_image(p[0] + 0.5, p[1])[0] - floor.to_image(p[0] - 0.5, p[1])[0]))
            drawn.append((fy, fx, sx, colour))
        for fy, fx, sx, colour in sorted(drawn):                # far people first
            hh, ww = 1.7 * sx * 0.8, 0.5 * sx
            cv2.rectangle(frame, (int(fx - ww / 2), int(fy - hh)), (int(fx + ww / 2), int(fy)), colour, -1)
            cv2.circle(frame, (int(fx), int(fy - hh)), max(2, int(ww * 0.4)), colour, -1)
        noise = rng.normal(0, 3, frame.shape)
        vw.write(np.clip(frame + noise, 0, 255).astype(np.uint8))
    vw.release()
    return floor, pd.DataFrame(gt, columns=["track_id", "t", "x", "y"])


def evaluate(gt, est, fps=8, gate_m=1.0, bin_s=0.5):
    """Compare agent output with ground truth. Returns a dict of metrics.

    Points are matched per video frame (Hungarian, within gate_m). Recall counts ground-truth
    person-bins of bin_s seconds that have at least one matched point, so it is comparable across store rates.
    """
    from scipy.optimize import linear_sum_assignment
    gt = gt.assign(k=(gt.t * fps).round().astype(int))
    est = est.assign(k=(est.t * fps).round().astype(int))
    gt_bins = set(zip(gt.track_id, (gt.t // bin_s).astype(int)))
    errs, hit_bins, matched, id_seq = [], set(), 0, {}
    gk = {k: g for k, g in gt.groupby("k")}
    for k, e in est.groupby("k"):
        g = gk.get(k)
        if g is None:
            continue
        cost = np.hypot(g.x.values[:, None] - e.x.values[None, :], g.y.values[:, None] - e.y.values[None, :])
        r, c = linear_sum_assignment(cost)
        for ri, ci in zip(r, c):
            if cost[ri, ci] <= gate_m:
                pid = int(g.track_id.values[ri])
                errs.append(cost[ri, ci])
                matched += 1
                hit_bins.add((pid, int(g.t.values[ri] // bin_s)))
                id_seq.setdefault(pid, []).append((float(e.t.values[ci]), int(e.track_id.values[ci])))
    switches = 0
    for seq in id_seq.values():
        ids = [i for _t, i in sorted(seq)]
        switches += sum(a != b for a, b in zip(ids, ids[1:]))
    return dict(gt_points=len(gt), est_points=len(est), matched=matched,
                recall=len(hit_bins) / max(1, len(gt_bins)), precision=matched / max(1, len(est)),
                median_err_m=float(np.median(errs)) if errs else float("nan"),
                p90_err_m=float(np.percentile(errs, 90)) if errs else float("nan"),
                id_switches=int(switches), gt_people=int(gt.track_id.nunique()), est_tracks=int(est.track_id.nunique()))


def evaluate_stops(gt, est, fps=8, hz=2.0):
    """Stops (config dwell rule: within 1 m for 5 s) found in ground truth vs in agent output.

    A truth stop counts as found if an agent stop with an anchor within 1 m exists (each used once).
    """
    import analytics as an
    step = int(round(fps / hz))
    g2 = gt[(gt.t * fps).round().astype(int) % step == 0]

    def stops(df):
        out = []
        for _tid, g in df.sort_values("t").groupby("track_id"):
            out += an.stop_episodes(g)
        return out
    sg, se = stops(g2), stops(est)
    used, found = set(), 0
    for ax, ay, _d in sg:
        best = min(((np.hypot(ax - bx, ay - by), j) for j, (bx, by, _e) in enumerate(se) if j not in used), default=None)
        if best and best[0] <= 1.0:
            used.add(best[1])
            found += 1
    return dict(true_stops=len(sg), agent_stops=len(se), found=found,
                true_stop_s=float(sum(d for *_a, d in sg)), agent_stop_s=float(sum(d for *_a, d in se)))
