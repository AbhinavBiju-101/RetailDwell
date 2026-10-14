"""Frames in, anonymous floor points out.

Per frame: detect people -> take the feet point -> floor metres -> drop off-floor and excluded-room points ->
track -> smooth -> hold each track back until it has lasted `min_track_s` (kills flicker and ghosts) ->
thin to the stored rate (config.SAMPLE_HZ) -> sink.
"""
import logging
import secrets
import time

import cv2
import numpy as np

import config
from .tracker import FloorTracker

log = logging.getLogger("agent")


class Pipeline:
    def __init__(self, floor, detector, sink, detect_width=640, store_hz=config.SAMPLE_HZ, min_track_s=2.0,
                 smooth=0.5, max_speed_mps=2.5, id_base=None, stitch=True, stitch_max_gap_s=config.STITCH_MAX_GAP_S,
                 stitch_min_gap_s=config.STITCH_MIN_GAP_S, stitch_base_m=config.STITCH_BASE_M,
                 stitch_speed_mps=config.STITCH_SPEED_MPS, stitch_edge_m=config.STITCH_EDGE_M):
        self.floor, self.det, self.sink = floor, detector, sink
        self.detect_width, self.store_dt = detect_width, 1.0 / store_hz
        self.min_track_s, self.smooth, self.max_speed = min_track_s, smooth, max_speed_mps
        # Ids are random per run, so two runs (or two days) can never be joined into one person.
        self.id_base = secrets.randbits(40) if id_base is None else id_base
        self._n = 0
        self.tracker = None if detector.gives_ids else FloorTracker(new_id=self._next_id)
        self.state = {}
        self.frames = self.dets = self.dropped_off_floor = 0
        # Track stitching: a new track that starts near where an established track was last seen, soon enough
        # and close enough to be the same walker, continues that track instead of counting as a new person.
        self.stitch, self.stitch_max_gap, self.stitch_min_gap = stitch, stitch_max_gap_s, stitch_min_gap_s
        self.stitch_base, self.stitch_speed, self.stitch_edge = stitch_base_m, stitch_speed_mps, stitch_edge_m
        self.alias, self.live, self.stitched, self._extra = {}, {}, 0, 0

    def _next_id(self):
        self._n += 1
        return self.id_base + self._n

    def feet_to_floor(self, boxes, src_w, src_h, scale):
        """boxes in detection-frame pixels -> [(ext_id, fx, fy)] in floor metres (off-floor points removed)."""
        if not boxes:
            return []
        b = np.asarray([(x1, y1, x2, y2) for x1, y1, x2, y2, _c, _i in boxes], dtype=np.float64)
        px, py = (b[:, 0] + b[:, 2]) / 2 / scale, b[:, 3] / scale              # bottom-centre = feet
        fs = self.floor.frame_size
        if fs and (fs[0] != src_w or fs[1] != src_h):                           # calibrated at another resolution
            px, py = px * fs[0] / src_w, py * fs[1] / src_h
        xy = self.floor.to_floor(px, py)
        out = []
        for (x, y), box in zip(xy, boxes):
            if self.floor.keep(float(x), float(y)):
                out.append((box[5], float(x), float(y)))
            else:
                self.dropped_off_floor += 1
        return out

    def process(self, frame, t):
        h, w = frame.shape[:2]
        scale = self.detect_width / w if w > self.detect_width else 1.0
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
        pts = self.feet_to_floor(self.det.detect(small), w, h, scale)
        self.frames += 1
        self.dets += len(pts)
        if self.tracker is not None:
            obs = self.tracker.update(t, [(x, y) for _i, x, y in pts])
        else:
            obs = [(self.id_base + int(i), x, y) for i, x, y in pts if i is not None]
        for tid, x, y in obs:
            self._observe(self._resolve(tid, t, x, y), t, x, y)
        self._purge(t)

    def _resolve(self, raw, t, x, y):
        """The id a detector track is stored under. Usually its own; a stitched track carries on an earlier id."""
        c = self.alias.get(raw, raw if raw in self.state else None)
        if c is not None and self.live.get(c, raw) == raw:
            return c
        if c is not None and raw not in self.alias:          # an id that was taken over has come back: a new track
            self._extra += 1
            fresh = self.id_base + 10 ** 6 + self._extra
            self.alias[raw] = fresh
            self.live[fresh] = raw
            return fresh
        self.alias.pop(raw, None)
        c = self._find_stitch(t, x, y) if self.stitch else None
        if c is None:
            self.live[raw] = raw
            return raw
        self.alias[raw] = c
        self.live[c] = raw
        self.stitched += 1
        return c

    def _find_stitch(self, t, x, y):
        """The established, currently unseen track this new track most plausibly continues, or None."""
        best, best_d = None, None
        fs = self.floor.floor_size
        for c, s in self.state.items():
            gap = t - s["seen"]
            if not s["released"] or gap < self.stitch_min_gap or gap > self.stitch_max_gap:
                continue
            if fs and (min(s["sx"], s["sy"], fs[0] - s["sx"], fs[1] - s["sy"]) < self.stitch_edge):
                continue                                      # last seen at the edge of the floor: it left
            d = np.hypot(x - s["sx"], y - s["sy"])
            if d <= self.stitch_base + self.stitch_speed * gap and (best_d is None or d < best_d):
                best, best_d = c, d
        return best

    def _observe(self, tid, t, x, y):
        s = self.state.get(tid)
        if s is None:
            s = self.state[tid] = dict(first=t, seen=t, sx=x, sy=y, last_emit=-1e9, released=False, buf=[])
        else:
            dt = max(t - s["seen"], 1e-3)
            if np.hypot(x - s["sx"], y - s["sy"]) > self.max_speed * dt + 1.0:
                s["sx"], s["sy"] = x, y                     # a jump, not a walk: restart the smoother
            else:
                a = self.smooth
                s["sx"], s["sy"] = a * x + (1 - a) * s["sx"], a * y + (1 - a) * s["sy"]
            s["seen"] = t
        if t - s["last_emit"] < self.store_dt - 1e-6:
            return
        s["last_emit"] = t
        row = (tid, t, s["sx"], s["sy"])
        if s["released"]:
            self.sink.add(*row)
            return
        s["buf"].append(row)
        if t - s["first"] >= self.min_track_s:
            for r in s["buf"]:
                self.sink.add(*r)
            s["buf"], s["released"] = [], True

    def _purge(self, t, idle_s=30.0):
        for tid in [k for k, s in self.state.items() if t - s["seen"] > idle_s]:
            del self.state[tid]

    def close(self):
        self.sink.close()
        log.info("%d frames, %d detections, %d off-floor dropped, %d tracks stitched, %d points kept",
                 self.frames, self.dets, self.dropped_off_floor, self.stitched, self.sink.count)


