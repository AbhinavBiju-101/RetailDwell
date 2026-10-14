"""Demo store specifications.

Each store has its own floor plan (zones in metres), shopper behaviour and a
before/after story, so the demo account shows genuinely different comparisons:

  lakeside-grocery   20 x 12  Offers end-cap moved beside the main aisle: reach rises.
  volt-electronics   26 x 14  Accessories rack moved to the till approach: reach rises,
                              but so does the checkout queue (a trade-off).
  thread-and-co      24 x 16  New-arrivals table moved to the entrance: it gets seen,
                              but shoppers stop there and skip the back of the store.
  corner-pharmacy    14 x 10  Same floor plan and same shoppers (paired seed), second pickup counter
                              opened: only the wait changes.

Zone tuple: (x0, y0, x1, y1, type). Types: merchandise, checkout, entrance, seating.
"""

DEFAULT_MIX = dict(loiterer=0.08, directed=0.42)  # remainder: exploratory

STORES = {
    # ------------------------------------------------------------------ grocery
    "lakeside-grocery": dict(
        name="Lakeside Grocery", kind="Grocery", w=20.0, h=12.0,
        blurb="Neighbourhood grocer. The weekly offers display was moved from the far wall to the main aisle.",
        entrance=(2.0, 0.8), aisle_y=4.0, register=(17.5, 2.0), queue=(14.8, 2.4),
        service=(12, 28), servers=1, window_s=300, hero="Weekly offers", timezone="Asia/Kolkata",
        interest={"Produce": 0.55, "Snacks": 0.5, "Beverages": 0.35, "Household": 0.25, "Weekly offers": 0.7},
        directed_mult={"Weekly offers": 0.5}, mix=DEFAULT_MIX,
        loiter_box=(0.8, 1.2, 3.2, 2.8),
        layouts={
            "A": dict(label="Before: offers on the far wall", visits=350, window_s=7200, starts="2026-10-03T05:30:00+00:00", seed=1,
                      zones={
                          "Entrance": (0, 0, 4, 3, "entrance"), "Checkout": (15, 0, 20, 4, "checkout"),
                          "Produce": (0, 6, 5, 12, "merchandise"), "Snacks": (6, 8, 10, 12, "merchandise"),
                          "Beverages": (11, 8, 16, 12, "merchandise"), "Household": (16.5, 9.5, 20, 12, "merchandise"),
                          "Weekly offers": (16.5, 5, 20, 9, "merchandise")}),
            "B": dict(label="After: offers beside the main aisle", visits=480, window_s=7200, starts="2026-10-10T05:30:00+00:00", seed=2,
                      zones={
                          "Entrance": (0, 0, 4, 3, "entrance"), "Checkout": (15, 0, 20, 4, "checkout"),
                          "Produce": (0, 6, 5, 12, "merchandise"), "Snacks": (6, 8, 10, 12, "merchandise"),
                          "Beverages": (11, 8, 16, 12, "merchandise"), "Household": (16.5, 9.5, 20, 12, "merchandise"),
                          "Weekly offers": (6, 4.6, 10, 7.4, "merchandise")}),
        },
    ),
    # ------------------------------------------------------------------ electronics
    "volt-electronics": dict(
        name="Volt Electronics", kind="Electronics", w=26.0, h=14.0,
        blurb="Large-format electronics. The accessories rack moved next to the tills to catch impulse buys.",
        entrance=(2.0, 0.8), aisle_y=4.0, register=(22.5, 2.2), queue=(19.6, 2.6),
        service=(15, 35), servers=1, window_s=300, hero="Accessories", timezone="Asia/Kolkata",
        interest={"Phones": 0.6, "Laptops": 0.45, "TV wall": 0.3, "Audio": 0.3, "Accessories": 0.55},
        directed_mult={}, buy_boost={"Accessories": 0.3}, vis=(0.7, 0.8),
        dwell={"TV wall": (30, 90), "Laptops": (15, 60)}, mix=dict(loiterer=0.06, directed=0.38),
        loiter_box=(0.8, 1.2, 3.2, 2.8),
        layouts={
            "A": dict(label="Before: accessories at the back", visits=300, window_s=7200, starts="2026-10-03T05:30:00+00:00", seed=11,
                      zones={
                          "Entrance": (0, 0, 4, 3, "entrance"), "Checkout": (19, 0, 26, 4, "checkout"),
                          "Phones": (0, 6.5, 6, 12, "merchandise"), "Laptops": (7, 8.5, 13, 13, "merchandise"),
                          "TV wall": (14, 8, 20, 13.5, "merchandise"), "Audio": (21, 5.5, 26, 9, "merchandise"),
                          "Accessories": (21, 10, 26, 13.5, "merchandise")}),
            "B": dict(label="After: accessories at the tills", visits=310, window_s=7200, starts="2026-10-10T05:30:00+00:00", seed=12,
                      zones={
                          "Entrance": (0, 0, 4, 3, "entrance"), "Checkout": (19, 0, 26, 4, "checkout"),
                          "Phones": (0, 6.5, 6, 12, "merchandise"), "Laptops": (7, 8.5, 13, 13, "merchandise"),
                          "TV wall": (14, 8, 20, 13.5, "merchandise"), "Audio": (21, 5.5, 26, 9, "merchandise"),
                          "Accessories": (13.5, 0.3, 18.5, 3.4, "merchandise")}),
        },
    ),
    # ------------------------------------------------------------------ apparel
    "thread-and-co": dict(
        name="Thread & Co.", kind="Apparel", w=24.0, h=16.0,
        blurb="Fashion boutique. The new-arrivals table moved to the entrance, which changes where people stop.",
        entrance=(2.0, 0.8), aisle_y=4.0, register=(20.5, 2.2), queue=(17.6, 2.6),
        service=(18, 40), servers=1, window_s=300, hero="New arrivals", timezone="Asia/Kolkata",
        interest={"New arrivals": 0.6, "Women": 0.55, "Men": 0.4, "Denim": 0.35, "Fitting rooms": 0.3},
        directed_mult={"New arrivals": 0.7, "Fitting rooms": 0.4}, stop_cap=(2, 4), vis=(0.6, 1.0),
        dwell={"Fitting rooms": (25, 70), "New arrivals": (12, 40)}, mix=dict(loiterer=0.07, directed=0.3),
        loiter_box=(0.8, 1.2, 3.2, 2.8),
        layouts={
            "A": dict(label="Before: new arrivals at the back", visits=330, window_s=7200, starts="2026-10-03T05:30:00+00:00", seed=21,
                      zones={
                          "Entrance": (0, 0, 4, 3, "entrance"), "Checkout": (17, 0, 24, 4, "checkout"),
                          "Women": (0, 6, 8, 15, "merchandise"), "Men": (9, 6, 16, 11.5, "merchandise"),
                          "Denim": (17, 9.5, 24, 15, "merchandise"), "Fitting rooms": (17, 5, 24, 8.5, "merchandise"),
                          "New arrivals": (9, 12.3, 16, 15.5, "merchandise")}),
            "B": dict(label="After: new arrivals at the entrance", visits=345, window_s=7200, starts="2026-10-10T05:30:00+00:00", seed=22,
                      zones={
                          "Entrance": (0, 0, 4, 3, "entrance"), "Checkout": (17, 0, 24, 4, "checkout"),
                          "Women": (0, 6, 8, 15, "merchandise"), "Men": (9, 6, 16, 11.5, "merchandise"),
                          "Denim": (17, 9.5, 24, 15, "merchandise"), "Fitting rooms": (17, 5, 24, 8.5, "merchandise"),
                          "New arrivals": (4.6, 0.3, 10.6, 3.4, "merchandise")}),
        },
    ),
    # ------------------------------------------------------------------ pharmacy
    "corner-pharmacy": dict(
        name="Corner Pharmacy", kind="Pharmacy", w=14.0, h=10.0,
        blurb="Small pharmacy. Same floor plan, but a second pickup counter was opened to shorten the queue.",
        entrance=(1.5, 0.8), aisle_y=3.6, register=(11.6, 1.7), queue=(8.6, 2.2),
        service=(35, 60), servers=1, window_s=600, hero="Everyday health", timezone="Asia/Kolkata", buy_rates=(0.9, 0.55),
        interest={"Everyday health": 0.5, "Wellness": 0.4, "Baby & care": 0.3},
        directed_mult={}, mix=dict(loiterer=0.15, directed=0.45),
        loiter_box=(0.4, 3.9, 3.0, 5.2),
        layouts={
            "A": dict(label="Before: one pickup counter", visits=220, window_s=7200, starts="2026-10-03T05:30:00+00:00", seed=31, servers=1,
                      zones={
                          "Entrance": (0, 0, 3, 2.5, "entrance"), "Checkout": (9, 0, 14, 3.2, "checkout"),
                          "Waiting seats": (0, 3.8, 3.2, 5.4, "seating"),
                          "Everyday health": (0, 6, 5, 9.5, "merchandise"), "Wellness": (5.6, 6.2, 10, 9.5, "merchandise"),
                          "Baby & care": (10.6, 4.6, 14, 9.5, "merchandise")}),
            "B": dict(label="After: two pickup counters", visits=220, window_s=7200, starts="2026-10-10T05:30:00+00:00", seed=31, servers=2,
                      zones={
                          "Entrance": (0, 0, 3, 2.5, "entrance"), "Checkout": (9, 0, 14, 3.2, "checkout"),
                          "Waiting seats": (0, 3.8, 3.2, 5.4, "seating"),
                          "Everyday health": (0, 6, 5, 9.5, "merchandise"), "Wellness": (5.6, 6.2, 10, 9.5, "merchandise"),
                          "Baby & care": (10.6, 4.6, 14, 9.5, "merchandise")}),
        },
    ),
}

