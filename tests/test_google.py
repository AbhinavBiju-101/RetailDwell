"""Sign-in with Google through Supabase (PKCE). Run:  python -m tests.test_google

The real Google and Supabase are not called. A fake Supabase HTTP session checks the requests this app makes against the
documented shapes: /auth/v1/authorize with a code challenge, and /auth/v1/token?grant_type=pkce with auth_code and
code_verifier. What these tests cannot show is that your Google project and Supabase settings are right.
"""
import base64
import hashlib
import os
import re
import tempfile
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")
from repo_base import AuthError
from repo_local import LocalRepo
from supa import Supabase, SupaError


class R:
    def __init__(self, status=200, data=None):
        self.status_code, self._d, self.ok, self.text = status, data or {}, status < 400, ""

    def json(self):
        return self._d


class FakeHttp:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, params=None, headers=None, timeout=None, json=None):
        self.calls.append(dict(url=url, params=params, headers=headers, json=json))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_the_authorize_url_and_the_code_exchange_match_the_documented_requests():
    http = FakeHttp(R(200, dict(access_token="tok", refresh_token="ref", expires_in=3600, user=dict(id="u1", email="g@x.com"))))
    c = Supabase("https://abc.supabase.co/", "anon", "service", session=http)
    q = parse_qs(urlparse(c.oauth_url("google", "https://app.example/auth/callback", "CHAL")).query)
    assert q == {"provider": ["google"], "redirect_to": ["https://app.example/auth/callback"], "code_challenge": ["CHAL"], "code_challenge_method": ["s256"]}
    assert c.oauth_url("google", "x", "y").startswith("https://abc.supabase.co/auth/v1/authorize?")
    assert c.exchange_pkce("CODE", "VER")["access_token"] == "tok"
    call = http.calls[0]
    assert call["url"] == "https://abc.supabase.co/auth/v1/token" and call["params"] == {"grant_type": "pkce"}
    assert call["json"] == {"auth_code": "CODE", "code_verifier": "VER"} and call["headers"]["apikey"] == "anon"
    try:
        Supabase("https://abc.supabase.co", "a", "s", session=FakeHttp(R(400, dict(msg="invalid request: both auth code and code verifier should be non-empty")))).exchange_pkce("", "")
        raise AssertionError("accepted")
    except SupaError as e:
        assert "non-empty" in e.message


class OAuthRepo(LocalRepo):
    """A local repo that behaves like the Supabase one for sign-in with Google."""
    supports_oauth = True

    def __init__(self, root):
        super().__init__(root)
        self.started, self.finished, self.fail = [], [], False

    def oauth_start(self, provider, redirect_to, code_challenge):
        self.started.append((provider, redirect_to, code_challenge))
        return f"https://abc.supabase.co/auth/v1/authorize?provider={provider}"

    def oauth_finish(self, code, code_verifier):
        self.finished.append((code, code_verifier))
        if self.fail:
            raise AuthError("bad")
        return self.sign_in("g@t.example", "password-1")


def _client(env):
    for k, v in env.items():
        os.environ[k] = v
    from app import create_app
    repo = OAuthRepo(tempfile.mkdtemp())
    repo.sign_up("g@t.example", "password-1", "G")
    return create_app(repo=repo).test_client(), repo


def _clear():
    for k in ("GOOGLE_SIGNIN", "APP_URL"):
        os.environ.pop(k, None)


def test_the_button_and_routes_exist_only_when_switched_on_and_supported():
    _clear()
    c, _ = _client({})
    assert "Continue with Google" not in c.get("/login").data.decode() and c.get("/auth/google").status_code == 404
    assert c.get("/auth/callback?code=x").status_code == 404
    _clear()
    os.environ["GOOGLE_SIGNIN"] = "1"
    from app import create_app
    plain = create_app(repo=LocalRepo(tempfile.mkdtemp())).test_client()          # a local repo cannot do OAuth
    assert "Continue with Google" not in plain.get("/login").data.decode() and plain.get("/auth/google").status_code == 404
    c, _ = _client({"GOOGLE_SIGNIN": "1"})
    assert "Continue with Google" in c.get("/login").data.decode()
    _clear()


def test_the_flow_signs_in_once_and_binds_the_code_to_the_browser_that_started_it():
    _clear()
    c, repo = _client({"GOOGLE_SIGNIN": "1", "APP_URL": "https://app.example/"})
    r = c.get("/auth/google?next=/app")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://abc.supabase.co/auth/v1/authorize")
    provider, redirect_to, challenge = repo.started[0]
    assert provider == "google" and redirect_to == "https://app.example/auth/callback"
    r = c.get("/auth/callback?code=AUTHCODE")
    code, verifier = repo.finished[0]
    assert code == "AUTHCODE" and len(verifier) >= 43
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == challenge   # S256 of the verifier
    assert r.status_code == 302 and r.headers["Location"].endswith("/app") and c.get("/app").status_code == 200
    # the verifier is single-use
    c2 = c.application.test_client()
    assert c2.get("/auth/callback?code=AGAIN").status_code == 302 and len(repo.finished) == 1
    _clear()


def test_a_callback_that_this_browser_did_not_start_is_refused():
    _clear()
    c, repo = _client({"GOOGLE_SIGNIN": "1"})
    r = c.get("/auth/callback?code=ATTACKERS")                                     # no session verifier: login CSRF attempt
    assert r.status_code == 302 and "/login" in r.headers["Location"] and not repo.finished
    assert "expired or was not started here" in c.get("/login").data.decode()
    _clear()


def test_cancelled_and_failed_sign_ins_explain_and_do_not_log_in():
    _clear()
    c, repo = _client({"GOOGLE_SIGNIN": "1"})
    c.get("/auth/google")
    assert "/login" in c.get("/auth/callback?error=access_denied").headers["Location"] and not repo.finished
    assert "cancelled or did not complete" in c.get("/login").data.decode()
    c.get("/auth/google")
    repo.fail = True
    assert "/login" in c.get("/auth/callback?code=X").headers["Location"]
    assert "could not be completed" in c.get("/login").data.decode() and c.get("/app").status_code in (302, 401)
    _clear()


def test_an_open_redirect_through_next_is_not_possible():
    _clear()
    c, repo = _client({"GOOGLE_SIGNIN": "1"})
    c.get("/auth/google?next=https://evil.example/")
    r = c.get("/auth/callback?code=X")
    assert "evil.example" not in r.headers["Location"]
    _clear()


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
