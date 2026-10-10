"""Analytics parameters (see the ideation doc: dwell by displacement, not by carried items).

Store geometry and zones now live with each store (stores.py for the demo stores,
the database for real ones).
"""

DWELL_RADIUS_M = 1.0     # a stop stays within this radius...
DWELL_MIN_S = 5.0        # ...for at least this long
MIN_PRESENCE_S = 2.0     # time inside a zone to count as a visit
SAMPLE_HZ = 2.0          # stored track rate (points per second)
QUEUE_MARGIN_M = 3.0     # the queue area is the checkout zone widened by this much
