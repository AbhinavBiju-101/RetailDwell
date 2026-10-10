"""Short-lived tracking in floor space (metres), for detectors that give no track ids.

Constant-velocity prediction, Hungarian matching, a distance gate that scales with elapsed time, and
a short coast through missed frames. IDs are never reused and never leave the current run.
"""
import numpy as np
from scipy.optimize import linear_sum_assignment


class FloorTracker:
    def __init__(self, max_speed_mps=1.8, base_gate_m=0.5, max_coast_s=1.5, max_gate_m=2.0, vel_alpha=0.5, new_id=None):
        self.max_speed, self.base_gate, self.max_gate = max_speed_mps, base_gate_m, max_gate_m
        self.max_coast, self.vel_alpha = max_coast_s, vel_alpha
        self.tracks = {}            # id -> dict(x, y, vx, vy, t, seen_t)
        self._new_id = new_id

    def update(self, t, dets):
        """dets: list of (x, y). Returns list of (track_id, x, y) for every detection that was matched or started."""
        ids = list(self.tracks)
        out = []
        matched_d, matched_t = set(), set()
        if ids and dets:
            D = np.asarray(dets, dtype=np.float64)
            pred = np.array([[self.tracks[i]["x"] + self.tracks[i]["vx"] * (t - self.tracks[i]["t"]),
                              self.tracks[i]["y"] + self.tracks[i]["vy"] * (t - self.tracks[i]["t"])] for i in ids])
            dt = np.array([max(t - self.tracks[i]["t"], 0.0) for i in ids])
            gate = np.minimum(self.base_gate + self.max_speed * dt, self.max_gate)
            cost = np.hypot(pred[:, None, 0] - D[None, :, 0], pred[:, None, 1] - D[None, :, 1])
            big = 1e6
            cost = np.where(cost > gate[:, None], big, cost)
            r, c = linear_sum_assignment(cost)
            for ti, di in zip(r, c):
                if cost[ti, di] >= big:
                    continue
                tr = self.tracks[ids[ti]]
                span = max(t - tr["t"], 1e-3)
                nvx, nvy = (D[di, 0] - tr["x"]) / span, (D[di, 1] - tr["y"]) / span
                a = self.vel_alpha
                tr["vx"], tr["vy"] = a * nvx + (1 - a) * tr["vx"], a * nvy + (1 - a) * tr["vy"]
                tr["x"], tr["y"], tr["t"], tr["seen_t"] = float(D[di, 0]), float(D[di, 1]), t, t
                matched_t.add(ti), matched_d.add(di)
                out.append((ids[ti], float(D[di, 0]), float(D[di, 1])))
        for di, d in enumerate(dets):
            if di in matched_d:
                continue
            tid = self._new_id()
            self.tracks[tid] = dict(x=float(d[0]), y=float(d[1]), vx=0.0, vy=0.0, t=t, seen_t=t)
            out.append((tid, float(d[0]), float(d[1])))
        for tid in [i for i, tr in self.tracks.items() if t - tr["seen_t"] > self.max_coast]:
            del self.tracks[tid]
        return out
