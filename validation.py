"""Input validation shared by the web forms, the CSV upload and the ingest API.

Privacy by schema: only (track_id, t, x, y) are ever kept. Any other column or field
(for example something that could identify a person) is dropped on the way in.
"""
import hashlib
import io
import math
import re
import secrets

import pandas as pd

from stores import ZONE_TYPES

MAX_ZONES = 40
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_ROWS = 3_000_000
MAX_INGEST_POINTS = 5000
KEEP_COLS = ["track_id", "t", "x", "y"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,8}$")


class ValidationError(ValueError):
    """A problem the user can fix; the message is safe to show."""


def clean_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValidationError("Enter a valid email address.")
    return email


def check_password(pw):
    if not pw or len(pw) < 8:
        raise ValidationError("Use a password of at least 8 characters.")
    if len(pw) > 128:
        raise ValidationError("Use a password of 128 characters or fewer.")
    return pw


def clean_text(value, label, max_len=60, required=True):
    value = re.sub(r"\s+", " ", (value or "")).strip()
    if required and not value:
        raise ValidationError(f"Enter a {label}.")
    if len(value) > max_len:
        raise ValidationError(f"The {label} must be {max_len} characters or fewer.")
    return value


def clean_number(value, label, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"The {label} must be a number.")
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValidationError(f"The {label} must be between {lo:g} and {hi:g}.")
    return v


def clean_layout_key(key):
    key = (key or "").strip().upper()
    if not KEY_RE.match(key):
        raise ValidationError("A layout key uses 1 to 8 letters, numbers, dashes or underscores.")
    return key


def clean_zones(zones, w, h):
    """Validate {name: [x0, y0, x1, y1, type]} against the floor size. Returns a cleaned copy."""
    if not isinstance(zones, dict):
        raise ValidationError("Zones must be a list of named rectangles.")
    if len(zones) > MAX_ZONES:
        raise ValidationError(f"Use at most {MAX_ZONES} zones.")
    out = {}
    for name, z in zones.items():
        name = clean_text(name, "zone name", 40)
        if name in out or name.lower() == "circulation":
            raise ValidationError(f'The zone name "{name}" is already used or reserved.')
        if not isinstance(z, (list, tuple)) or len(z) != 5:
            raise ValidationError(f'Zone "{name}" needs x0, y0, x1, y1 and a type.')
        x0, y0, x1, y1 = (clean_number(v, f'"{name}" coordinate', -1, max(w, h) + 1) for v in z[:4])
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 < 0.3 or y1 - y0 < 0.3:
            raise ValidationError(f'Zone "{name}" is too small. Make it at least 0.3 m on each side.')
        if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
            raise ValidationError(f'Zone "{name}" extends outside the {w:g} by {h:g} m floor.')
        if z[4] not in ZONE_TYPES:
            raise ValidationError(f'Zone "{name}" has an unknown type. Use one of: {", ".join(ZONE_TYPES)}.')
        out[name] = [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2), z[4]]
    return out


def tracks_from_csv(data, w, h):
    """Parse an uploaded CSV (bytes). Keeps only track_id, t, x, y. Raises ValidationError."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError("That file is larger than 50 MB. Split it into several recordings.")
    try:
        df = pd.read_csv(io.BytesIO(data), nrows=MAX_UPLOAD_ROWS + 1)
    except Exception:
        raise ValidationError("That file could not be read as CSV (plain or .gz).")
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in KEEP_COLS if c not in df.columns]
    if missing:
        raise ValidationError("The CSV needs the columns track_id, t, x and y. Missing: " + ", ".join(missing) + ".")
    if len(df) > MAX_UPLOAD_ROWS:
        raise ValidationError("That file has more than 3 million rows. Split it into several recordings.")
    return clean_points_frame(df[KEEP_COLS], w, h)


def clean_points_frame(df, w, h):
    df = df[KEEP_COLS].copy()
    for c in KEEP_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    before = len(df)
    df = df.dropna()
    if df.empty:
        raise ValidationError("No usable rows. Check that track_id, t, x and y are numbers.")
    if len(df) < before * 0.9:
        raise ValidationError("More than 10% of rows were not numeric. Check the file format.")
    df["track_id"] = df["track_id"].astype("int64")
    df["x"] = df["x"].clip(-1, w + 1).round(2)
    df["y"] = df["y"].clip(-1, h + 1).round(2)
    df["t"] = df["t"].round(2)
    return df.sort_values(["track_id", "t"]).reset_index(drop=True)


def points_from_json(points, w, h):
    """Validate an ingest batch: a list of {track_id, t, x, y}. Extra fields are rejected."""
    if not isinstance(points, list) or not points:
        raise ValidationError("points must be a non-empty list.")
    if len(points) > MAX_INGEST_POINTS:
        raise ValidationError(f"Send at most {MAX_INGEST_POINTS} points per request.")
    rows = []
    for p in points:
        if not isinstance(p, dict) or set(p) != set(KEEP_COLS):
            raise ValidationError("Each point must have exactly track_id, t, x and y. Nothing else is accepted.")
        rows.append([p[c] for c in KEEP_COLS])
    return clean_points_frame(pd.DataFrame(rows, columns=KEEP_COLS), w, h)


# ---- API keys: only a hash is stored; the plain key is shown once
def new_api_key():
    plain = "dtm_" + secrets.token_urlsafe(32)
    return plain, plain[:12], hash_key(plain)


def hash_key(plain):
    return hashlib.sha256(plain.encode()).hexdigest()
