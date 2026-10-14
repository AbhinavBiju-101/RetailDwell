"""Sales (POS) matching and revenue per visitor.

Input is the analysis of one recording (analytics.analyse_layout) plus a list of sales lines:
    txn_id, t, zone, amount     one row per section of a basket; t is seconds since the recording
                                started, the same clock as the tracks; zone is the section the items
                                belong to (the SKU-to-section map is the store's job).

How a sale is tied to a shopper
    1. Find every track that stood still in a checkout zone and take when its last stop there ended.
       That is when the shopper finished paying.
    2. Estimate the clock gap between the POS and the camera. Cashier systems drift, so the gap is
       found from the data: the offset at which the most sales line up with a register visit.
    3. Assign sales to register visits one-to-one (Hungarian algorithm on the time difference), allowing
       only pairs within POS_TOL_S seconds.
    4. Give every pair a confidence from how much better it is than the next-best alternative on either
       side. Pairs below POS_MIN_CONF are "ambiguous" (typically two tills finishing together). They are
       counted but kept out of revenue figures, because a wrong basket is worse than a missing one.

Nobody carries anything and nothing identifies anyone: the join is by time and place at the register.
What comes out is correlation. A shopper who bought from a zone is not shown to have bought *because*
of it, and shoppers who never reach a register never match.
"""
import math

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from analytics import DT, LOITERER, _stop_spans
from config import (POS_CLEAR_CONF, POS_MAP_MIN_SHOPPERS, POS_MIN_CONF, POS_MIN_MATCH_RATE,
                    POS_MIN_STOPPERS, POS_MIN_Z, POS_OFFSET_MAX_S, POS_TOL_S, QUEUE_MARGIN_M)

POS_COLS = ["txn_id", "t", "zone", "amount"]
SEGMENTS = ["directed", "exploratory", LOITERER, "pass-through"]
UNMAPPED = "Unmapped"
_BIG = 1e6


# ---------------------------------------------------------------- register visits
def register_services(df, zones, party_of=None):
    """One row per track that stood at a checkout: when its last stop there started and ended.
    With `party_of` ({track_id: party_id}) a party is one row: people who arrive together stand at the register
    together, and a companion's visit must not look like a second customer. The row carries the party's lowest track id
    and the mean of its members' end times."""
    cols = ["track_id", "t_start", "t_end"]
    cz = [z for z in zones.values() if z[4] == "checkout"]
    if not cz or df.empty:
        return pd.DataFrame(columns=cols)
    m = np.zeros(len(df), dtype=bool)
    for z in cz:
        m |= ((df["x"] >= z[0]) & (df["x"] <= z[2]) & (df["y"] >= z[1]) & (df["y"] <= z[3])).to_numpy()
    rows = []
    for tid, g in df[m].groupby("track_id"):
        if len(g) < 3:
            continue
        spans = _stop_spans(g.sort_values("t"))
        if spans:
            rows.append((tid, spans[-1][2], spans[-1][3]))
    out = pd.DataFrame(rows, columns=cols)
    if party_of and len(out):
        out["party"] = out["track_id"].map(lambda t: party_of.get(int(t), int(t)))
        out = out.groupby("party", as_index=False).agg(track_id=("track_id", "min"), t_start=("t_start", "min"), t_end=("t_end", "mean"))
        out = out[cols]
    return out


