"""Storage interface used by the web app. Two implementations:

  repo_supabase.SupabaseRepo  - Supabase Auth, Postgres (with row level security) and Storage
  repo_local.LocalRepo        - SQLite and files on disk, for development without keys

Every read the app makes on behalf of a signed-in user goes through `sess` (the session dict),
so access rules live in one place: the database policies in Supabase, mirrored in LocalRepo.
"""
import re

import pandas as pd

from config import POS_MAX_TXNS

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class RepoError(Exception):
    """A failure with a message that is safe to show the user."""


class AuthError(RepoError):
    pass


class NotFound(RepoError):
    pass


class Forbidden(RepoError):
    pass


def store_ref(store):
    """Short, readable reference used in URLs: the slug for demo stores, otherwise the id."""
    return store.get("slug") or store["id"]


def is_uuid(value):
    return bool(UUID_RE.match(value or ""))


class Repo:
    mode = "abstract"
    supports_oauth = False        # sign-in with an outside provider such as Google (Supabase only)

    def oauth_start(self, provider, redirect_to, code_challenge): raise NotImplementedError
    def oauth_finish(self, code, code_verifier): raise NotImplementedError

    # ---- auth
    def sign_up(self, email, password, name):          # -> {"session": dict|None, "needs_confirm": bool}
        raise NotImplementedError

    def sign_in(self, email, password):                # -> session dict
        raise NotImplementedError

    def fresh(self, sess):                             # -> session (refreshed if it is about to expire)
        raise NotImplementedError

    def sign_out(self, sess):
        raise NotImplementedError

    # ---- stores, layouts, datasets (as the signed-in user)
    def list_stores(self, sess): raise NotImplementedError
    def get_store(self, sess, ref): raise NotImplementedError
    def create_store(self, sess, fields): raise NotImplementedError
    def update_store(self, sess, store_id, values): raise NotImplementedError
    def delete_store(self, sess, store_id): raise NotImplementedError
    def list_layouts(self, sess, store_id): raise NotImplementedError
    def upsert_layout(self, sess, store_id, key, label, zones): raise NotImplementedError
    def delete_layout(self, sess, store_id, layout_id): raise NotImplementedError
    def list_datasets(self, sess, layout_ids): raise NotImplementedError
    def create_dataset(self, sess, layout_id, label, source, observed_on=None): raise NotImplementedError

    # ---- API keys (as the owner)
    def create_api_key(self, sess, store_id, label): raise NotImplementedError   # -> (record, plain key)
    def list_api_keys(self, sess, store_id): raise NotImplementedError
    def revoke_api_key(self, sess, store_id, key_id): raise NotImplementedError

    # ---- server-side operations (service role): data files, ingest, seeding
    def load_tracks(self, dataset, store): raise NotImplementedError             # -> DataFrame
    def save_tracks(self, dataset, store, df): raise NotImplementedError
    def append_points(self, dataset, df): raise NotImplementedError
    def resolve_api_key(self, plain): raise NotImplementedError                  # -> {"store_id", "key_id"} | None
    def svc_get_store(self, store_id): raise NotImplementedError
    def svc_layout_by_key(self, store_id, key): raise NotImplementedError
    def svc_get_or_create_dataset(self, layout_id, label, source): raise NotImplementedError
    def delete_dataset(self, dataset, store): raise NotImplementedError          # a recording, its tracks file and its sales file
    def set_dataset_start(self, dataset, started_at): raise NotImplementedError   # ISO UTC text or None
    def save_pos(self, dataset, store, df): raise NotImplementedError            # sales lines: txn_id, t, zone, amount
    def load_pos(self, dataset, store): raise NotImplementedError                # -> DataFrame (empty when none)

    def append_pos(self, dataset, store, df):
        """Add sales lines to a recording; a line with the same txn_id and zone replaces the earlier one."""
        cur = self.load_pos(dataset, store)
        out = pd.concat([cur, df], ignore_index=True).drop_duplicates(["txn_id", "zone"], keep="last")
        if out["txn_id"].nunique() > POS_MAX_TXNS:
            raise RepoError(f"A recording holds at most {POS_MAX_TXNS:,} sales. Start a new recording for the next period.")
        return self.save_pos(dataset, store, out)

    def save_plan(self, store, data, content_type): raise NotImplementedError
    def get_plan(self, store): raise NotImplementedError                          # -> (bytes, content_type) | None
    def ensure_user(self, email, password, name, role): raise NotImplementedError
    def seed_demo_store(self, slug, spec, frames, sales=None): raise NotImplementedError
    def has_demo_stores(self): raise NotImplementedError
