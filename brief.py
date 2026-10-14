"""Store-manager brief: a short written summary of one layout's figures.

Two writers share one set of facts:
  * a template that always works and never leaves the server, and
  * Gemini, used only when GEMINI_API_KEY is set and a person asks for it.

What goes to Gemini is the facts dictionary and nothing else: counts, percentages, zone names and the app's own
findings. No tracks, no sales lines, no identities. If Gemini cannot be used for any reason (no key, quota, a rejected
key, an unknown model, a blocked or cut-off answer, a prompt that is too large, the daily limit, or text that
contains figures not in the data) the template brief is shown with a warning that says why.

Settings (environment):
  GEMINI_API_KEY            required to use Gemini
  GEMINI_MODEL              default gemini-3.8-flash
  GEMINI_API                generate_content (default) or interactions
  BRIEF_MAX_INPUT_TOKENS    refuse to send more than this (default 6000, estimated)
  BRIEF_MAX_OUTPUT_TOKENS   ask Gemini for at most this many (default 2048; it includes any thinking)
  BRIEF_DAILY_LIMIT         Gemini calls per store per day (default 10, counted per server process)
  BRIEF_AI_FOR_DEMO         set to 1 to let the demo stores call Gemini (off by default)
"""
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timezone

DEFAULT_MODEL = "gemini-3.8-flash"
GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

SYSTEM = (
    "You write a short brief for a store manager from the JSON facts you are given. Rules: use only facts that are in "
    "the JSON, and quote their numbers exactly as given; do not calculate new numbers, estimate, or add anything that "
    "is not there. Describe patterns as patterns, never as proven causes. If there are findings of kind 'Data check', "
    "say plainly that they limit how far the figures can be trusted. Plain words, no jargon, no marketing tone. "
    "At most 200 words. Use exactly three sections, each starting with a line '## ' followed by the title: "
    "'What happened', 'What to look at', 'Try next'. Under each, 1 to 3 lines starting with '- '. "
    "Keep currency symbols and zone names as given.")


class BriefUnavailable(Exception):
    """Gemini could not be used. The message is written for the person reading the page."""


class TooLarge(BriefUnavailable):
    pass


def config(env=None):
    e = os.environ if env is None else env
    num = lambda k, d: int(e.get(k) or d)
    return dict(key=(e.get("GEMINI_API_KEY") or "").strip(), model=(e.get("GEMINI_MODEL") or DEFAULT_MODEL).strip(),
                api=(e.get("GEMINI_API") or "generate_content").strip().lower(), timeout_s=25.0,
                max_input_tokens=num("BRIEF_MAX_INPUT_TOKENS", 6000), max_output_tokens=num("BRIEF_MAX_OUTPUT_TOKENS", 2048),
                daily_limit=num("BRIEF_DAILY_LIMIT", 10), ai_for_demo=(e.get("BRIEF_AI_FOR_DEMO") or "") == "1")


# ---------------------------------------------------------------- facts
def _r(v, d=0):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), d) if d else int(round(float(v)))


def build_facts(store, layout_label, R, items, wait=None, experiment=None, currency="\u20b9"):
    """The aggregate figures for one layout, as plain numbers and text. Nothing here identifies a person."""
    st, lab = R["stats"], R["labels"]
    merch = st[st.type == "merchandise"].sort_values("visit_share", ascending=False)
    clk = R.get("clock")
    facts = dict(
        store=dict(name=store.get("name"), kind=store.get("kind")), layout=layout_label, currency=currency,
        recording=dict(started=clk.start_label() if clk and clk.known else None,
                       length_min=_r(R["df"]["t"].max() / 60) if len(R["df"]) else 0),
        visits=int(st.attrs["n_visits"]), shoppers=int(st.attrs["n_shoppers"]),
        average_visit_min=_r(lab.visit_s.mean() / 60, 1), loiterers_and_waiters_pct=_r((lab.mission == "loiterer / waiter").mean() * 100),
        staff_tracks_left_out=len(R.get("staff") or {}),
        parties=dict(count=sum(1 for v in (R.get("parties") or {}).get("sizes", {}).values() if v > 1)),
        zones=[dict(zone=r.zone, reached_pct=_r(r.visit_share * 100), of_those_who_reach_stop_pct=_r(r.stop_rate * 100),
                    average_stop_seconds=_r(r.engaged_dwell_s)) for r in merch.itertuples()],
        checkout_wait_min=dict(median=_r(wait["median"], 1), peak=_r(wait["peak"], 1)) if wait else None,
        findings=[dict(kind=i["kind"], text=i["text"]) for i in items[:8]])
    P = R.get("pos")
    if P:
        k, S = P["kpi"], P["match"]
        facts["revenue"] = dict(
            revenue_per_shopper=_r(k["revenue_per_shopper"]), shoppers_with_a_matched_sale_pct=_r((k["conversion"] or 0) * 100),
            average_basket=_r(k["avg_basket"]), sales=S["n_txn"], sales_matched_pct=_r(S["match_rate"] * 100),
            zones=[dict(zone=z["zone"], of_those_who_stop_buy_pct=_r((z["stop_to_buy"] or 0) * 100), revenue=_r(z["revenue"]))
                   for z in P["zones"] if z["stopped"] >= 15])
    if experiment:
        facts["layout_experiment"] = experiment
    return facts


