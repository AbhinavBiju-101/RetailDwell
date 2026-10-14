"""Per-store detection settings.

Every setting here has a default from config.py, a range, and a plain-language explanation, and is stored per store
(not per layout), so a before/after experiment always compares two layouts judged by the same rules.

Staff, party and sales settings are applied by the server whenever a recording is analysed, so changing one re-analyses
existing recordings at once. Stitching settings run in the camera agent, which fetches them from the server when it
starts (python -m agent run ... --api), so they apply to the next run, not to points already sent.

Only values that differ from the default are stored. A stored value that is out of range, unknown or the wrong type is
ignored when settings are read, so a bad row can never break a page; it is refused when settings are saved.
"""
from collections import OrderedDict, namedtuple

import config as C

Spec = namedtuple("Spec", "key default lo hi kind group label help unit applies")

GROUPS = OrderedDict([
    ("staff", "Staff"),
    ("parties", "Parties"),
    ("cameras", "Several cameras (fusion)"),
    ("stitching", "Track stitching (camera agent)"),
    ("sales", "Sales matching"),
])
SERVER, AGENT = "Applies to existing recordings straight away.", "Applies the next time a camera agent starts."

SPECS = [
    Spec("staff_filter", True, None, None, "bool", "staff", "Leave staff out of the figures",
         "Tracks that behave like staff are removed from every figure. Turn off to count everyone.", "", SERVER),
    Spec("staff_zone_min_s", C.STAFF_ZONE_MIN_S, 10, 600, "int", "staff", "Time in a staff zone",
         "A track that spends this long inside a zone of type staff is staff.", "seconds", SERVER),
    Spec("staff_checkout_min_s", C.STAFF_CHECKOUT_MIN_S, 120, 3600, "int", "staff", "Time standing in the checkout zone",
         "A track that stands in a checkout zone this long in total is a cashier, not a shopper. Raise it if customers can queue inside the zone for a long time.", "seconds", SERVER),
    Spec("staff_min_duration_s", C.STAFF_MIN_DURATION_S, 900, 14400, "int", "staff", "Time on the floor",
         "A track on the floor this long is staff. Only judged when the recording is at least twice as long.", "seconds", SERVER),

    Spec("parties_enabled", True, None, None, "bool", "parties", "Detect parties",
         "People who arrive, walk and leave together are treated as one buyer. Turn off to treat everyone alone.", "", SERVER),
    Spec("party_dist_m", C.PARTY_DIST_M, 0.5, 3.0, "float", "parties", "How close together",
         "People must stay within this distance of each other.", "metres", SERVER),
    Spec("party_min_s", C.PARTY_MIN_S, 10, 300, "int", "parties", "How long together",
         "...for at least this long.", "seconds", SERVER),
    Spec("party_min_frac", C.PARTY_MIN_FRAC, 0.3, 0.95, "float", "parties", "Share of the visit together",
         "...and for this share of the shorter visit.", "0 to 1", SERVER),
    Spec("party_min_moving_s", C.PARTY_MIN_MOVING_S, 0, 120, "int", "parties", "Time walking together",
         "At least this much of that time one of them is walking, so strangers queueing side by side are not joined.", "seconds", SERVER),
    Spec("party_entry_gap_s", C.PARTY_ENTRY_GAP_S, 1, 60, "int", "parties", "Arrive and leave within",
         "Companions first appear, and last disappear, within this many seconds of each other.", "seconds", SERVER),
    Spec("party_max_size", C.PARTY_MAX_SIZE, 2, 12, "int", "parties", "Largest party",
         "A bigger cluster is treated as a crowd, not one party.", "people", SERVER),

    Spec("fusion_enabled", True, None, None, "bool", "cameras", "Merge tracks from different cameras",
         "When cameras overlap or hand over to each other, one person must not count as two. Only does anything when more than one camera sends data.", "", SERVER),
    Spec("fuse_dist_m", C.FUSE_DIST_M, 0.2, 3.0, "float", "cameras", "Cameras agree within",
         "Two cameras are seeing the same person if they place them this close together. Set it from how far apart the cameras place the same person (shown on Overview), not lower.", "metres", SERVER),
    Spec("fuse_min_overlap_s", C.FUSE_MIN_OVERLAP_S, 1, 30, "float", "cameras", "Seen together for at least",
         "Tracks from two cameras must overlap in time for this long before they can be one person.", "seconds", SERVER),
    Spec("fuse_frac", C.FUSE_FRAC, 0.4, 0.95, "float", "cameras", "Share of that time close",
         "...and be within the distance above for this share of the overlap.", "0 to 1", SERVER),
    Spec("handoff_gap_s", C.HANDOFF_GAP_S, 0, 60, "float", "cameras", "Longest hand-over gap",
         "A track that starts this soon after a different camera's track ended, nearby, continues it. 0 turns hand-overs off.", "seconds", SERVER),
    Spec("handoff_base_m", C.HANDOFF_BASE_M, 0.2, 3.0, "float", "cameras", "Hand-over position jump",
         "How far from where the earlier track ended the new one may start, before allowing for walking.", "metres", SERVER),
    Spec("handoff_speed_mps", C.HANDOFF_SPEED_MPS, 0.3, 3.0, "float", "cameras", "Hand-over walking pace",
         "Added to the jump for every second of gap.", "metres per second", SERVER),

    Spec("stitch", True, None, None, "bool", "stitching", "Re-link tracks that vanish and reappear",
         "Stops one person from being counted as several when the camera loses them for a moment.", "", AGENT),
    Spec("stitch_max_gap_s", C.STITCH_MAX_GAP_S, 1, 60, "float", "stitching", "Longest gap bridged",
         "A new track can continue an earlier one lost up to this long ago. Longer fixes more standing people but risks joining two different people.", "seconds", AGENT),
    Spec("stitch_base_m", C.STITCH_BASE_M, 0.2, 3.0, "float", "stitching", "Allowed position jump",
         "How far from where it was last seen a new track may start, before allowing for walking.", "metres", AGENT),
    Spec("stitch_speed_mps", C.STITCH_SPEED_MPS, 0.3, 3.0, "float", "stitching", "Walking pace allowed in the gap",
         "Added to the jump for every second of gap.", "metres per second", AGENT),
    Spec("stitch_min_gap_s", C.STITCH_MIN_GAP_S, 0, 5, "float", "stitching", "Shortest gap",
         "A track still being seen is never continued.", "seconds", AGENT),
    Spec("stitch_edge_m", C.STITCH_EDGE_M, 0, 3, "float", "stitching", "Floor-edge guard",
         "A track last seen this close to the edge of the floor is assumed to have left and is not continued.", "metres", AGENT),

    Spec("pos_tol_s", C.POS_TOL_S, 5, 120, "float", "sales", "Sale-to-shopper time window",
         "A sale is matched to a shopper whose stop at the register ended within this long of it, after clock correction.", "seconds", SERVER),
    Spec("pos_min_conf", C.POS_MIN_CONF, 0.05, 0.55, "float", "sales", "Lowest confidence kept",
         "Matches below this are ambiguous and left out of revenue figures.", "0 to 1", SERVER),
]
BY_KEY = {s.key: s for s in SPECS}
AGENT_KEYS = [s.key for s in SPECS if s.group == "stitching"]


