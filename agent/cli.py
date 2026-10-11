"""python -m agent <command>

  snapshot   save one frame so you can read pixel positions for calibration (delete it afterwards)
  click      click reference points on a frame and type their floor positions in metres (needs a display)
  check      report calibration quality and save an overlay grid
  run        process a video file / camera / RTSP stream into a CSV and/or a store's ingest API
  selftest   render a synthetic store, run the agent on it, and print measured accuracy
"""
import argparse
import json
import logging
import os
import sys
import tempfile
import time

import cv2
import numpy as np

from .floor import CalibrationError, Floor


def _grab_frame(source, at_s=0.0):
    cap = cv2.VideoCapture(int(source) if str(source).isdigit() else source)
    if at_s:
        cap.set(cv2.CAP_PROP_POS_MSEC, at_s * 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"Could not read a frame from {source}.")
    return frame


def cmd_snapshot(a):
    frame = _grab_frame(a.source, a.at)
    cv2.imwrite(a.out, frame)
    print(f"Saved {a.out} ({frame.shape[1]}x{frame.shape[0]}). Read pixel positions of floor reference points from it, "
          "then DELETE this image: it may show people.")


def cmd_click(a):
    frame = _grab_frame(a.source, a.at) if not a.image else cv2.imread(a.image)
    h, w = frame.shape[:2]
    pts = []

    def on_mouse(ev, x, y, *_):
        if ev == cv2.EVENT_LBUTTONDOWN:
            pts.append((x, y))
            cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)
            cv2.putText(frame, str(len(pts)), (x + 8, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.namedWindow("calibrate")
    cv2.setMouseCallback("calibrate", on_mouse)
    print("Click 4 to 8 floor points you can measure (tile corners, tape marks). Press Enter when done.")
    while True:
        cv2.imshow("calibrate", frame)
        if cv2.waitKey(30) in (13, 10):
            break
    cv2.destroyAllWindows()
    floor_pts = []
    for i, p in enumerate(pts, 1):
        xs, ys = input(f"Point {i} at pixel {p}: floor x,y in metres (from bottom-left of the floor plan): ").split(",")
        floor_pts.append([float(xs), float(ys)])
    out = dict(image_pts=[list(p) for p in pts], floor_pts=floor_pts, frame_size=[w, h],
               floor_size=[a.width, a.height] if a.width and a.height else None, exclude=[])
    json.dump(out, open(a.out, "w"), indent=2)
    print(f"Saved {a.out}. Check it with: python -m agent check --calib {a.out}")


def cmd_check(a):
    try:
        f = Floor.load(a.calib)
    except CalibrationError as e:
        raise SystemExit(str(e))
    print(f"{len(f.image_pts)} points. Reprojection error: RMS {f.rms_m:.2f} m, worst {f.max_m:.2f} m "
          f"({f.quality()}).")
    for i, e in enumerate(f.errors_m, 1):
        flag = "  <- re-measure" if e > max(0.35, 2 * f.rms_m) else ""
        print(f"  point {i}: {e:.2f} m{flag}")
    if a.image or a.source:
        frame = cv2.imread(a.image) if a.image else _grab_frame(a.source)
        w, h = (f.floor_size or (max(f.floor_pts[:, 0]), max(f.floor_pts[:, 1])))
        for gx in np.arange(0, w + 1e-6, 1.0):
            a_, b_ = f.to_image([gx, gx], [0, h]).astype(int)
            cv2.line(frame, tuple(a_), tuple(b_), (0, 255, 255), 1)
        for gy in np.arange(0, h + 1e-6, 1.0):
            a_, b_ = f.to_image([0, w], [gy, gy]).astype(int)
            cv2.line(frame, tuple(a_), tuple(b_), (0, 255, 255), 1)
        cv2.imwrite(a.overlay, frame)
        print(f"Wrote {a.overlay}: a 1 m grid on the floor. If the lines do not follow the floor, recalibrate. "
              "Delete the image afterwards.")


def _build_detector(a):
    if a.detector == "motion":
        from .detect import MotionDetector
        return MotionDetector()
    from .detect import YoloDetector
    return YoloDetector(a.model, conf=a.conf, imgsz=a.imgsz, device=a.device)


def cmd_run(a):
    from .pipeline import Pipeline, run_source
    from .sinks import ApiSink, CsvSink
    floor = Floor.load(a.calib)
    log = logging.getLogger("agent")
    log.info("calibration: RMS %.2f m (%s)", floor.rms_m, floor.quality())
    if a.api:
        sink = ApiSink(a.api, os.environ.get("DWELL_API_KEY", a.key), a.layout,
                       a.recording or time.strftime("Live %Y-%m-%d"), spool_dir=a.spool_dir)
    elif a.csv:
        sink = CsvSink(a.csv)
    else:
        raise SystemExit("Give --csv FILE or --api URL.")
    pipe = Pipeline(floor, _build_detector(a), sink, min_track_s=a.min_track_s)
    run_source(a.source, pipe, track_fps=a.fps, max_seconds=a.max_seconds, preview=a.preview)


def cmd_selftest(a):
    from .pipeline import Pipeline, run_source
    from .sinks import CsvSink
    from .detect import MotionDetector
    from . import synth
    import pandas as pd
    with tempfile.TemporaryDirectory() as d:
        video, out = os.path.join(d, "scene.mp4"), os.path.join(d, "tracks.csv")
        print(f"Rendering a {a.seconds}s synthetic store with {a.people} people ...")
        floor, gt = synth.render(video, seconds=a.seconds, n_people=a.people, seed=a.seed)
        pipe = Pipeline(floor, MotionDetector(), CsvSink(out), id_base=0)
        t0 = time.time()
        run_source(video, pipe, track_fps=8.0)
        est = pd.read_csv(out)
        m = synth.evaluate(gt, est)
        print(f"Processed in {time.time() - t0:.1f}s. Ground truth: {m['gt_people']} people. "
              f"Agent: {m['est_tracks']} tracks, {m['est_points']:,} points.")
        print(f"Position error: median {m['median_err_m']:.2f} m, 90th percentile {m['p90_err_m']:.2f} m. "
              f"Recall {m['recall']:.0%}, precision {m['precision']:.0%}, identity switches {m['id_switches']}.")
        st = synth.evaluate_stops(gt, est)
        print(f"Stops (within 1 m for 5 s): truth {st['true_stops']}, agent {st['agent_stops']}, "
              f"{st['found']} of the true stops found; stopped time truth {st['true_stop_s']:.0f}s, agent {st['agent_stop_s']:.0f}s.")
        from . import evaluate as ev
        print(ev.format_report(ev.evaluate_tracks(gt, est, zones=synth.ZONES)))
        print("This measures our geometry and tracking on drawn people. It says nothing about real footage.")


def cmd_evaluate(a):
    from . import evaluate as ev
    try:
        if a.truth_format == "mmptrack":
            gt = ev.load_mmptrack_truth(a.truth, a.voxel_m, (a.origin_x, a.origin_y), a.truth_fps)
        else:
            gt = ev.load_tracks(a.truth)
        est = ev.load_tracks(a.est)
        zones = ev.load_zones(a.zones) if a.zones else None
        r = ev.evaluate_tracks(gt, est, zones=zones, hz=a.hz, gate_m=a.gate_m, time_offset_s=a.time_offset)
    except ev.EvalError as e:
        raise SystemExit(f"Evaluation problem: {e}")
    print(ev.format_report(r))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(r, f, indent=2, default=float)
        print(f"Wrote {a.json}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m agent", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot"); s.add_argument("--source", required=True); s.add_argument("--out", default="frame.png")
    s.add_argument("--at", type=float, default=0.0, help="seconds into a file"); s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("click"); s.add_argument("--source"); s.add_argument("--image"); s.add_argument("--at", type=float, default=0.0)
    s.add_argument("--out", default="calib.json"); s.add_argument("--width", type=float, help="floor width in metres")
    s.add_argument("--height", type=float, help="floor height in metres"); s.set_defaults(fn=cmd_click)

    s = sub.add_parser("check"); s.add_argument("--calib", required=True); s.add_argument("--source")
    s.add_argument("--image"); s.add_argument("--overlay", default="overlay.png"); s.set_defaults(fn=cmd_check)

    s = sub.add_parser("run")
    s.add_argument("--source", required=True, help="video file, camera index, or rtsp:// URL")
    s.add_argument("--calib", required=True); s.add_argument("--layout", default="A")
    s.add_argument("--csv", help="write points to this CSV (.csv or .csv.gz)")
    s.add_argument("--api", help="base URL of the app, e.g. https://yourapp.onrender.com")
    s.add_argument("--key", default="", help="store API key (prefer the DWELL_API_KEY environment variable)")
    s.add_argument("--recording", help='recording name in the app (default "Live <date>")')
    s.add_argument("--spool-dir", default="spool", help="where unsent points wait if the server is unreachable")
    s.add_argument("--detector", choices=["yolo", "motion"], default="yolo")
    s.add_argument("--model", default="yolov8n.pt"); s.add_argument("--conf", type=float, default=0.25)
    s.add_argument("--imgsz", type=int, default=640); s.add_argument("--device", default=None)
    s.add_argument("--fps", type=float, default=8.0, help="detection rate"); s.add_argument("--min-track-s", type=float, default=2.0)
    s.add_argument("--max-seconds", type=float); s.add_argument("--preview", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("evaluate", help="score agent output against ground-truth floor tracks")
    s.add_argument("--truth", required=True, help="ground truth: CSV (track_id,t,x,y in metres) or an MMPTRACK folder")
    s.add_argument("--truth-format", choices=["csv", "mmptrack"], default="csv")
    s.add_argument("--est", required=True, help="agent output CSV (track_id,t,x,y)")
    s.add_argument("--zones", help='JSON {"name": [x0, y0, x1, y1, "merchandise"]} for the zone-level scores')
    s.add_argument("--hz", type=float, default=2.0, help="resampling rate; the app assumes 2")
    s.add_argument("--gate-m", type=float, default=1.0, help="largest position error that still counts as a match")
    s.add_argument("--time-offset", type=float, default=0.0, help="seconds added to the agent's times to line the clocks up")
    s.add_argument("--voxel-m", type=float, help="mmptrack: metres per grid cell, from the dataset's calibration")
    s.add_argument("--origin-x", type=float, default=0.0); s.add_argument("--origin-y", type=float, default=0.0)
    s.add_argument("--truth-fps", type=float, default=15.0, help="mmptrack frame rate")
    s.add_argument("--json", help="also write the scores to this file"); s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("selftest"); s.add_argument("--seconds", type=int, default=120)
    s.add_argument("--people", type=int, default=14); s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_selftest)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    try:
        a.fn(a)
    except CalibrationError as e:
        raise SystemExit(f"Calibration problem: {e}")


if __name__ == "__main__":
    main()