def estimate_tokens(obj):
    return int(len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))) / 3.2) + 350      # 350 for the instructions


def shrink(facts, budget):
    """Trim the least important detail until the facts fit the token budget. Raises TooLarge if they cannot."""
    f = json.loads(json.dumps(facts))
    for step in (lambda d: d.update(zones=d["zones"][:8]), lambda d: d.update(findings=d["findings"][:5]),
                 lambda d: d.get("revenue") and d["revenue"].update(zones=d["revenue"]["zones"][:5]),
                 lambda d: d.update(zones=d["zones"][:5], findings=d["findings"][:3])):
        if estimate_tokens(f) <= budget:
            break
        step(f)
    if estimate_tokens(f) > budget:
        raise TooLarge(f"The summary was too large to send (about {estimate_tokens(f):,} tokens against a limit of {budget:,}).")
    return f


# ---------------------------------------------------------------- the template writer
def template_brief(facts):
    """Deterministic summary of the facts. Always available."""
    cur = facts.get("currency", "")
    z = facts["zones"]
    happened = [f"{facts['layout']}: {facts['visits']} visits, {facts['shoppers']} of them shoppers, "
                f"an average visit of {facts['average_visit_min']} minutes."]
    if facts["recording"].get("started"):
        happened[0] += f" Recording started {facts['recording']['started']}."
    if z:
        happened.append(f"{z[0]['zone']} reaches the most shoppers ({z[0]['reached_pct']}%); {z[-1]['zone']} the fewest ({z[-1]['reached_pct']}%).")
    w = facts.get("checkout_wait_min")
    if w:
        happened.append(f"Estimated checkout wait: {w['median']} minutes typically, {w['peak']} at the worst point.")
    rv = facts.get("revenue")
    if rv:
        happened.append(f"Revenue per shopper was {cur}{rv['revenue_per_shopper']:,}, with a matched sale for {rv['shoppers_with_a_matched_sale_pct']}% "
                        f"of shoppers and an average basket of {cur}{rv['average_basket']:,}. {rv['sales_matched_pct']}% of {rv['sales']} sales were tied to a shopper.")
    if facts.get("staff_tracks_left_out") or facts["parties"]["count"]:
        bits = []
        if facts["staff_tracks_left_out"]:
            bits.append(f"{facts['staff_tracks_left_out']} staff track(s) were left out")
        if facts["parties"]["count"]:
            bits.append(f"{facts['parties']['count']} groups who shopped together were found")
        happened.append(" and ".join(bits).capitalize() + ".")
    order = {"Offer problem": 0, "Data check": 1, "Bottleneck": 2, "Hidden earner": 3}
    fs = sorted(facts["findings"], key=lambda f: order.get(f["kind"], 4))
    look = [f["text"] for f in fs[:3]] or ["Nothing unusual stands out against the store's own averages."]
    nxt = []
    kinds = {f["kind"] for f in facts["findings"]}
    if "Data check" in kinds:
        nxt.append("Fix the data issue flagged above first (zone edges, camera coverage or clocks), because it limits what the other figures can say.")
    if "Offer problem" in kinds:
        zone = next(f.get("zone") or "" for f in facts["findings"] if f["kind"] == "Offer problem") if any(f["kind"] == "Offer problem" for f in facts["findings"]) else ""
        nxt.append("Look at price, stock and staffing where shoppers stop but do not buy, and check it in person" + (f" ({zone})." if zone else "."))
    if "Hidden earner" in kinds:
        nxt.append("Try moving the zone that converts well but is rarely reached to where more people pass, then compare with the Experiment page.")
    if "Bottleneck" in kinds:
        nxt.append("Plan extra checkout cover for the busiest windows.")
    if facts.get("layout_experiment"):
        nxt.append("Keep or reverse the layout change according to the experiment result, and judge it on the range, not only the average.")
    if not nxt:
        nxt.append("Pick one zone to change, record another day, and compare it on the Experiment page.")
    return dict(sections=[dict(title="What happened", items=happened), dict(title="What to look at", items=look),
                          dict(title="Try next", items=nxt[:3])])


