"""Supabase backend. User-facing reads and writes carry the user's own access token, so the row
level security policies in supabase/migrations decide what each account can see and change.
The service-role key is used only for ingest, seeding and file storage."""
import gzip
import io
import time
from datetime import datetime, timezone

import pandas as pd

from repo_base import AuthError, Forbidden, NotFound, Repo, RepoError, is_uuid
from supa import Supabase, SupaError
from validation import hash_key, new_api_key

AUTH_MESSAGES = (
    ("invalid login", "The email or password is not right."),
    ("invalid_credentials", "The email or password is not right."),
    ("already registered", "An account with that email already exists. Try signing in."),
    ("user_already_exists", "An account with that email already exists. Try signing in."),
    ("email not confirmed", "Confirm your email first. The link was sent to your inbox."),
    ("email_not_confirmed", "Confirm your email first. The link was sent to your inbox."),
    ("weak_password", "That password is too weak. Use a longer one with a mix of characters."),
    ("rate limit", "Too many attempts. Wait a minute and try again."),
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _auth_error(e):
    text = f"{e.message} {e.code or ''}".lower()
    for needle, msg in AUTH_MESSAGES:
        if needle in text:
            return AuthError(msg)
    return AuthError("Sign-in did not work. Check the details and try again.")


class SupabaseRepo(Repo):
    mode = "supabase"

    def __init__(self, url, anon_key, service_key, client=None):
        self.c = client or Supabase(url, anon_key, service_key)
        self.svc = self.c.service

    # ---------------------------------------------------------------- auth
    def _session(self, data, role=None, name=None):
        token = data["access_token"]
        user = data.get("user") or {}
        uid = user.get("id")
        if role is None:
            rows = self.c.select("profiles", token, {"id": f"eq.{uid}", "select": "role,display_name"})
            role = rows[0]["role"] if rows else "owner"
            name = rows[0].get("display_name") if rows else None
        return dict(user_id=uid, email=user.get("email"), name=name or (user.get("email") or "").split("@")[0],
                    role=role, access_token=token, refresh_token=data.get("refresh_token", ""),
                    expires_at=int(data.get("expires_at") or time.time() + int(data.get("expires_in", 3600))))

    def sign_up(self, email, password, name):
        try:
            data = self.c.sign_up(email, password, name)
        except SupaError as e:
            raise _auth_error(e)
        if data.get("access_token"):
            return dict(session=self._session(data), needs_confirm=False)
        return dict(session=None, needs_confirm=True)

    def sign_in(self, email, password):
        try:
            return self._session(self.c.sign_in(email, password))
        except SupaError as e:
            raise _auth_error(e)

    def fresh(self, sess):
        if sess["expires_at"] - time.time() > 60:
            return sess
        try:
            data = self.c.refresh(sess["refresh_token"])
        except SupaError:
            raise AuthError("Your session ended. Sign in again.")
        return self._session(data, role=sess["role"], name=sess["name"]) | dict(
            user_id=sess["user_id"], email=sess["email"])

    def sign_out(self, sess):
        try:
            self.c.sign_out(sess["access_token"])
        except Exception:
            pass

    def ensure_user(self, email, password, name, role):
        user = self.c.admin_upsert_user(email, password, name)
        self.c.insert("profiles", self.svc, [dict(id=user["id"], email=email, display_name=name, role=role)], upsert_on="id")
        return user["id"]

    # ---------------------------------------------------------------- helpers
    def _user(self, sess, fn, *a, **k):
        try:
            return fn(*a, **k)
        except SupaError as e:
            if e.status in (401,):
                raise AuthError("Your session ended. Sign in again.")
            if e.status == 403 or (e.code in ("42501", "PGRST301")):
                raise Forbidden("You do not have permission to change that. The demo account is read-only.")
            if e.status == 409:
                raise RepoError("That already exists.")
            raise RepoError("The database could not complete that request. Try again.")

    # ---------------------------------------------------------------- stores
    def list_stores(self, sess):
        return self._user(sess, self.c.select, "stores", sess["access_token"],
                          {"select": "*", "order": "is_demo.desc,name.asc"})

    def get_store(self, sess, ref):
        col = "id" if is_uuid(ref) else "slug"
        rows = self._user(sess, self.c.select, "stores", sess["access_token"], {"select": "*", col: f"eq.{ref}", "limit": "1"})
        return rows[0] if rows else None

    def create_store(self, sess, f):
        row = dict(owner_id=sess["user_id"], name=f["name"], kind=f.get("kind", "Retail"), blurb=f.get("blurb"),
                   width_m=f["width_m"], height_m=f["height_m"], window_s=f.get("window_s", 300),
                   hero_zone=f.get("hero_zone"), is_demo=False)
        return self._user(sess, self.c.insert, "stores", sess["access_token"], [row])[0]

    def update_store(self, sess, store_id, values):
        allowed = {"name", "kind", "blurb", "width_m", "height_m", "window_s", "hero_zone"}
        values = {k: v for k, v in values.items() if k in allowed}
        rows = self._user(sess, self.c.update, "stores", sess["access_token"], {"id": f"eq.{store_id}"}, values)
        if not rows:
            raise Forbidden("You do not have permission to change that store.")
        return rows[0]

    def delete_store(self, sess, store_id):
        rows = self._user(sess, self.c.delete, "stores", sess["access_token"], {"id": f"eq.{store_id}"})
        if not rows:
            raise Forbidden("You do not have permission to delete that store.")

    # ---------------------------------------------------------------- layouts and datasets
    def list_layouts(self, sess, store_id):
        return self._user(sess, self.c.select, "layouts", sess["access_token"],
                          {"select": "*", "store_id": f"eq.{store_id}", "order": "key.asc"})

    def upsert_layout(self, sess, store_id, key, label, zones):
        row = dict(store_id=store_id, key=key, label=label, zones=zones)
        return self._user(sess, self.c.insert, "layouts", sess["access_token"], [row], upsert_on="store_id,key")[0]

    def delete_layout(self, sess, store_id, layout_id):
        rows = self._user(sess, self.c.delete, "layouts", sess["access_token"],
                          {"id": f"eq.{layout_id}", "store_id": f"eq.{store_id}"})
        if not rows:
            raise NotFound("That layout does not exist.")

    def list_datasets(self, sess, layout_ids):
        if not layout_ids:
            return []
        return self._user(sess, self.c.select, "datasets", sess["access_token"],
                          {"select": "*", "layout_id": f"in.({','.join(layout_ids)})", "order": "created_at.desc"})

    def create_dataset(self, sess, layout_id, label, source, observed_on=None):
        row = dict(layout_id=layout_id, label=label, source=source, observed_on=observed_on)
        return self._user(sess, self.c.insert, "datasets", sess["access_token"], [row], upsert_on="layout_id,label")[0]

    # ---------------------------------------------------------------- API keys
    KEY_COLS = "id,label,prefix,created_at,last_used_at,revoked_at"

    def create_api_key(self, sess, store_id, label):
        plain, prefix, digest = new_api_key()
        row = self._user(sess, self.c.insert, "api_keys", sess["access_token"],
                         [dict(store_id=store_id, label=label, prefix=prefix, key_hash=digest)])[0]
        row.pop("key_hash", None)
        return row, plain

    def list_api_keys(self, sess, store_id):
        return self._user(sess, self.c.select, "api_keys", sess["access_token"],
                          {"select": self.KEY_COLS, "store_id": f"eq.{store_id}", "order": "created_at.desc"})

    def revoke_api_key(self, sess, store_id, key_id):
        self._user(sess, self.c.update, "api_keys", sess["access_token"],
                   {"id": f"eq.{key_id}", "store_id": f"eq.{store_id}", "revoked_at": "is.null"}, {"revoked_at": _now()})

    def resolve_api_key(self, plain):
        rows = self.c.select("api_keys", self.svc, {"select": "id,store_id", "key_hash": f"eq.{hash_key(plain)}",
                                                    "revoked_at": "is.null", "limit": "1"})
        if not rows:
            return None
        try:
            self.c.update("api_keys", self.svc, {"id": f"eq.{rows[0]['id']}"}, {"last_used_at": _now()})
        except SupaError:
            pass
        return dict(store_id=rows[0]["store_id"], key_id=rows[0]["id"])

    # ---------------------------------------------------------------- server-side data
    def svc_get_store(self, store_id):
        rows = self.c.select("stores", self.svc, {"select": "*", "id": f"eq.{store_id}", "limit": "1"})
        return rows[0] if rows else None

    def svc_layout_by_key(self, store_id, key):
        rows = self.c.select("layouts", self.svc, {"select": "*", "store_id": f"eq.{store_id}", "key": f"eq.{key}", "limit": "1"})
        return rows[0] if rows else None

    def svc_get_or_create_dataset(self, layout_id, label, source):
        rows = self.c.select("datasets", self.svc, {"select": "*", "layout_id": f"eq.{layout_id}", "label": f"eq.{label}", "limit": "1"})
        if rows:
            return rows[0]
        return self.c.insert("datasets", self.svc, [dict(layout_id=layout_id, label=label, source=source)],
                             upsert_on="layout_id,label")[0]

    def load_tracks(self, dataset, store):
        path = dataset.get("storage_path")
        if path:
            got = self.c.download("tracks", path)
            if got is None:
                raise NotFound("The track file for this recording is missing.")
            return pd.read_csv(io.BytesIO(got[0]), compression="gzip" if path.endswith(".gz") else None)
        rows = self.c.select("ingest_points", self.svc, {"select": "track_id,t,x,y", "dataset_id": f"eq.{dataset['id']}",
                                                         "order": "track_id.asc,t.asc"})
        return pd.DataFrame(rows, columns=["track_id", "t", "x", "y"])

    def save_tracks(self, dataset, store, df):
        path = f"{store['id']}/{dataset['id']}.csv.gz"
        body = gzip.compress(df[["track_id", "t", "x", "y"]].to_csv(index=False).encode())
        self.c.upload("tracks", path, body, "application/gzip")
        return self.c.update("datasets", self.svc, {"id": f"eq.{dataset['id']}"},
                             dict(storage_path=path, n_points=int(len(df)), n_tracks=int(df.track_id.nunique()),
                                  updated_at=_now()))[0]

    def append_points(self, dataset, df):
        rows = [dict(dataset_id=dataset["id"], track_id=int(a), t=float(b), x=float(c), y=float(d))
                for a, b, c, d in df[["track_id", "t", "x", "y"]].itertuples(index=False)]
        self.c.insert_bulk("ingest_points", self.svc, rows)
        cur = self.c.select("datasets", self.svc, {"select": "n_points", "id": f"eq.{dataset['id']}"})
        n = (cur[0]["n_points"] if cur else 0) + len(rows)
        self.c.update("datasets", self.svc, {"id": f"eq.{dataset['id']}"}, dict(n_points=n, updated_at=_now()))

    def save_plan(self, store, data, content_type):
        path = f"{store['id']}/plan"
        self.c.upload("plans", path, data, content_type)
        self.c.update("stores", self.svc, {"id": f"eq.{store['id']}"}, {"plan_path": path})

    def get_plan(self, store):
        if not store.get("plan_path"):
            return None
        return self.c.download("plans", store["plan_path"])

    # ---------------------------------------------------------------- demo seeding
    def has_demo_stores(self):
        return bool(self.c.select("stores", self.svc, {"select": "id", "is_demo": "eq.true", "limit": "1"}))

    def seed_demo_store(self, slug, spec, frames):
        fields = dict(name=spec["name"], kind=spec["kind"], blurb=spec["blurb"], width_m=spec["w"], height_m=spec["h"],
                      window_s=spec["window_s"], hero_zone=spec["hero"])
        rows = self.c.select("stores", self.svc, {"select": "*", "slug": f"eq.{slug}", "limit": "1"})
        if rows:
            store = self.c.update("stores", self.svc, {"id": f"eq.{rows[0]['id']}"}, fields)[0]
        else:
            store = self.c.insert("stores", self.svc, [dict(fields, slug=slug, owner_id=None, is_demo=True)])[0]
        for key, lay in spec["layouts"].items():
            zones = {n: list(z) for n, z in lay["zones"].items()}
            layout = self.c.insert("layouts", self.svc, [dict(store_id=store["id"], key=key, label=lay["label"], zones=zones)],
                                   upsert_on="store_id,key")[0]
            ds = self.svc_get_or_create_dataset(layout["id"], "Simulated day", "simulated")
            self.save_tracks(ds, store, frames[key])
        return store
