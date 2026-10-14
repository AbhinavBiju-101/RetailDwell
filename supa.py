"""Thin Supabase client over the documented REST endpoints (GoTrue, PostgREST, Storage).

Uses `requests` only, so there is no extra SDK to install. Two kinds of calls:
  - as the signed-in user: their access token goes in Authorization, so row level security applies
  - as the service: the service-role key, used only on the server for ingest, seeding and files
"""
import requests
from urllib.parse import urlencode

TIMEOUT = 15
PAGE = 1000


class SupaError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _raise(r):
    try:
        body = r.json()
    except ValueError:
        body = {}
    msg = (body.get("msg") or body.get("message") or body.get("error_description")
           or body.get("error") or r.text[:200] or f"HTTP {r.status_code}")
    raise SupaError(r.status_code, msg, body.get("error_code") or body.get("code"))


class Supabase:
    def __init__(self, url, anon_key, service_key, session=None):
        self.url = url.rstrip("/")
        self.anon, self.service = anon_key, service_key
        self.http = session or requests.Session()

    # ---------------------------------------------------------------- auth
    def _auth_headers(self):
        return {"apikey": self.anon, "Content-Type": "application/json"}

    def sign_up(self, email, password, name):
        r = self.http.post(f"{self.url}/auth/v1/signup", headers=self._auth_headers(), timeout=TIMEOUT,
                           json={"email": email, "password": password, "data": {"display_name": name}})
        if not r.ok:
            _raise(r)
        return r.json()

    def sign_in(self, email, password):
        r = self.http.post(f"{self.url}/auth/v1/token", params={"grant_type": "password"},
                           headers=self._auth_headers(), timeout=TIMEOUT,
                           json={"email": email, "password": password})
        if not r.ok:
            _raise(r)
        return r.json()

    def oauth_url(self, provider, redirect_to, code_challenge):
        """Where to send the browser to sign in with an outside provider (PKCE). Supabase returns the person to
        redirect_to with ?code=..., which exchange_pkce turns into a session."""
        q = urlencode({"provider": provider, "redirect_to": redirect_to, "code_challenge": code_challenge,
                       "code_challenge_method": "s256"})
        return f"{self.url}/auth/v1/authorize?{q}"

    def exchange_pkce(self, auth_code, code_verifier):
        r = self.http.post(f"{self.url}/auth/v1/token", params={"grant_type": "pkce"}, headers=self._auth_headers(),
                           timeout=TIMEOUT, json={"auth_code": auth_code, "code_verifier": code_verifier})
        if not r.ok:
            _raise(r)
        return r.json()

    def refresh(self, refresh_token):
        r = self.http.post(f"{self.url}/auth/v1/token", params={"grant_type": "refresh_token"},
                           headers=self._auth_headers(), timeout=TIMEOUT, json={"refresh_token": refresh_token})
        if not r.ok:
            _raise(r)
        return r.json()

    def sign_out(self, access_token):
        self.http.post(f"{self.url}/auth/v1/logout", timeout=TIMEOUT,
                       headers={"apikey": self.anon, "Authorization": f"Bearer {access_token}"})

    # admin (service role): create or update a user without email confirmation
    def admin_headers(self):
        return {"apikey": self.service, "Authorization": f"Bearer {self.service}", "Content-Type": "application/json"}

    def admin_find_user(self, email):
        page = 1
        while True:
            r = self.http.get(f"{self.url}/auth/v1/admin/users", headers=self.admin_headers(), timeout=TIMEOUT,
                              params={"page": page, "per_page": 200})
            if not r.ok:
                _raise(r)
            users = r.json().get("users", [])
            for u in users:
                if (u.get("email") or "").lower() == email.lower():
                    return u
            if len(users) < 200:
                return None
            page += 1

    def admin_upsert_user(self, email, password, name):
        existing = self.admin_find_user(email)
        body = {"email": email, "password": password, "email_confirm": True, "user_metadata": {"display_name": name}}
        if existing:
            r = self.http.put(f"{self.url}/auth/v1/admin/users/{existing['id']}", headers=self.admin_headers(),
                              timeout=TIMEOUT, json=body)
        else:
            r = self.http.post(f"{self.url}/auth/v1/admin/users", headers=self.admin_headers(),
                               timeout=TIMEOUT, json=body)
        if not r.ok:
            _raise(r)
        return r.json()

    # ---------------------------------------------------------------- database (PostgREST)
    def _rest_headers(self, token, prefer=None, extra=None):
        h = {"apikey": self.anon if token != self.service else self.service,
             "Authorization": f"Bearer {token or self.anon}", "Content-Type": "application/json"}
        if prefer:
            h["Prefer"] = prefer
        h.update(extra or {})
        return h

    def select(self, table, token, params=None):
        """All matching rows (pages through PostgREST's 1000-row limit)."""
        out, start = [], 0
        while True:
            r = self.http.get(f"{self.url}/rest/v1/{table}", params=params or {}, timeout=TIMEOUT * 4,
                              headers=self._rest_headers(token, extra={"Range-Unit": "items",
                                                                       "Range": f"{start}-{start + PAGE - 1}"}))
            if not r.ok:
                _raise(r)
            rows = r.json()
            out += rows
            if len(rows) < PAGE:
                return out
            start += PAGE

    def insert(self, table, token, rows, upsert_on=None):
        prefer = "return=representation"
        params = {}
        if upsert_on:
            prefer = "resolution=merge-duplicates," + prefer
            params["on_conflict"] = upsert_on
        r = self.http.post(f"{self.url}/rest/v1/{table}", params=params, json=rows, timeout=TIMEOUT * 4,
                           headers=self._rest_headers(token, prefer))
        if not r.ok:
            _raise(r)
        return r.json()

    def insert_bulk(self, table, token, rows, chunk=2000):
        """Insert many rows without asking for them back."""
        for i in range(0, len(rows), chunk):
            r = self.http.post(f"{self.url}/rest/v1/{table}", json=rows[i:i + chunk], timeout=TIMEOUT * 4,
                               headers=self._rest_headers(token, "return=minimal"))
            if not r.ok:
                _raise(r)

    def update(self, table, token, params, values):
        r = self.http.patch(f"{self.url}/rest/v1/{table}", params=params, json=values, timeout=TIMEOUT,
                            headers=self._rest_headers(token, "return=representation"))
        if not r.ok:
            _raise(r)
        return r.json()

    def delete(self, table, token, params):
        r = self.http.delete(f"{self.url}/rest/v1/{table}", params=params, timeout=TIMEOUT * 4,
                             headers=self._rest_headers(token, "return=representation"))
        if not r.ok:
            _raise(r)
        return r.json()

    # ---------------------------------------------------------------- storage (service role)
    def _storage_headers(self, extra=None):
        h = {"apikey": self.service, "Authorization": f"Bearer {self.service}"}
        h.update(extra or {})
        return h

    def upload(self, bucket, path, data, content_type="application/octet-stream"):
        r = self.http.post(f"{self.url}/storage/v1/object/{bucket}/{path}", data=data, timeout=TIMEOUT * 8,
                           headers=self._storage_headers({"Content-Type": content_type, "x-upsert": "true"}))
        if not r.ok:
            _raise(r)

    def download(self, bucket, path):
        r = self.http.get(f"{self.url}/storage/v1/object/{bucket}/{path}", timeout=TIMEOUT * 8,
                          headers=self._storage_headers())
        if r.status_code == 404 or r.status_code == 400:
            return None
        if not r.ok:
            _raise(r)
        return r.content, r.headers.get("Content-Type", "application/octet-stream")

    def remove(self, bucket, paths):
        r = self.http.delete(f"{self.url}/storage/v1/object/{bucket}", json={"prefixes": paths},
                             timeout=TIMEOUT, headers=self._storage_headers({"Content-Type": "application/json"}))
        if not r.ok:
            _raise(r)
