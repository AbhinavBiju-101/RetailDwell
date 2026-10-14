"""Wall-clock time for recordings.

A recording's positions are in seconds since it started. Knowing when it started (UTC, stored per recording) and
where the store is (an IANA timezone, stored per store) turns that into "Saturday 14:05", and lets a till export
with real timestamps be lined up with the tracks. Nothing else in the app depends on it: with no start time set,
everything keeps working in minutes since the recording started.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "UTC"
COMMON_ZONES = ["Asia/Kolkata", "UTC", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "Europe/London",
                "Europe/Paris", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Denver",
                "America/Los_Angeles", "America/Sao_Paulo", "Africa/Johannesburg", "Africa/Lagos"]


def valid_timezone(name):
    try:
        ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ValueError(f"'{name}' is not a timezone name. Use a name like Asia/Kolkata or Europe/London.")
    return str(name)


def _parse(text, tz):
    s = str(text).strip().replace("Z", "+00:00") if str(text).strip().endswith("Z") else str(text).strip()
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"'{text}' is not a date and time. Use a form like 2026-10-10 14:00.")
    return d.replace(tzinfo=ZoneInfo(tz)) if d.tzinfo is None else d


def parse_start(text, tz=DEFAULT_TZ, now=None):
    """A recording start typed by a person, in the store's timezone unless it carries its own offset.
    Returns an aware UTC datetime. Refuses dates that are not plausible."""
    d = _parse(text, tz).astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if not (2000 <= d.year <= 2100):
        raise ValueError("That start time is not plausible. Check the year.")
    if d > now + timedelta(days=1):
        raise ValueError("That start time is in the future.")
    return d


def iso(d):
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


class Clock:
    """Turns seconds since a recording started into local wall-clock labels."""

    def __init__(self, started_at=None, tz=None):
        self.tz_name = tz or DEFAULT_TZ
        try:
            self.tz = ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            self.tz_name, self.tz = DEFAULT_TZ, ZoneInfo(DEFAULT_TZ)
        self.start = None
        if started_at:
            try:
                s = str(started_at).strip()
                d = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
                self.start = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            except ValueError:
                self.start = None

    @classmethod
    def for_recording(cls, dataset, store):
        return cls((dataset or {}).get("started_at"), (store or {}).get("timezone"))

    @property
    def known(self):
        return self.start is not None

    def local(self, minutes):
        return (self.start + timedelta(minutes=minutes)).astimezone(self.tz)

    def window(self, a_min, b_min):
        """Label for a window, e.g. '14:05 to 14:10' (or '5 to 10' when the start is unknown)."""
        if not self.known:
            return f"{a_min} to {b_min}"
        return f"{self.local(a_min):%H:%M} to {self.local(b_min):%H:%M}"

    def span(self, a_min, b_min):
        """For sentences: 'minutes 5 and 10', or '14:05 and 14:10 on Sat 10 Oct'."""
        if not self.known:
            return f"minutes {a_min} and {b_min}"
        a, b = self.local(a_min), self.local(b_min)
        return f"{a:%H:%M} and {b:%H:%M} on {a:%a} {a.day} {a:%b}"

    def start_label(self):
        if not self.known:
            return None
        s = self.local(0)
        return f"{s:%a} {s.day} {s:%b} {s:%H:%M}"

    def local_input(self):
        """Value for an <input type=datetime-local>."""
        return self.local(0).strftime("%Y-%m-%dT%H:%M") if self.known else ""

    def seconds_since_start(self, values):
        """Till timestamps (ISO text, local or with an offset; or epoch seconds or milliseconds) -> seconds since
        the recording started. Plain small numbers are already seconds since the start and are returned as they are."""
        vals = [str(v).strip() for v in values]
        nums = []
        for v in vals:
            try:
                nums.append(float(v))
            except ValueError:
                nums = None
                break
        if nums is not None:
            if all(n < 1e9 for n in nums):
                return nums                                             # seconds since the recording started
            if not self.known:
                raise ValueError("These times are real timestamps, so the recording needs a start time. Set it in Settings, Layouts and recordings.")
            return [(n / 1000.0 if n > 1e11 else n) - self.start.timestamp() for n in nums]
        if not self.known:
            raise ValueError("These times are real timestamps, so the recording needs a start time. Set it in Settings, Layouts and recordings.")
        out = []
        for v in vals:
            try:
                out.append((_parse(v, self.tz_name).astimezone(timezone.utc) - self.start).total_seconds())
            except ValueError:
                raise ValueError(f"'{v}' in the t column is not a time. Use seconds since the start, or a timestamp like 2026-10-10 14:07:31.")
        return out
