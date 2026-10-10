"""Camera agent: video in, anonymous floor points out.

Only (track_id, t, x, y) ever leaves this package. Frames live in memory for one detection pass and are
never written to disk (the one exception is `calibrate snapshot`, a single setup image you delete afterwards).
"""