def defaults():
    return {s.key: s.default for s in SPECS}


def _coerce(spec, v):
    """The value as the right type, or ValueError."""
    if spec.kind == "bool":
        if isinstance(v, bool):
            return v
        if str(v).strip().lower() in ("1", "true", "on", "yes"):
            return True
        if str(v).strip().lower() in ("0", "false", "off", "no"):
            return False
        raise ValueError("must be on or off")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("must be a number")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("must be a number")
    if spec.kind == "int":
        if x != int(x):
            raise ValueError("must be a whole number")
        x = int(x)
    if not (spec.lo <= x <= spec.hi):
        raise ValueError(f"must be between {spec.lo:g} and {spec.hi:g}")
    return x


def resolve(raw):
    """Effective settings: the defaults with any valid stored values on top. Never raises."""
    out = defaults()
    for k, v in (raw or {}).items():
        spec = BY_KEY.get(k)
        if spec is not None:
            try:
                out[k] = _coerce(spec, v)
            except ValueError:
                pass
    return out


def parse_form(form):
    """Settings from the settings page, as the dict to store (only values that differ from the default).
    Empty fields mean the default. Raises ValueError naming the first bad field."""
    out = {}
    for s in SPECS:
        v = form.get("t_" + s.key)
        if v is None or str(v).strip() == "":
            continue
        try:
            x = _coerce(s, v)
        except ValueError as e:
            raise ValueError(f"{s.label} {e}.")
        if x != s.default:
            out[s.key] = x
    return out


def agent_settings(raw):
    """What a camera agent needs, as plain JSON."""
    eff = resolve(raw)
    return {k: eff[k] for k in AGENT_KEYS}


def describe(raw):
    """For the settings page: groups of specs with their current value and whether it is the default."""
    eff = resolve(raw)
    groups = OrderedDict((g, []) for g in GROUPS)
    for s in SPECS:
        groups[s.group].append(dict(spec=s, value=eff[s.key], is_default=eff[s.key] == s.default))
    return [(GROUPS[g], rows) for g, rows in groups.items()]