# ---------------------------------------------------------------- clock offset
def estimate_offset(t_txn, t_end, max_s=POS_OFFSET_MAX_S, bin_s=2.0):
    """POS clock minus camera clock, in seconds, from the peak in the histogram of every sale-to-visit
    time difference. Returns (offset, found). Not found when there is no clear peak, then 0 is used."""
    if len(t_txn) < 5 or len(t_end) < 5:
        return 0.0, False
    ends = np.sort(np.asarray(t_end, float))
    t_txn = np.asarray(t_txn, float)
    lo = np.searchsorted(ends, t_txn - max_s - bin_s)
    hi = np.searchsorted(ends, t_txn + max_s + bin_s)
    parts = [t_txn[i] - ends[lo[i]:hi[i]] for i in range(len(t_txn)) if hi[i] > lo[i]]
    if not parts:
        return 0.0, False
    d = np.concatenate(parts)
    edges = np.arange(-max_s - bin_s, max_s + 2 * bin_s, bin_s)
    hist, _ = np.histogram(d, bins=edges)
    sm = np.convolve(hist, np.ones(3), mode="same")
    k = int(np.argmax(sm))
    floor = max(1.0, float(np.median(sm)))
    # A real alignment stands well clear of the random baseline. Otherwise the clocks are assumed to agree.
    if sm[k] < 5 or sm[k] < floor + 5 * np.sqrt(floor):
        return 0.0, False
    # Regular service intervals make a comb of near-equal peaks (the right offset, plus or minus one service
    # time, and so on). Clocks are normally close, so among the near-equal peaks take the one nearest zero.
    mid = (edges[:-1] + edges[1:]) / 2
    local = np.array([sm[i] == sm[max(0, i - 2):i + 3].max() for i in range(len(sm))])
    cand = np.where(local & (sm >= 0.85 * sm[k]))[0]
    centre = float(mid[min(cand, key=lambda i: abs(mid[i]))])
    for _ in range(3):                       # re-centre on the median so the window is not lopsided
        near = d[np.abs(d - centre) <= 3 * bin_s]
        centre = float(np.median(near))
    return centre, True


# ---------------------------------------------------------------- matching
def _empty_match(n_txn, n_services, offset=0.0, found=False, tol_s=POS_TOL_S):
    pairs = pd.DataFrame(columns=["txn_id", "track_id", "t_txn", "t_end", "residual_s", "confidence", "status"])
    return dict(pairs=pairs, summary=dict(
        n_txn=n_txn, n_services=n_services, n_assigned=0, n_usable=0, n_clear=0, n_probable=0, n_ambiguous=0,
        n_unmatched_txn=n_txn, n_unmatched_services=n_services, offset_s=offset, offset_found=found,
        tol_s=tol_s, match_rate=0.0, median_abs_residual_s=None))


def match_transactions(services, txns, tol_s=POS_TOL_S, offset_s=None, min_conf=POS_MIN_CONF):
    """services: register_services(). txns: DataFrame txn_id, t (one row per sale).
    Returns dict(pairs, summary). `offset_s` forces the clock offset; None estimates it."""
    n_txn, n_srv = len(txns), len(services)
    if n_txn == 0 or n_srv == 0:
        return _empty_match(n_txn, n_srv, tol_s=tol_s)
    t_txn = txns["t"].to_numpy(float)
    t_end = services["t_end"].to_numpy(float)
    if offset_s is None:
        offset, found = estimate_offset(t_txn, t_end)
    else:
        offset, found = float(offset_s), False
    cost = np.abs(t_txn[:, None] - (t_end[None, :] + offset))
    gated = np.where(cost <= tol_s, cost, _BIG)
    ri, ci = linear_sum_assignment(gated)
    rows = []
    for i, j in zip(ri, ci):
        if cost[i, j] > tol_s:
            continue
        r = cost[i, j]
        row_alt = np.delete(cost[i], j)
        col_alt = np.delete(cost[:, j], i)
        alts = [a.min() for a in (row_alt, col_alt) if a.size and a.min() <= tol_s]
        if alts:
            alt = min(alts)
            conf = float(np.clip((alt - r) / (alt + r + 1e-9), 0.0, 1.0))
        else:
            conf = 1.0
        status = "clear" if conf >= POS_CLEAR_CONF else "probable" if conf >= min_conf else "ambiguous"
        rows.append((txns["txn_id"].iloc[i], services["track_id"].iloc[j], t_txn[i], t_end[j],
                     t_txn[i] - t_end[j] - offset, round(conf, 3), status))
    pairs = pd.DataFrame(rows, columns=["txn_id", "track_id", "t_txn", "t_end", "residual_s", "confidence", "status"])
    n_clear, n_prob = int((pairs.status == "clear").sum()), int((pairs.status == "probable").sum())
    n_amb = int((pairs.status == "ambiguous").sum())
    usable = n_clear + n_prob
    summary = dict(
        n_txn=n_txn, n_services=n_srv, n_assigned=len(pairs), n_usable=usable, n_clear=n_clear, n_probable=n_prob,
        n_ambiguous=n_amb, n_unmatched_txn=n_txn - len(pairs), n_unmatched_services=n_srv - len(pairs),
        offset_s=offset, offset_found=found, tol_s=tol_s, match_rate=usable / n_txn,
        median_abs_residual_s=float(pairs.residual_s.abs().median()) if len(pairs) else None)
    return dict(pairs=pairs, summary=summary)


