"""Person detectors. Each returns boxes in the pixel space of the frame it was given:
list of (x1, y1, x2, y2, conf, track_id or None).

  YoloDetector    Ultralytics YOLO (person class) with ByteTrack ids. The real backend. Needs requirements-video.txt.
  MotionDetector  OpenCV background subtraction. No model, no GPU. For tests and dry runs only: it cannot see people
                  who stand still for long, merges people who walk together, and reacts to light changes.
"""
import cv2
import numpy as np


class YoloDetector:
    name = "yolo"
    gives_ids = True

    def __init__(self, model="yolov8n.pt", conf=0.25, imgsz=640, tracker="bytetrack.yaml", device=None, _model=None):
        if _model is None:
            try:
                from ultralytics import YOLO
            except ImportError:
                raise SystemExit("The YOLO backend needs ultralytics: pip install -r requirements-video.txt")
            _model = YOLO(model)
        self.model, self.conf, self.imgsz, self.tracker, self.device = _model, conf, imgsz, tracker, device

    def detect(self, frame):
        res = self.model.track(frame, persist=True, classes=[0], conf=self.conf, imgsz=self.imgsz,
                               tracker=self.tracker, device=self.device, verbose=False)[0]
        b = res.boxes
        if b is None:
            return []
        xyxy, conf = _np(b.xyxy).reshape(-1, 4), _np(b.conf).reshape(-1)
        if len(xyxy) == 0:
            return []
        ids = _np(b.id).astype(int) if getattr(b, "id", None) is not None else [None] * len(xyxy)
        return [(float(x1), float(y1), float(x2), float(y2), float(c), None if i is None else int(i))
                for (x1, y1, x2, y2), c, i in zip(xyxy, conf, ids)]


def _np(a):
    return a.cpu().numpy() if hasattr(a, "cpu") else np.asarray(a)


class MotionDetector:
    name = "motion"
    gives_ids = False

    def __init__(self, min_area_frac=0.0006, history=300, var_threshold=24, learn_rate=0.0008, shadows=True):
        self.mog = cv2.createBackgroundSubtractorMOG2(history=history, varThreshold=var_threshold,
                                                      detectShadows=shadows)
        self.min_area_frac, self.learn = min_area_frac, learn_rate
        self.k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))

    def detect(self, frame):
        g = cv2.GaussianBlur(frame, (5, 5), 0)
        m = self.mog.apply(g, learningRate=self.learn)
        m = cv2.threshold(m, 200, 255, cv2.THRESH_BINARY)[1]       # 127 = shadow, dropped
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, self.k_open)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, self.k_close)
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        area_min = self.min_area_frac * frame.shape[0] * frame.shape[1]
        out = []
        for i in range(1, n):
            x, y, w, h, a = stats[i]
            if a >= area_min:
                out.append((float(x), float(y), float(x + w), float(y + h), min(1.0, a / (area_min * 8)), None))
        return out
