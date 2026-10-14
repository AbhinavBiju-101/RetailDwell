"""Several recordings of one layout analysed together.

Each recording is shifted in time so no two days overlap (people from different days are never "together", and
checkout windows from different days never merge), and its track ids are made unique. Everything downstream is the
ordinary analysis of one big recording, with one change: when every recording has a start time, time-of-day windows are
keyed by the clock hour, so "14:00 to 14:30" gathers shoppers from every day. The baseline for a zone is then its
average across the hours of all pooled days, which is what lets a quiet Tuesday and a busy Saturday share one picture
of the day, and what makes a thin hour reliable once enough days are pooled.

Limits worth knowing: days are pooled as they are (weekdays and weekends together), the newest MAX_POOL are used, and the
test that flags an hour treats shoppers as independent, so a few unusual days can move a pooled hour more than the test
suggests. Sales are pooled too, with one clock correction for all days.
"""
from datetime import timedelta

import numpy as np
import pandas as pd

from clock import Clock

MAX_POOL = 14
ID_STRIDE = 2 ** 32
GAP_S = 3600.0


def pool(recordings, window_s, tz=None):
    """recordings: list (oldest first) of dict(label, tracks, lines, started_at).
    Returns dict(tracks, lines, slot_fn, minute_of_day_fn, n, labels, undated)."""
    parts, line_parts, shifts, starts, labels = [], [], [], [], []
    cum, undated = 0.0, 0
    for i, rec in enumerate(recordings):
        t = rec["tracks"][["track_id", "t", "x", "y"]].copy()
        dur = float(t["t"].max()) if len(t) else 0.0
        codes, _ = pd.factorize(t["track_id"])                         # dense, so ids can never collide whatever bits they carry
        t["track_id"] = i * ID_STRIDE + codes.astype("int64")
        t["t"] = t["t"] + cum
        parts.append(t)
        if rec.get("lines") is not None and len(rec["lines"]):
            ln = rec["lines"].copy()
            ln["txn_id"] = f"{i}:" + ln["txn_id"].astype(str)
            ln["t"] = ln["t"] + cum
            line_parts.append(ln)
        clk = Clock(rec.get("started_at"), tz)
        undated += 0 if clk.known else 1
        starts.append(None if not clk.known else _minute_of_day(clk))
        shifts.append(cum)
        labels.append(rec.get("label"))
        cum += dur + GAP_S
    out = dict(tracks=pd.concat(parts, ignore_index=True), lines=pd.concat(line_parts, ignore_index=True) if line_parts else None,
               n=len(recordings), labels=labels, undated=undated, slot_fn=None, minute_of_day_fn=None)
    if undated == 0 and recordings:
        out["slot_fn"], out["minute_of_day_fn"] = clock_functions(shifts, starts, window_s)
    return out


def _minute_of_day(clk):
    s = clk.local(0)
    return s.hour * 60 + s.minute + s.second / 60.0


def clock_functions(shifts, start_minutes, window_s):
    """Functions from pooled time (seconds) to the window index of the clock day, and to the minute of the day."""
    shifts = np.asarray(shifts, float)
    starts = np.asarray(start_minutes, float)
    wm = window_s / 60.0
    n_slots = int(round(1440 / wm))

    def minute_of_day(t_s):
        t_s = np.asarray(t_s, float)
        i = np.clip(np.searchsorted(shifts, t_s, side="right") - 1, 0, len(shifts) - 1)
        return (starts[i] + (t_s - shifts[i]) / 60.0) % 1440

    def slot(t_s):
        return (minute_of_day(t_s) // wm).astype(int) % n_slots
    return slot, minute_of_day


def hhmm(minute):
    m = int(minute) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"