# ---------------------------------------------------------------- the report
def _transactions(lines):
    return (lines.groupby("txn_id").agg(t=("t", "min"), amount=("amount", "sum")).reset_index())


def analyse_pos(R, lines, tol_s=None, min_conf=None):
    """Match the sales to the shoppers of one analysed recording and build the revenue figures.
    Returns None when there are no sales."""
    if lines is None or len(lines) == 0:
        return None
    lines = lines[POS_COLS].copy()
    txns = _transactions(lines)
    zones = R["zones"]
    party_of = (R.get("parties") or {}).get("of", {})
    party = lambda t: party_of.get(int(t), int(t))
    members_of = {}
    for t_, p_ in party_of.items():
        members_of.setdefault(p_, []).append(t_)
    services = register_services(R["df"], zones, party_of)
    prm = R.get("params") or {}
    tol_s = tol_s if tol_s is not None else prm.get("pos_tol_s", POS_TOL_S)
    min_conf = min_conf if min_conf is not None else prm.get("pos_min_conf", POS_MIN_CONF)
    match = match_transactions(services, txns, tol_s, min_conf=min_conf)
    S = match["summary"]
    pairs = match["pairs"]
    has_checkout = any(z[4] == "checkout" for z in zones.values())

    usable = pairs[pairs.status != "ambiguous"]
    ambiguous = {m for t_ in pairs[pairs.status == "ambiguous"].track_id for m in members_of.get(party(t_), [int(t_)])}
    amount = txns.set_index("txn_id")["amount"]
    spend_by_track = usable.set_index("track_id")["txn_id"].map(amount)

    lab = R["labels"][["track_id", "mission", "confidence"]].copy()
    lab["known"] = ~lab.track_id.isin(ambiguous)          # a track tied to an unclear sale: purchase unknown
    lab["paid"] = lab.track_id.isin(spend_by_track.index)                   # the member whose sale was matched
    lab["bought"] = lab.track_id.map(party).isin({party(t_) for t_ in spend_by_track.index})   # anyone in a party that paid
    lab["spend"] = lab.track_id.map(spend_by_track).fillna(0.0)
    shop = lab[(lab.mission != LOITERER) & lab.known]
    shop_ids = set(shop.track_id)

    matched_lines = lines.merge(usable[["txn_id", "track_id"]], on="txn_id", how="inner")      # one payer per sale: revenue
    each = pd.DataFrame([(r.txn_id, m) for r in usable.itertuples() for m in members_of.get(party(r.track_id), [int(r.track_id)])],
                        columns=["txn_id", "track_id"])
    member_lines = lines.merge(each, on="txn_id", how="inner")                                  # every member: who bought what
    total_rev = float(txns.amount.sum())
    matched_rev = float(spend_by_track.sum())
    loit = lab[(lab.mission == LOITERER) & lab.bought]

    kpi = dict(
        n_shoppers=int(len(lab[lab.mission != LOITERER])), n_known=int(len(shop)),
        n_unknown=int((~lab.known & (lab.mission != LOITERER)).sum()),
        buyers=int(shop.bought.sum()), conversion=float(shop.bought.mean()) if len(shop) else None,
        revenue_per_shopper=float(shop.spend.sum() / len(shop)) if len(shop) else None,
        avg_basket=float(shop[shop.paid].spend.mean()) if shop.paid.any() else None,
        n_parties=int(sum(1 for v in (R.get("parties") or {}).get("sizes", {}).values() if v > 1)),
        in_parties=int(sum(v for v in (R.get("parties") or {}).get("sizes", {}).values() if v > 1)),
        staff_excluded=len(R.get("staff") or {}),
        revenue_per_party=(float(shop.spend.sum() / shop.track_id.map(party).nunique()) if len(shop) else None),
        n_units=int(shop.track_id.map(party).nunique()),
        total_revenue=total_rev, matched_revenue=matched_rev,
        coverage=matched_rev / total_rev if total_rev else None,
        loiterer_buyers=int(len(loit)), loiterer_revenue=float(loit.spend.sum()))

    missions = []
    for seg in SEGMENTS:
        g = lab[(lab.mission == seg) & lab.known]
        b, pay = g[g.bought], g[g.paid]
        missions.append(dict(mission=seg, n=int(len(g)), buyers=int(len(b)),
                             conversion=float(len(b) / len(g)) if len(g) else None,
                             avg_basket=float(pay.spend.mean()) if len(pay) else None,
                             revenue_per_visit=float(g.spend.sum() / len(g)) if len(g) else None))

    zone_rows = _zone_funnel(R, shop, shop_ids, matched_lines, member_lines)
    miss = _tracking_miss(R, shop_ids, matched_lines, party_of)
    P = dict(
        match=S, pairs=pairs, kpi=kpi, missions=missions, zones=zone_rows, miss=miss, tol_s=tol_s,
        has_checkout=has_checkout, shop=shop[["track_id", "spend", "bought", "paid"]].reset_index(drop=True),
        spend_by_track=spend_by_track, lines=matched_lines, all_lines=lines,
        lab=lab, unmapped=sorted(set(lines.zone) - set(zones) - {UNMAPPED}) if len(lines) else [])
    P["items"] = pos_flags(P)
    return P


