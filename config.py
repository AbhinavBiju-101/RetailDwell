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
