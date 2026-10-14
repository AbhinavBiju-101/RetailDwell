"""Analytics parameters (see the ideation doc: dwell by displacement, not by carried items).

Store geometry and zones now live with each store (stores.py for the demo stores,
the database for real ones).
"""

DWELL_RADIUS_M = 1.0     # a stop stays within this radius...
DWELL_MIN_S = 5.0        # ...for at least this long
MIN_PRESENCE_S = 2.0     # time inside a zone to count as a visit
SAMPLE_HZ = 2.0          # stored track rate (points per second)
QUEUE_MARGIN_M = 3.0     # the queue area is the checkout zone widened by this much

TIME_WINDOW_S = 1800     # time-of-day windows for each zone's own baseline (30 minutes)
TIME_MIN_SHOPPERS = 20   # a window needs this many shoppers to be judged
TIME_MIN_EXPECTED = 5    # ...and the zone must be expected to get at least this many visitors in it
TIME_MIN_Z = 3.0         # how far beyond chance a window must be to be flagged

# ---- several cameras on one floor (server-side fusion)
FUSE_DIST_M = 0.8               # two cameras place one person within this distance
FUSE_MIN_OVERLAP_S = 1.0        # ...over at least this many shared seconds (a fast walker crosses a 3 m overlap in about 2)
FUSE_FRAC = 0.7                 # ...for at least this share of them
HANDOFF_GAP_S = 4.0             # a track starting this soon after another camera's track ended can continue it
HANDOFF_BASE_M = 0.8
HANDOFF_SPEED_MPS = 1.2

# ---- track stitching (runs in the camera agent; defaults here, overridable per store)
STITCH_MAX_GAP_S = 8.0
STITCH_BASE_M = 0.6
STITCH_SPEED_MPS = 1.2
STITCH_MIN_GAP_S = 0.5
STITCH_EDGE_M = 1.0

# ---- staff and parties
STAFF_ZONE_MIN_S = 60           # time inside a zone of type staff before a track counts as staff
STAFF_CHECKOUT_MIN_S = 600      # total time standing inside a checkout zone: a cashier, not a shopper
STAFF_MIN_DURATION_S = 2400     # on the floor this long (needs a recording at least twice as long to apply)
PARTY_DIST_M = 1.5              # people this close, moving together, may be one party
PARTY_MIN_S = 40                # ...for at least this long
PARTY_MIN_FRAC = 0.6            # ...and for this share of the shorter track
PARTY_MIN_MOVING_S = 10         # ...of which this much while at least one is walking (not just queueing side by side)
PARTY_ENTRY_GAP_S = 8           # companions first appear within this many seconds of each other, and last disappear within it
PARTY_MAX_SIZE = 6              # a bigger cluster is a crowd, not a party

# ---- sales (POS) matching and revenue per visitor
POS_TOL_S = 30.0          # a sale and the end of a shopper's stop at the register must be this close (after clock correction)
POS_OFFSET_MAX_S = 120.0  # POS clock drift searched for, either way
POS_CLEAR_CONF = 0.6      # match confidence at or above this is "clear"
POS_MIN_CONF = 0.3        # below this the match is "ambiguous" and left out of revenue figures
POS_MIN_MATCH_RATE = 0.7  # warn when fewer sales than this are tied to a shopper
POS_MIN_STOPPERS = 15     # a zone needs this many stoppers before its buy rate is judged
POS_MIN_Z = 3.0           # ...and its buy rate must be this many standard errors from the median (same bar as time-of-day flags)
POS_MAP_MIN_SHOPPERS = 8  # a map cell needs this many shoppers before its average spend is shown
POS_MAX_TXNS = 5000       # sales per recording (matching builds a sales x register-visits table)
POS_MAX_LINES = 50000     # rows per upload