def _zone_funnel(R, shop, shop_ids, matched_lines, member_lines=None):
    member_lines = matched_lines if member_lines is None else member_lines
    pres, eps = R["presence"], R["episodes"]
    n = len(shop_ids)
    rows = []
    spend = shop.set_index("track_id")["spend"]
    for name, z in R["zones"].items():
        if z[4] != "merchandise":
            continue
        reached = set(pres[pres.zone == name].track_id) & shop_ids
        stopped = set(eps[(eps.zone == name) & (eps.zone_type == "merchandise")].track_id) & shop_ids
        zl = matched_lines[(matched_lines.zone == name) & matched_lines.track_id.isin(shop_ids)]    # payer lines: revenue
        buyers = set(member_lines[(member_lines.zone == name) & member_lines.track_id.isin(shop_ids)].track_id)   # a party buys together
        in_stop = buyers & stopped
        in_reach_only = (buyers & reached) - stopped
        missed = buyers - reached
        sp_in = spend.reindex(list(stopped)).mean() if stopped else None
        sp_out = spend.drop(index=list(stopped), errors="ignore").mean() if len(spend) > len(stopped) else None
        rows.append(dict(
            zone=name, reached=len(reached), stopped=len(stopped), bought=len(buyers),
            bought_after_stop=len(in_stop), bought_without_stop=len(in_reach_only), bought_not_seen=len(missed),
            share_reached=len(reached) / n if n else 0.0, share_stopped=len(stopped) / n if n else 0.0,
            share_bought=len(buyers) / n if n else 0.0,
            stop_to_buy=len(in_stop) / len(stopped) if stopped else None,
            reach_to_buy=len(buyers & reached) / len(reached) if reached else None,
            revenue=float(zl.amount.sum()),
            revenue_per_reached=float(zl[zl.track_id.isin(reached)].amount.sum() / len(reached)) if reached else None,
            spend_stoppers=None if sp_in is None or sp_in != sp_in else float(sp_in),
            spend_others=None if sp_out is None or sp_out != sp_out else float(sp_out)))
    return rows