ZONE_TYPES = ["merchandise", "checkout", "entrance", "seating", "staff"]

# Mock sales, one config per demo store (prices in rupees). A shopper who stopped at a zone buys from it with
# probability take[zone] (directed shoppers are pulled up towards 1; a (explorer, directed) pair sets both). `extra` items are bought regardless of
# where the shopper went (here: prescriptions, which are not a floor zone). The POS clock runs skew_s ahead of
# the camera clock, with noise_s of jitter, and a few sales (untracked) have no matching track at all.
# The simulator sets all of this, so the numbers show the method recovers a known pattern, not real behaviour.
# Extra people for the demo, added after the shoppers are simulated (their tracks are not touched): party_rate is the
# share of shoppers who arrive with 1 or 2 companions who walk with them and do not pay; staff is the number of staff
# on the floor all day (a cashier behind the register and a stocker who patrols and pauses at the shelves).
CROWD = {"corner-pharmacy": dict(party_rate=0.22, staff=2)}

DEFAULT_TAKE = 0.55
BASKETS = {
    "lakeside-grocery": dict(
        price={"Produce": 120, "Snacks": 90, "Beverages": 110, "Household": 260, "Weekly offers": 150},
        take={"Weekly offers": 0.8}, skew_s=4.0, noise_s=3.0),
    "volt-electronics": dict(
        price={"Phones": 18000, "Laptops": 42000, "TV wall": 38000, "Audio": 3500, "Accessories": 900},
        take={"Phones": 0.45, "Laptops": (0.06, 0.12), "TV wall": (0.05, 0.10), "Audio": 0.4, "Accessories": 0.7}, skew_s=-6.0, noise_s=4.0),
    "thread-and-co": dict(
        price={"New arrivals": 2200, "Women": 1800, "Men": 1600, "Denim": 2400, "Fitting rooms": 1900},
        take={"New arrivals": 0.3, "Women": 0.5, "Men": 0.45, "Denim": 0.5, "Fitting rooms": 0.8}, skew_s=2.0, noise_s=4.0),
    "corner-pharmacy": dict(
        price={"Everyday health": 180, "Wellness": 350, "Baby & care": 420},
        take={"Everyday health": 0.6, "Wellness": 0.5, "Baby & care": 0.45},
        extra={"Prescriptions": (0.55, 520)}, skew_s=0.0, noise_s=3.0),
}


def store_keys():
    return list(STORES)


def main_path(spec):
    """Walking route used to judge how visible a zone is: entrance, up to the aisle, along it, to the till."""
    ent, q = spec["entrance"], spec["queue"]
    return [(ent[0], 1.0), (ent[0], spec["aisle_y"]), (q[0], spec["aisle_y"]), spec["register"]]
