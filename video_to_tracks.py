"""Video -> anonymous floor tracks. UNTESTED SCAFFOLD: written against the
Ultralytics API but not run (no footage or model available yet).

Keeps only (layout, track_id, t, x, y). Frames are never written to disk.

Usage:
  python video_to_tracks.py --source clip.mp4 --calib calib.json --layout A --out data/tracks_A.csv

calib.json:
  {"image_pts": [[px,py], ...4+], "floor_pts": [[xm,ym], ...4+]}
  floor_pts are tape-measured metres on the same axes as config.py.
"""
import argparse
import json

import cv2
import numpy as np
import pandas as pd

TRACK_FPS = 8        # tracker rate (ByteTrack assumes small motion between frames)
STORE_FPS = 2.0      # rate written out, matches config.SAMPLE_HZ


def load_homography(path):
    c = json.load(open(path))
    H, _ = cv2.findHomography(np.float32(c["image_pts"]), np.float32(c["floor_pts"]), cv2.RANSAC)
    return H


def to_floor(H, px, py):
    p = H @ np.array([px, py, 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="video file or rtsp:// URL")
    ap.add_argument("--calib", required=True)
    ap.add_argument("--layout", default="A")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="yolov8n.pt")
    args = ap.parse_args()

    from ultralytics import YOLO  # imported here so the rest of the project needs no GPU stack
    model, H = YOLO(args.model), load_homography(args.calib)
    cap = cv2.VideoCapture(args.source)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(src_fps / TRACK_FPS))
    store_every = max(1, round(TRACK_FPS / STORE_FPS))

    rows, i, n_out = [], 0, 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            frame = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))
            scale = frame.shape[1] / cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            res = model.track(frame, persist=True, classes=[0], tracker="bytetrack.yaml",
                              verbose=False)[0]
            n_out += 1
            if res.boxes.id is not None and n_out % store_every == 0:
                t = i / src_fps
                for (x1, y1, x2, y2), tid in zip(res.boxes.xyxy.cpu().numpy(),
                                                 res.boxes.id.int().cpu().numpy()):
                    # bottom-centre of the box = feet; undo the resize before the homography
                    fx, fy = to_floor(H, (x1 + x2) / 2 / scale, y2 / scale)
                    rows.append((args.layout, int(tid), round(t, 2), round(fx, 2), round(fy, 2)))
        i += 1
    cap.release()
    pd.DataFrame(rows, columns=["layout", "track_id", "t", "x", "y"]).to_csv(args.out, index=False)
    print(f"wrote {len(rows):,} anonymous points to {args.out}")


if __name__ == "__main__":
    main()
