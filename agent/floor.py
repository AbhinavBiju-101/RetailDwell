"""Camera calibration: image pixels -> floor metres (planar homography) and floor-space filters."""
import json

import cv2
import numpy as np


class CalibrationError(ValueError):
    """A calibration problem with a message that says how to fix it."""


class Floor:
    """Maps image points to floor metres, from >= 4 tape-measured reference points.

    calib.json:
      image_pts   [[px, py], ...]   pixel positions in a frame of size frame_size
      floor_pts   [[xm, ym], ...]   the same points in metres, origin bottom-left of the floor plan
      frame_size  [W, H]            size of the frame the pixels were read from (default: the source's size)
      floor_size  [w, h]            floor width and height in metres; points outside (plus margin) are dropped
      exclude     [[x0,y0,x1,y1]]   floor rectangles never recorded (staff rooms, back office)
    """

    def __init__(self, image_pts, floor_pts, frame_size=None, floor_size=None, exclude=None, margin_m=0.75):
        ip = np.asarray(image_pts, dtype=np.float64).reshape(-1, 2)
        fp = np.asarray(floor_pts, dtype=np.float64).reshape(-1, 2)
        if len(ip) != len(fp):
            raise CalibrationError("image_pts and floor_pts must have the same number of points.")
        if len(ip) < 4:
            raise CalibrationError("Use at least 4 reference points (6 to 8 is better, spread over the floor).")
        if _collinear(fp) or _collinear(ip):
            raise CalibrationError("The reference points lie on one line. Spread them across the floor.")
        self.image_pts, self.floor_pts = ip, fp
        self.frame_size = tuple(frame_size) if frame_size else None
        self.floor_size = tuple(float(v) for v in floor_size) if floor_size else None
        self.exclude = [tuple(map(float, r)) for r in (exclude or [])]
        self.margin = margin_m
        method = cv2.RANSAC if len(ip) > 4 else 0
        H, _ = cv2.findHomography(ip, fp, method, 0.25)
        if H is None:
            raise CalibrationError("Could not fit a homography. Check that the points are matched correctly.")
        self.H = H
        proj = self.to_floor(ip[:, 0], ip[:, 1])
        err = np.hypot(proj[:, 0] - fp[:, 0], proj[:, 1] - fp[:, 1])
        self.errors_m = err
        self.rms_m = float(np.sqrt(np.mean(err ** 2)))
        self.max_m = float(err.max())

    @classmethod
    def load(cls, path):
        try:
            c = json.load(open(path))
            return cls(c["image_pts"], c["floor_pts"], c.get("frame_size"), c.get("floor_size"), c.get("exclude"))
        except (OSError, KeyError, json.JSONDecodeError) as e:
            raise CalibrationError(f"Could not read calibration file {path}: {e!r}. "
                                   "It needs image_pts and floor_pts.")

    def to_floor(self, px, py):
        """Vectorised: arrays of pixel x, y -> (n, 2) array of floor metres."""
        px, py = np.atleast_1d(px).astype(np.float64), np.atleast_1d(py).astype(np.float64)
        p = np.stack([px, py, np.ones_like(px)])
        q = self.H @ p
        return np.stack([q[0] / q[2], q[1] / q[2]], axis=1)

    def to_image(self, x, y):
        """Floor metres -> pixels (inverse homography). Used for overlays and the synthetic tests."""
        x, y = np.atleast_1d(x).astype(np.float64), np.atleast_1d(y).astype(np.float64)
        q = np.linalg.inv(self.H) @ np.stack([x, y, np.ones_like(x)])
        return np.stack([q[0] / q[2], q[1] / q[2]], axis=1)

    def keep(self, x, y):
        """False for points that are off the floor (bad projection) or inside an excluded room."""
        if self.floor_size:
            w, h = self.floor_size
            if not (-self.margin <= x <= w + self.margin and -self.margin <= y <= h + self.margin):
                return False
        for x0, y0, x1, y1 in self.exclude:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return False
        return True

    def quality(self):
        if self.rms_m <= 0.15:
            return "good"
        if self.rms_m <= 0.35:
            return "usable (re-measure the points with the largest error)"
        return "poor (re-measure; positions will be off by about this much)"


def _collinear(pts, tol=1e-3):
    c = pts - pts.mean(axis=0)
    s = np.linalg.svd(c, compute_uv=False)
    return s[1] < tol * max(s[0], 1e-9)