def _tracking_miss(R, shop_ids, matched_lines, party_of=None):
    """Items bought from a zone by a shopper the tracker never saw in it. A data-quality check:
    zone edges drawn too tight, a camera blind spot, or items that sit in more than one place."""
    pres = R["presence"]
    party_of = party_of or {}
    pid = lambda t: party_of.get(int(t), int(t))
    seen = {(pid(t), z) for t, z in zip(pres.track_id, pres.zone)}                  # seen if any member of the party was there
    ml = matched_lines[matched_lines.track_id.isin(shop_ids) & matched_lines.zone.isin(
        [n for n, z in R["zones"].items() if z[4] == "merchandise"])]
    if ml.empty:
        return dict(lines=0, not_seen=0, rate=None)
    miss = sum((pid(t), z) not in seen for t, z in zip(ml.track_id, ml.zone))
    return dict(lines=int(len(ml)), not_seen=int(miss), rate=miss / len(ml))


# ---------------------------------------------------------------- plain-language flags
def pos_flags(P):
    out = []
    S, kpi = P["match"], P["kpi"]
    if not P["has_checkout"]:
        return [dict(kind="Data check", text="This layout has no checkout zone, so sales cannot be tied to shoppers. "
                     "Draw the register area in the zone editor and set its type to checkout.")]
    if S["n_txn"] and S["match_rate"] < POS_MIN_MATCH_RATE:
        why = ("Check the POS and camera clocks" if S["n_unmatched_txn"] > S["n_ambiguous"]
               else "Several shoppers finished paying at nearly the same moment, which is common with more than one till")
        out.append(dict(kind="Data check", text=(
            f"Only {S['match_rate']:.0%} of {S['n_txn']} sales could be tied to a shopper with confidence "
            f"({S['n_ambiguous']} were ambiguous, {S['n_unmatched_txn']} had no one at the register at that time). "
            f"{why}. Revenue figures cover the matched sales only.")))
    miss = P["miss"]
    if miss["rate"] is not None and miss["lines"] >= 30 and miss["rate"] >= 0.25:
        out.append(dict(kind="Data check", text=(
            f"{miss['rate']:.0%} of items were bought by shoppers the tracker never saw in that zone. "
            f"Zone edges may be too tight, a camera may not cover part of the floor, or items may sit in "
            f"more than one place. Fix this before trusting zone-level revenue.")))
    rows = [r for r in P["zones"] if r["stopped"] >= POS_MIN_STOPPERS and r["stop_to_buy"] is not None]
    if len(rows) >= 3:
        med_buy = float(np.median([r["stop_to_buy"] for r in rows]))
        med_stop = float(np.median([r["share_stopped"] for r in P["zones"]]))
        med_reach = float(np.median([r["share_reached"] for r in P["zones"]]))
        for r in rows:
            if med_buy <= 0 or med_buy >= 1:
                break
            ratio = r["stop_to_buy"] / med_buy
            z = (r["stop_to_buy"] - med_buy) / math.sqrt(med_buy * (1 - med_buy) / r["stopped"])
            counts = f"{r['bought_after_stop']} of {r['stopped']} who stopped"
            if ratio < 0.7 and z <= -POS_MIN_Z and r["share_stopped"] >= 0.7 * med_stop:
                out.append(dict(kind="Offer problem", zone=r["zone"], text=(
                    f"{r['zone']}: {r['share_stopped']:.0%} of shoppers stop there (zone median {med_stop:.0%}) but "
                    f"only {r['stop_to_buy']:.0%} of those who stop buy from it ({counts}), against a median of "
                    f"{med_buy:.0%}. They are interested and not buying. Look at price, stock, how easy it is to "
                    f"compare, or whether someone is on hand to help. Worth a look in person: this is a pattern, "
                    f"not a cause.")))
            elif ratio > 1.3 and z >= POS_MIN_Z and r["share_reached"] < 0.7 * med_reach:
                out.append(dict(kind="Hidden earner", zone=r["zone"], text=(
                    f"{r['zone']}: only {r['share_reached']:.0%} of shoppers reach it (zone median {med_reach:.0%}), "
                    f"but {r['stop_to_buy']:.0%} of those who stop buy from it ({counts}), against a median of "
                    f"{med_buy:.0%}. Those who find it want it. Moving it where more people pass could pay off. "
                    f"Test it with the layout experiment.")))
    return out