# ---------------------------------------------------------------- guarding the model's text
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _numbers(obj, out):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        out.update(float(m.replace(",", "")) for m in _NUM.findall(obj) if m.replace(",", "").replace(".", "", 1).isdigit())
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, out)


def unknown_numbers(text, facts):
    """Figures in the text that are not in the facts (small whole numbers and rounding are allowed)."""
    allowed = set()
    _numbers(facts, allowed)
    bad = []
    for m in _NUM.findall(text):
        try:
            v = float(m.replace(",", ""))
        except ValueError:
            continue
        if v <= 10 and v == int(v):
            continue
        if not any(abs(v - a) <= max(0.51, 0.01 * abs(a)) for a in allowed):
            bad.append(m)
    return bad


def parse_sections(text):
    """'## Title' lines and '- ' bullets into sections. Anything outside that shape is kept as a plain line."""
    sections, cur = [], None
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            cur = dict(title=line.lstrip("# ").strip().rstrip(":"), items=[])
            sections.append(cur)
            continue
        if cur is None:
            cur = dict(title="Summary", items=[])
            sections.append(cur)
        cur["items"].append(re.sub(r"^[-*\u2022]\s*", "", line).replace("**", "").strip())
    sections = [s for s in sections if s["items"]]
    if not sections:
        raise BriefUnavailable("Gemini's answer could not be read as a brief.")
    return sections[:4]


# ---------------------------------------------------------------- Gemini
def _error_message(status, body, cfg):
    try:
        err = (body or {}).get("error") or {}
    except AttributeError:
        err = {}
    msg, st = str(err.get("message") or "").lower(), str(err.get("status") or "")
    if status == 429 or st == "RESOURCE_EXHAUSTED":
        return "Gemini's rate or quota limit was reached. Try again later, or check the plan and quota for this key."
    if status in (400, 401) and ("api key" in msg or st in ("UNAUTHENTICATED",)):
        return "Gemini rejected the API key. Check GEMINI_API_KEY."
    if status == 403:
        return "Gemini refused the request (403). Check that the key is allowed to use this model and region."
    if status == 404:
        return f"Gemini does not know the model '{cfg['model']}'. Set GEMINI_MODEL to one your key can use."
    if status == 400:
        return "Gemini rejected the request as invalid, which may mean the model name or API setting is wrong (GEMINI_MODEL, GEMINI_API)."
    return f"Gemini had a problem on its side ({status}). Try again in a few minutes."


def _parse_generate(data):
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise BriefUnavailable(f"Gemini declined to write this summary ({block}).")
    cands = data.get("candidates") or []
    if not cands:
        raise BriefUnavailable("Gemini returned no answer.")
    c = cands[0]
    fr = c.get("finishReason")
    if fr == "MAX_TOKENS":
        raise BriefUnavailable("Gemini's answer hit the length limit and was cut off.")
    if fr and fr not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        raise BriefUnavailable(f"Gemini stopped before finishing ({fr}).")
    text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
    return text, (data.get("usageMetadata") or {}).get("candidatesTokenCount")


def _parse_interactions(data):
    status = data.get("status")
    if status and status != "completed":
        raise BriefUnavailable(f"Gemini did not finish the answer (status: {status}).")
    text = "".join(c.get("text", "") for s in data.get("steps", []) if s.get("type") == "model_output"
                   for c in s.get("content", []) if c.get("type") == "text")
    return text, (data.get("usage") or {}).get("total_output_tokens")


