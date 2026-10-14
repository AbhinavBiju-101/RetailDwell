"""Where anonymous points go: a CSV file, or a store's ingest API (batched, retried, spooled to disk if offline)."""
import glob
import gzip
import json
import logging
import os
import time

import pandas as pd

log = logging.getLogger("agent.sink")
COLS = ["track_id", "t", "x", "y"]
INGEST_PATH = "/api/ingest/v1/tracks"
MAX_BATCH = 5000        # the server's hard limit per request


class SinkError(RuntimeError):
    """Fatal: retrying will not help (bad key, no such layout). The message says how to fix it."""


class CsvSink:
    def __init__(self, path):
        self.path, self.rows, self.count = path, [], 0

    def add(self, tid, t, x, y):
        self.rows.append((int(tid), round(float(t), 2), round(float(x), 2), round(float(y), 2)))
        self.count += 1

    def flush(self, force=False):
        pass

    def close(self):
        df = pd.DataFrame(self.rows, columns=COLS)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        df.to_csv(self.path, index=False)          # pandas gzips on a .gz name
        log.info("wrote %s anonymous points to %s", f"{len(df):,}", self.path)


class ApiSink:
    def __init__(self, base_url, key, layout, recording, batch=2000, flush_s=5.0, spool_dir=None,
                 session=None, retries=4, backoff_s=1.0, timeout_s=20.0, clock=time.monotonic, sleep=time.sleep,
                 started_at=None):
        if not key:
            raise SinkError("No API key. Set DWELL_API_KEY (Settings > API keys in the app).")
        self.url = base_url if base_url.rstrip("/").endswith(INGEST_PATH) else base_url.rstrip("/") + INGEST_PATH
        self.key, self.layout, self.recording = key, layout, recording
        self.started_at = started_at        # ISO UTC time the run began: the server keeps the first one it hears
        self.batch, self.flush_s = min(int(batch), MAX_BATCH), flush_s
        self.spool_dir = spool_dir
        self.retries, self.backoff, self.timeout = retries, backoff_s, timeout_s
        self.clock, self.sleep = clock, sleep
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.buf, self.count, self.sent, self.spooled = [], 0, 0, 0
        self.last_flush = clock()
        self.resend_spool()

    def add(self, tid, t, x, y):
        self.buf.append({"track_id": int(tid), "t": round(float(t), 2), "x": round(float(x), 2), "y": round(float(y), 2)})
        self.count += 1
        if len(self.buf) >= self.batch or self.clock() - self.last_flush >= self.flush_s:
            self.flush()

    def flush(self, force=False):
        self.last_flush = self.clock()
        while self.buf:
            chunk, self.buf = self.buf[:self.batch], self.buf[self.batch:]
            if not self._send(chunk):
                self._spool(chunk)

    def close(self):
        self.flush(force=True)
        log.info("sent %s points, %s spooled for retry", f"{self.sent:,}", f"{self.spooled:,}")

    # -- internals
    def _send(self, chunk):
        body = {"layout": self.layout, "recording": self.recording, "points": chunk}
        if self.started_at:
            body["started_at"] = self.started_at
        for attempt in range(self.retries + 1):
            try:
                r = self.session.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"},
                                      timeout=self.timeout)
            except Exception as e:                       # network down, DNS, timeout
                log.warning("send failed (%s), attempt %d", e.__class__.__name__, attempt + 1)
            else:
                if r.status_code in (200, 202):
                    self.sent += len(chunk)
                    return True
                msg = _err(r)
                if r.status_code == 401:
                    raise SinkError(f"The server rejected the API key: {msg}")
                if r.status_code in (403, 404):
                    raise SinkError(f"The server refused the data ({r.status_code}): {msg}")
                if r.status_code == 400:
                    raise SinkError(f"The server rejected the points: {msg}")
                log.warning("server error %s: %s, attempt %d", r.status_code, msg, attempt + 1)
            if attempt < self.retries:
                self.sleep(self.backoff * 2 ** attempt)
        return False

    def _spool(self, chunk):
        self.spooled += len(chunk)
        if not self.spool_dir:
            log.error("dropped %d points: server unreachable and no --spool-dir set", len(chunk))
            return
        os.makedirs(self.spool_dir, exist_ok=True)
        path = os.path.join(self.spool_dir, f"spool_{int(time.time() * 1000)}_{len(os.listdir(self.spool_dir))}.json.gz")
        with gzip.open(path, "wt") as fh:
            json.dump({"layout": self.layout, "recording": self.recording, "points": chunk, "started_at": self.started_at}, fh)
        log.warning("server unreachable: kept %d points in %s", len(chunk), path)

    def resend_spool(self):
        if not self.spool_dir:
            return
        for path in sorted(glob.glob(os.path.join(self.spool_dir, "spool_*.json.gz"))):
            try:
                with gzip.open(path, "rt") as fh:
                    saved = json.load(fh)
                if saved["layout"] != self.layout:
                    continue
                keep = (self.recording, self.layout)
                self.recording = saved["recording"]
                ok = self._send(saved["points"])
                self.recording = keep[0]
            except SinkError:
                raise
            except Exception:
                log.exception("could not read %s", path)
                continue
            if ok:
                os.remove(path)
                self.spooled = max(0, self.spooled - len(saved["points"]))
            else:
                break


def _err(r):
    try:
        return r.json().get("error", "")
    except Exception:
        return getattr(r, "text", "")[:200]
