"""Store-manager brief and the Gemini fallbacks. Run:  python -m tests.test_brief

No test here calls the real Gemini API. Responses are built in the shapes the documentation shows
(generateContent: candidates / finishReason / usageMetadata; Interactions: status / steps / usage), so a change in
Google's wire format is the thing these tests cannot catch. Failures are covered by status code and body.
"""
import json
import os
import re
import tempfile
from datetime import date, timedelta

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
import brief as B
from tests.test_pos import _analyse, _csrf, _login

ENV = dict(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-3.8-flash")


class Resp:
    def __init__(self, status=200, data=None, raw=False):
        self.status_code, self._data, self._raw = status, data, raw

    def json(self):
        if self._raw:
            raise ValueError("not json")
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(dict(url=url, json=json, headers=headers, timeout=timeout))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


_FACTS = {}


def facts():
    if not _FACTS:
        from app import findings
        R, _ = _analyse("lakeside-grocery", "B")
        R["clock"] = None
        _FACTS["f"] = B.build_facts(dict(name="Lakeside", kind="Grocery"), "After: offers", R, findings(R), wait=dict(median=2.1, peak=4.4))
    return json_copy(_FACTS["f"])


def json_copy(x):
    return json.loads(json.dumps(x))


def gen(text, finish="STOP", out=120):
    return Resp(200, dict(candidates=[dict(content=dict(parts=[dict(text=text)]), finishReason=finish)], usageMetadata=dict(candidatesTokenCount=out)))


def good_text(f):
    z = f["zones"][0]
    return (f"## What happened\n- {f['visits']} visits and {f['shoppers']} shoppers; {z['zone']} reached {z['reached_pct']}% of shoppers.\n"
            f"## What to look at\n- Check {f['zones'][-1]['zone']}, the least reached zone.\n## Try next\n- Compare one layout change on the Experiment page.\n")


def svc(*responses, env=None, today=date.today):
    return B.BriefService(session=FakeSession(*responses), sleep=lambda s: None, env=dict(ENV, **(env or {})), today=today)


# ------------------------------------------------------------------ facts and the template
def test_facts_hold_aggregates_only():
    f = facts()
    blob = json.dumps(f)
    for banned in ("track_id", "txn_id", "\"x\"", "\"y\"", "mission"):
        assert banned not in blob, banned
    assert f["visits"] > 0 and f["zones"] and "findings" in f and B.estimate_tokens(f) < 3000


def test_the_template_brief_works_with_no_network_and_says_what_it_found():
    t = B.template_brief(facts())
    titles = [s["title"] for s in t["sections"]]
    assert titles == ["What happened", "What to look at", "Try next"] and all(s["items"] for s in t["sections"])
    assert str(facts()["visits"]) in t["sections"][0]["items"][0]


def test_no_key_means_template_with_a_note_and_no_call():
    s = svc(Resp(200, {}), env=dict(GEMINI_API_KEY=""))
    r = s.write("store1", facts())
    assert r["source"] == "template" and r["warning"] is None and "GEMINI_API_KEY" in r["note"] and not s.session.calls


def test_demo_stores_do_not_call_gemini_unless_enabled():
    s = svc(gen("x"))
    r = s.write("demo", facts(), is_demo=True)
    assert r["source"] == "template" and "demo" in r["note"] and not s.session.calls
    s2 = svc(gen(good_text(facts())), env=dict(BRIEF_AI_FOR_DEMO="1"))
    assert s2.write("demo", facts(), is_demo=True)["source"] == "gemini"


# ------------------------------------------------------------------ Gemini success paths
def test_a_good_answer_is_used_sent_with_the_key_in_a_header_and_cached():
    f = facts()
    s = svc(gen(good_text(f)))
    r = s.write("s1", f)
    assert r["source"] == "gemini" and r["model"] == "gemini-3.8-flash" and r["warning"] is None and r["tokens_out"] == 120
    assert [x["title"] for x in r["sections"]] == ["What happened", "What to look at", "Try next"]
    call = s.session.calls[0]
    assert call["url"].endswith("/models/gemini-3.8-flash:generateContent") and call["headers"]["x-goog-api-key"] == "test-key"
    assert "key=" not in call["url"]                                                   # the key never goes in the URL
    cfgs = call["json"]["generationConfig"]
    assert cfgs["maxOutputTokens"] == 2048 and "Facts (JSON)" in call["json"]["contents"][0]["parts"][0]["text"]
    assert "use only facts" in call["json"]["systemInstruction"]["parts"][0]["text"]
    assert s.write("s1", f)["source"] == "gemini" and len(s.session.calls) == 1        # same facts: no second call
    assert s.cached("s1", f)["source"] == "gemini" and s.cached("other-store", f) is None
    s.write("s1", f, force=True)
    assert len(s.session.calls) == 2                                                   # asking again does call


def test_the_interactions_api_shape_is_understood_and_nothing_is_stored_server_side():
    f = facts()
    data = dict(status="completed", usage=dict(total_output_tokens=90),
                steps=[dict(type="thought", signature="x"), dict(type="model_output", content=[dict(type="text", text=good_text(f))])])
    s = svc(Resp(200, data), env=dict(GEMINI_API="interactions"))
    r = s.write("s1", f)
    call = s.session.calls[0]
    assert r["source"] == "gemini" and r["tokens_out"] == 90
    assert call["url"].endswith("/v1beta/interactions") and call["json"]["store"] is False and call["json"]["model"] == "gemini-3.8-flash"
    unfinished = svc(Resp(200, dict(status="in_progress", steps=[])), env=dict(GEMINI_API="interactions")).write("s1", f)
    assert unfinished["source"] == "template" and "did not finish" in unfinished["warning"]


# ------------------------------------------------------------------ every way Gemini can fail
def _fails(resp, phrase, calls=1, env=None):
    s = svc(*([resp] if not isinstance(resp, list) else resp), env=env)
    r = s.write("s1", facts())
    assert r["source"] == "template" and phrase in r["warning"] and "standard summary" in r["warning"], r["warning"]
    assert len(s.session.calls) == calls, len(s.session.calls)
    assert s.cached("s1", facts()) is None                                              # failures are not remembered
    return r


def test_quota_and_rate_limits_fall_back_with_a_clear_warning_and_are_not_retried():
    _fails(Resp(429, dict(error=dict(code=429, status="RESOURCE_EXHAUSTED", message="You exceeded your current quota"))), "rate or quota limit")


def test_a_rejected_key_a_forbidden_key_and_an_unknown_model_say_which():
    _fails(Resp(400, dict(error=dict(code=400, status="INVALID_ARGUMENT", message="API key not valid. Please pass a valid API key."))), "rejected the API key")
    _fails(Resp(403, dict(error=dict(code=403, message="denied"))), "refused the request")
    _fails(Resp(404, dict(error=dict(code=404, message="models/nope is not found"))), "does not know the model 'gemini-3.8-flash'")
    _fails(Resp(400, dict(error=dict(code=400, message="Invalid JSON payload"))), "invalid")


def test_a_server_error_is_retried_once_then_reported_and_a_network_failure_too():
    _fails([Resp(503, dict(error=dict(code=503, message="overloaded")))], "problem on its side", calls=2)
    _fails([ConnectionError("down")], "could not be reached", calls=2)
    s = svc(Resp(503, {}), gen(good_text(facts())))                                      # recovers on the retry
    assert s.write("s1", facts())["source"] == "gemini" and len(s.session.calls) == 2


def test_a_non_json_reply_is_handled():
    _fails(Resp(502, None, raw=True), "problem on its side", calls=2)
    _fails(Resp(200, None, raw=True), "problem on its side", calls=1)          # a 200 that is not JSON is not retried


def test_token_limits_cut_off_blocked_and_empty_answers_fall_back():
    _fails(gen("## What happened\n- cut", finish="MAX_TOKENS"), "length limit")
    _fails(gen("blocked", finish="SAFETY"), "stopped before finishing (SAFETY)")
    _fails(Resp(200, dict(promptFeedback=dict(blockReason="OTHER"), candidates=[])), "declined to write")
    _fails(Resp(200, dict(candidates=[])), "no answer")
    _fails(gen("   "), "empty answer")
    plain = svc(gen("Visits were steady and nothing stood out.")).write("s1", facts())
    assert plain["source"] == "gemini" and plain["sections"][0]["title"] == "Summary"   # unstructured text is kept, not thrown away


def test_thinking_parts_are_ignored_when_reading_the_answer():
    f = facts()
    data = dict(candidates=[dict(content=dict(parts=[dict(text="I should look at the numbers", thought=True), dict(text=good_text(f))]), finishReason="STOP")])
    r = svc(Resp(200, data)).write("s1", f)
    assert r["source"] == "gemini" and "I should look" not in json.dumps(r["sections"])


def test_the_model_cannot_slip_in_figures_that_are_not_in_the_data():
    f = facts()
    invented = ("## What happened\n- Sales rose 47% to 9,876 over last year, with 83% repeat customers.\n"
                "## What to look at\n- x\n## Try next\n- y\n")
    r = _fails(gen(invented), "figures that are not in your data")
    assert "47" in r["warning"]
    z = f["zones"][0]
    near = f"## What happened\n- About {z['reached_pct'] + 0.3:.1f}% reached {z['zone']} across {f['visits']} visits.\n## What to look at\n- ok\n## Try next\n- ok\n"
    assert svc(gen(near)).write("s1", f)["source"] == "gemini"                           # rounding is fine
    assert B.unknown_numbers("3 zones and 10 people", f) == [] and B.unknown_numbers("2026", f) == ["2026"]


# ------------------------------------------------------------------ limits
def test_too_large_a_summary_is_trimmed_then_refused_without_calling_gemini():
    f = facts()
    f["findings"] = [dict(kind="Flag", text="word " * 400) for _ in range(8)]
    f["zones"] = f["zones"] * 6
    assert B.estimate_tokens(f) > 1500
    s = svc(gen(good_text(facts())), env=dict(BRIEF_MAX_INPUT_TOKENS="1500"))
    shrunk = B.shrink(f, 5000)
    assert B.estimate_tokens(shrunk) <= 5000 and len(shrunk["findings"]) <= 5 and len(shrunk["zones"]) <= 8
    r = s.write("s1", f)
    assert r["source"] == "template" and "too large" in r["warning"] and not s.session.calls
    tiny = svc(gen("x"), env=dict(BRIEF_MAX_INPUT_TOKENS="200")).write("s1", facts())
    assert "too large" in tiny["warning"]


def test_the_daily_limit_stops_calls_and_resets_the_next_day():
    day = [date(2026, 10, 11)]
    s = svc(gen(good_text(facts())), env=dict(BRIEF_DAILY_LIMIT="2"), today=lambda: day[0])
    f = facts()
    assert s.remaining("s1") == 2
    s.write("s1", f, force=True)
    s.write("s1", f, force=True)
    assert s.remaining("s1") == 0 and len(s.session.calls) == 2
    r = s.write("s1", f, force=True)
    assert r["source"] == "template" and "2 Gemini summaries for today" in r["warning"] and len(s.session.calls) == 2
    assert s.remaining("other") == 2                                                     # per store
    day[0] += timedelta(days=1)
    assert s.remaining("s1") == 2 and s.write("s1", f, force=True)["source"] == "gemini"


def test_failed_calls_still_count_against_the_daily_limit():
    s = svc(Resp(429, {}), env=dict(BRIEF_DAILY_LIMIT="3"))
    s.write("s1", facts())
    assert s.remaining("s1") == 2


def test_parsing_tolerates_bold_bullets_and_stray_text():
    secs = B.parse_sections("Intro line\n## What happened:\n* **Visits** were steady\n\u2022 second\n## Try next\n- one")
    assert secs[0]["title"] == "Summary" and secs[1]["items"] == ["Visits were steady", "second"] and secs[2]["items"] == ["one"]
    try:
        B.parse_sections("\n\n")
        raise AssertionError("accepted nothing")
    except B.BriefUnavailable:
        pass


# ------------------------------------------------------------------ the pages
def _app(service, spec_store="lakeside-grocery"):
    from app import create_app
    from repo_local import LocalRepo
    import simulate
    from stores import STORES
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    spec = STORES[spec_store]
    st = repo.create_store(sess, dict(name="Lakeside", width_m=spec["w"], height_m=spec["h"], window_s=300, hero_zone=spec["hero"]))
    for lk, (df, lines, _t) in simulate.generate_with_sales(spec_store).items():
        lay = repo.upsert_layout(sess, st["id"], lk, spec["layouts"][lk]["label"], {n: list(z) for n, z in spec["layouts"][lk]["zones"].items()})
        ds = repo.create_dataset(sess, lay["id"], "Day", "upload")
        repo.save_tracks(ds, st, df[["track_id", "t", "x", "y"]])
        repo.save_pos(ds, st, lines)
    c = create_app(repo=repo, brief_service=service).test_client()
    _login(c, "o@t.example", "password-1")
    return c, f"/app/s/{st['id']}"


def test_brief_page_shows_the_template_then_gemini_then_a_warning_when_it_fails():
    f = {}

    class Learn(FakeSession):                                    # answers with text built from whatever facts it is sent
        def post(self, url, json=None, headers=None, timeout=None):
            sent = __import__("json").loads(json["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
            return gen(good_text(sent))
    s = B.BriefService(session=Learn(), sleep=lambda x: None, env=ENV)
    c, base = _app(s)
    page = c.get(base + "/brief").data.decode()
    assert "standard summary" in page and "What happened" in page and "Write it with Gemini" in page and "left today" in page
    assert "Gemini receives only the summary figures" in page and "What this is based on" in page
    tok = _csrf(c, base + "/brief")
    assert c.post(base + "/brief/write", data=dict(csrf=tok, layout="A")).status_code == 302
    page = c.get(base + "/brief?layout=A").data.decode()
    assert "Written by Gemini (gemini-3.8-flash)" in page and "Write it again with Gemini" in page
    # the same figures give the same brief without another call, and a different layout does not inherit it
    other = c.get(base + "/brief?layout=B").data.decode()
    assert "standard summary" in other

    failing = B.BriefService(session=FakeSession(Resp(429, dict(error=dict(status="RESOURCE_EXHAUSTED", message="quota")))), sleep=lambda x: None, env=ENV)
    c2, base2 = _app(failing)
    tok = _csrf(c2, base2 + "/brief")
    c2.post(base2 + "/brief/write", data=dict(csrf=tok, layout="A"))
    page = c2.get(base2 + "/brief?layout=A").data.decode()
    assert "rate or quota limit was reached" in page and "role=\"alert\"" in page and "What happened" in page
    assert "role=\"alert\"" not in c2.get(base2 + "/brief?layout=A").data.decode()          # the warning is shown once


def test_brief_page_without_a_key_has_no_button_and_says_how_to_enable_it():
    s = B.BriefService(session=FakeSession(Resp(200, {})), env=dict(GEMINI_API_KEY=""))
    c, base = _app(s)
    page = c.get(base + "/brief").data.decode()
    assert "Write it with Gemini" not in page and "GEMINI_API_KEY" in page and "What happened" in page
    assert not s.session.calls


def test_brief_includes_the_experiment_and_revenue_figures_in_its_facts():
    s = B.BriefService(session=FakeSession(Resp(200, {})), env=ENV)
    c, base = _app(s)
    page = c.get(base + "/brief?layout=B").data.decode()
    assert "before and after result for Weekly offers is included" in page and "Revenue page" in page


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