def ask_gemini(facts, cfg, session=None, sleep=time.sleep):
    """One request to Gemini. Returns (text, output_tokens) or raises BriefUnavailable."""
    if session is None:
        import requests
        session = requests
    prompt = "Facts (JSON):\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    headers = {"x-goog-api-key": cfg["key"], "Content-Type": "application/json"}
    if cfg["api"] == "interactions":
        url = INTERACTIONS_URL
        body = {"model": cfg["model"], "input": SYSTEM + "\n\n" + prompt, "store": False}
        parse = _parse_interactions
    else:
        url = GENERATE_URL.format(model=cfg["model"])
        body = {"systemInstruction": {"parts": [{"text": SYSTEM}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": cfg["max_output_tokens"], "temperature": 0.3}}
        parse = _parse_generate
    last = None
    for attempt in range(2):                              # one retry, only for a server-side or network problem
        try:
            r = session.post(url, json=body, headers=headers, timeout=cfg["timeout_s"])
        except Exception:
            last = "Gemini could not be reached (network error or timeout)."
        else:
            try:
                data = r.json()
            except ValueError:
                data = None
            if r.status_code == 200 and isinstance(data, dict):
                return parse(data)
            if r.status_code >= 500 and attempt == 0:
                last = _error_message(r.status_code, data, cfg)
            else:
                raise BriefUnavailable(_error_message(r.status_code, data, cfg))
        if attempt == 0:
            sleep(1.0)
    raise BriefUnavailable(last or "Gemini could not be reached.")


class BriefService:
    """Writes briefs, remembers the Gemini ones, and keeps count of calls so quota is not burned by accident."""

    def __init__(self, session=None, sleep=time.sleep, today=date.today, env=None):
        self.session, self.sleep, self.today, self.env = session, sleep, today, env
        self.cache, self.used, self.lock = {}, {}, threading.Lock()

    def cfg(self):
        return config(self.env)

    def available(self):
        return bool(self.cfg()["key"])

    def remaining(self, store_id):
        with self.lock:
            return max(0, self.cfg()["daily_limit"] - self.used.get((store_id, self.today()), 0))

    @staticmethod
    def _key(facts, cfg):
        return hashlib.sha256((json.dumps(facts, sort_keys=True, default=str) + cfg["model"] + cfg["api"]).encode()).hexdigest()

    def cached(self, store_id, facts):
        cfg = self.cfg()
        return self.cache.get((store_id, self._key(facts, cfg)))

    def template(self, facts, note=None, warning=None):
        t = template_brief(facts)
        return dict(sections=t["sections"], source="template", model=None, warning=warning, note=note,
                    generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def write(self, store_id, facts, force=False, is_demo=False):
        """The brief for these facts: Gemini's when it can be had, otherwise the template with a warning."""
        cfg = self.cfg()
        if not cfg["key"]:
            return self.template(facts, note="Set GEMINI_API_KEY on the server to have Gemini write this summary.")
        if is_demo and not cfg["ai_for_demo"]:
            return self.template(facts, note="Gemini writing is off for the demo stores. Use your own store, or set BRIEF_AI_FOR_DEMO=1.")
        key = (store_id, self._key(facts, cfg))
        if not force and key in self.cache:
            return self.cache[key]
        try:
            sent = shrink(facts, cfg["max_input_tokens"])
            with self.lock:
                n = self.used.get((store_id, self.today()), 0)
                if n >= cfg["daily_limit"]:
                    raise BriefUnavailable(f"This store has used its {cfg['daily_limit']} Gemini summaries for today. The limit resets tomorrow.")
                self.used[(store_id, self.today())] = n + 1
            text, out_tokens = ask_gemini(sent, cfg, self.session, self.sleep)
            if not text.strip():
                raise BriefUnavailable("Gemini returned an empty answer.")
            bad = unknown_numbers(text, sent)
            if len(bad) > 2:
                raise BriefUnavailable("Gemini's text included figures that are not in your data (" + ", ".join(bad[:3]) + "), so it was not used.")
            sections = parse_sections(text)
        except BriefUnavailable as e:
            return self.template(facts, warning=f"{e} Showing the standard summary instead.")
        out = dict(sections=sections, source="gemini", model=cfg["model"], warning=None, note=None, tokens_out=out_tokens,
                   generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self.lock:
            self.cache[key] = out
        return out