# ---------------------------------------------------------------- revenue per shopper on the floor
def revenue_map(R, P, w, h, cell=0.5):
    """Average spend of the shoppers who walked through each 0.5 m cell, before they reached the register
    queue. Paths are cut where a shopper joins the queue; otherwise every cell near the till would just show
    "people who are about to pay". Cells with too few shoppers are left blank."""
    df = R["df"]
    shop = P["shop"]
    ids = set(shop.track_id)
    d = df[df.track_id.isin(ids)][["track_id", "t", "x", "y"]]
    cz = [z for z in R["zones"].values() if z[4] == "checkout"]
    if cz:
        z = cz[0]
        q = d[(d.x >= z[0] - QUEUE_MARGIN_M) & (d.x <= z[2]) & (d.y >= z[1]) & (d.y <= z[3] + 0.5)]
        first_q = q.groupby("track_id")["t"].min()
        cut = d.track_id.map(first_q)
        d = d[cut.isna() | (d.t < cut)]
    nx, ny = max(int(round(w / cell)), 1), max(int(round(h / cell)), 1)
    cx = np.clip((d.x / cell).astype(int), 0, nx - 1)
    cy = np.clip((d.y / cell).astype(int), 0, ny - 1)
    cells = pd.DataFrame({"track_id": d.track_id.to_numpy(), "cx": cx.to_numpy(), "cy": cy.to_numpy()}).drop_duplicates()
    spend = shop.set_index("track_id")["spend"]
    cells["spend"] = cells.track_id.map(spend)
    g = cells.groupby(["cy", "cx"]).agg(n=("track_id", "size"), total=("spend", "sum")).reset_index()
    g = g[g.n >= POS_MAP_MIN_SHOPPERS]
    Z = np.full((ny, nx), np.nan)
    Z[g.cy.to_numpy(), g.cx.to_numpy()] = (g.total / g.n).to_numpy()
    xs = (np.arange(nx) + 0.5) * cell
    ys = (np.arange(ny) + 0.5) * cell
    return Z, xs, ys


# ---------------------------------------------------------------- before / after on revenue
def spend_array(R, zone=None):
    """Spend per shopper (zero for shoppers with no matched sale). With `zone`, only items from that zone."""
    P = R.get("pos")
    if not P or P["shop"].empty:
        return None
    shop = P["shop"]
    if zone is None:
        return shop["spend"].to_numpy(float)
    zl = P["lines"][P["lines"].zone == zone].groupby("track_id")["amount"].sum()
    return shop.track_id.map(zl).fillna(0.0).to_numpy(float)


def compare_revenue(Ra, Rb, zone=None, n_boot=1500, seed=0):
    """Revenue per shopper before and after, with a bootstrap range on the relative change. Per shopper, so a
    busier day does not look like a better layout. None when either recording has no sales."""
    a, b = spend_array(Ra, zone), spend_array(Rb, zone)
    if a is None or b is None or len(a) < 2 or len(b) < 2:
        return None
    rng = np.random.default_rng(seed)
    ma, mb = a.mean(), b.mean()
    ra = rng.choice(a, size=(n_boot, len(a))).mean(axis=1)
    rb = rng.choice(b, size=(n_boot, len(b))).mean(axis=1)
    ok = ra > 0
    lifts = rb[ok] / ra[ok] - 1
    lo, hi = np.percentile(lifts, [2.5, 97.5]) if len(lifts) else (np.nan, np.nan)
    cov = lambda R: R["pos"]["match"]["match_rate"]
    return dict(zone=zone, per_a=float(ma), per_b=float(mb), lift=float(mb / ma - 1) if ma else float("nan"),
                ci_low=float(lo), ci_high=float(hi), n_a=int(len(a)), n_b=int(len(b)),
                match_a=float(cov(Ra)), match_b=float(cov(Rb)))