def open_source(src):
    return cv2.VideoCapture(int(src) if str(src).isdigit() else src)


def run_source(source, pipe, track_fps=8.0, max_seconds=None, preview=False, reconnect=5, clock=time.monotonic):
    """Drive the pipeline from a file, camera index or rtsp:// URL. Live sources reconnect with backoff."""
    live = str(source).isdigit() or str(source).lower().startswith(("rtsp", "http", "rtmp"))
    cap = open_source(source)
    if not cap.isOpened() and not live:
        raise SystemExit(f"Could not open {source}.")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    due, i, t0, fails = 0.0, 0, clock(), 0
    try:
        while True:
            if not cap.grab():
                if not live:
                    break
                fails += 1
                if fails > reconnect:
                    log.error("stream lost after %d reconnect attempts", reconnect)
                    break
                log.warning("stream dropped, reconnecting (%d/%d)", fails, reconnect)
                cap.release()
                time.sleep(min(2 ** fails, 30))
                cap = open_source(source)
                continue
            fails = 0
            t = (clock() - t0) if live else i / src_fps
            i += 1
            if max_seconds and t > max_seconds:
                break
            if t + 1e-6 < due:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                continue
            due = t + 1.0 / track_fps - 1e-6
            pipe.process(frame, t)          # the frame is dropped here; nothing is written to disk
            if preview:
                cv2.imshow("RetailDwell agent (q to quit)", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        cap.release()
        pipe.close()
