"""Local backend: SQLite and files on disk. Mirrors the Supabase access rules (owner accounts see
their own stores, the demo account sees the shared demo stores read-only), so the app runs and is
tested without any keys. Not meant for production use."""
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import pandas as pd
from werkzeug.security import check_password_hash, generate_password_hash

from repo_base import AuthError, Forbidden, NotFound, Repo, is_uuid
from validation import hash_key, new_api_key

SCHEMA = """
create table if not exists users (id text primary key, email text unique not null, name text, role text not null default 'owner', pw_hash text not null, created_at text);
create table if not exists stores (id text primary key, owner_id text, slug text unique, name text not null, kind text, blurb text,
  width_m real not null, height_m real not null, window_s integer default 300, hero_zone text, plan_path text, is_demo integer default 0, created_at text);
create table if not exists layouts (id text primary key, store_id text not null, key text not null, label text not null, zones text not null default '{}', created_at text, unique (store_id, key));
create table if not exists datasets (id text primary key, layout_id text not null, label text not null, source text not null, storage_path text,
  n_points integer default 0, n_tracks integer default 0, observed_on text, created_at text, updated_at text, unique (layout_id, label));
create table if not exists ingest_points (dataset_id text not null, track_id integer not null, t real not null, x real not null, y real not null);
create index if not exists ingest_points_ds on ingest_points (dataset_id, track_id, t);
create table if not exists api_keys (id text primary key, store_id text not null, label text, prefix text, key_hash text unique not null, created_at text, last_used_at text, revoked_at text);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LocalRepo(Repo):
    mode = "local"

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "files"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(root, "local.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    # ---------------------------------------------------------------- plumbing
    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _run(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    @staticmethod
    def _store(row):
        if row:
            row["is_demo"] = bool(row["is_demo"])
        return row

    @staticmethod
    def _layout(row):
        if row:
            row["zones"] = json.loads(row["zones"] or "{}")
        return row

    def _path(self, *parts):
        p = os.path.join(self.root, "files", *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    # ---------------------------------------------------------------- auth
    @staticmethod
    def _session(u):
        return dict(user_id=u["id"], email=u["email"], name=u["name"], role=u["role"],
                    access_token="local-" + secrets.token_hex(8), refresh_token="", expires_at=int(time.time()) + 10 ** 7)

    def sign_up(self, email, password, name):
        if self._one("select id from users where email = ?", (email,)):
            raise AuthError("An account with that email already exists. Try signing in.")
        uid = str(uuid.uuid4())
        self._run("insert into users (id, email, name, role, pw_hash, created_at) values (?,?,?,?,?,?)",
                  (uid, email, name or email.split("@")[0], "owner", generate_password_hash(password), _now()))
        return dict(session=self._session(self._one("select * from users where id = ?", (uid,))), needs_confirm=False)

    def sign_in(self, email, password):
        u = self._one("select * from users where email = ?", (email,))
        if not u or not check_password_hash(u["pw_hash"], password):
            raise AuthError("The email or password is not right.")
        return self._session(u)

    def fresh(self, sess):
        return sess

    def sign_out(self, sess):
        return None

    def ensure_user(self, email, password, name, role):
        u = self._one("select * from users where email = ?", (email,))
        if u:
            self._run("update users set pw_hash = ?, name = ?, role = ? where id = ?",
                      (generate_password_hash(password), name, role, u["id"]))
            return u["id"]
        uid = str(uuid.uuid4())
        self._run("insert into users (id, email, name, role, pw_hash, created_at) values (?,?,?,?,?,?)",
                  (uid, email, name, role, generate_password_hash(password), _now()))
        return uid

    # ---------------------------------------------------------------- access rules
    def _visible(self, sess, ref):
        col = "id" if is_uuid(ref) else "slug"
        return self._store(self._one(
            f"select * from stores where {col} = ? and (owner_id = ? or (is_demo = 1 and ? = 'demo'))",
            (ref, sess["user_id"], sess["role"])))

    def _readable(self, sess, store_id):
        s = self._visible(sess, store_id)
        if not s:
            raise NotFound("That store does not exist, or you do not have access to it.")
        return s

    def _owned(self, sess, store_id):
        s = self._readable(sess, store_id)
        if s["is_demo"] or s["owner_id"] != sess["user_id"]:
            raise Forbidden("The demo account is read-only.")
        return s

    # ---------------------------------------------------------------- stores
    def list_stores(self, sess):
        rows = self._all("select * from stores where owner_id = ? or (is_demo = 1 and ? = 'demo') order by is_demo desc, name",
                         (sess["user_id"], sess["role"]))
        return [self._store(r) for r in rows]

    def get_store(self, sess, ref):
        return self._visible(sess, ref)

    def create_store(self, sess, f):
        if sess["role"] == "demo":
            raise Forbidden("The demo account is read-only.")
        sid = str(uuid.uuid4())
        self._run("insert into stores (id, owner_id, name, kind, blurb, width_m, height_m, window_s, hero_zone, is_demo, created_at) "
                  "values (?,?,?,?,?,?,?,?,?,0,?)",
                  (sid, sess["user_id"], f["name"], f.get("kind", "Retail"), f.get("blurb"), f["width_m"], f["height_m"],
                   f.get("window_s", 300), f.get("hero_zone"), _now()))
        return self._store(self._one("select * from stores where id = ?", (sid,)))

    def update_store(self, sess, store_id, values):
        self._owned(sess, store_id)
        allowed = {"name", "kind", "blurb", "width_m", "height_m", "window_s", "hero_zone"}
        values = {k: v for k, v in values.items() if k in allowed}
        if values:
            cols = ", ".join(f"{k} = ?" for k in values)
            self._run(f"update stores set {cols} where id = ?", (*values.values(), store_id))
        return self._store(self._one("select * from stores where id = ?", (store_id,)))

    def delete_store(self, sess, store_id):
        self._owned(sess, store_id)
        for l in self._all("select id from layouts where store_id = ?", (store_id,)):
            self._drop_layout(l["id"])
        self._run("delete from api_keys where store_id = ?", (store_id,))
        self._run("delete from stores where id = ?", (store_id,))

    # ---------------------------------------------------------------- layouts and datasets
    def list_layouts(self, sess, store_id):
        self._readable(sess, store_id)
        return [self._layout(r) for r in self._all("select * from layouts where store_id = ? order by key", (store_id,))]

    def upsert_layout(self, sess, store_id, key, label, zones):
        self._owned(sess, store_id)
        ex = self._one("select id from layouts where store_id = ? and key = ?", (store_id, key))
        if ex:
            self._run("update layouts set label = ?, zones = ? where id = ?", (label, json.dumps(zones), ex["id"]))
            lid = ex["id"]
        else:
            lid = str(uuid.uuid4())
            self._run("insert into layouts (id, store_id, key, label, zones, created_at) values (?,?,?,?,?,?)",
                      (lid, store_id, key, label, json.dumps(zones), _now()))
        return self._layout(self._one("select * from layouts where id = ?", (lid,)))

    def _drop_layout(self, layout_id):
        for d in self._all("select * from datasets where layout_id = ?", (layout_id,)):
            if d["storage_path"] and os.path.exists(d["storage_path"]):
                os.remove(d["storage_path"])
            self._run("delete from ingest_points where dataset_id = ?", (d["id"],))
        self._run("delete from datasets where layout_id = ?", (layout_id,))
        self._run("delete from layouts where id = ?", (layout_id,))

    def delete_layout(self, sess, store_id, layout_id):
        self._owned(sess, store_id)
        if not self._one("select id from layouts where id = ? and store_id = ?", (layout_id, store_id)):
            raise NotFound("That layout does not exist.")
        self._drop_layout(layout_id)

    def list_datasets(self, sess, layout_ids):
        out = []
        for lid in layout_ids:
            l = self._one("select store_id from layouts where id = ?", (lid,))
            if l and self._visible(sess, l["store_id"]):
                out += self._all("select * from datasets where layout_id = ? order by created_at desc", (lid,))
        return out

    def create_dataset(self, sess, layout_id, label, source, observed_on=None):
        l = self._one("select store_id from layouts where id = ?", (layout_id,))
        if not l:
            raise NotFound("That layout does not exist.")
        self._owned(sess, l["store_id"])
        return self._new_dataset(layout_id, label, source, observed_on)

    def _new_dataset(self, layout_id, label, source, observed_on=None):
        did = str(uuid.uuid4())
        self._run("insert into datasets (id, layout_id, label, source, observed_on, created_at, updated_at) values (?,?,?,?,?,?,?)",
                  (did, layout_id, label, source, observed_on, _now(), _now()))
        return self._one("select * from datasets where id = ?", (did,))

    # ---------------------------------------------------------------- API keys
    def create_api_key(self, sess, store_id, label):
        self._owned(sess, store_id)
        plain, prefix, digest = new_api_key()
        kid = str(uuid.uuid4())
        self._run("insert into api_keys (id, store_id, label, prefix, key_hash, created_at) values (?,?,?,?,?,?)",
                  (kid, store_id, label, prefix, digest, _now()))
        return self._one("select id, label, prefix, created_at, last_used_at, revoked_at from api_keys where id = ?", (kid,)), plain

    def list_api_keys(self, sess, store_id):
        self._owned(sess, store_id)
        return self._all("select id, label, prefix, created_at, last_used_at, revoked_at from api_keys where store_id = ? order by created_at desc",
                         (store_id,))

    def revoke_api_key(self, sess, store_id, key_id):
        self._owned(sess, store_id)
        self._run("update api_keys set revoked_at = ? where id = ? and store_id = ? and revoked_at is null", (_now(), key_id, store_id))

    def resolve_api_key(self, plain):
        k = self._one("select id, store_id from api_keys where key_hash = ? and revoked_at is null", (hash_key(plain),))
        if k:
            self._run("update api_keys set last_used_at = ? where id = ?", (_now(), k["id"]))
            return dict(store_id=k["store_id"], key_id=k["id"])
        return None

    # ---------------------------------------------------------------- server-side data
    def svc_get_store(self, store_id):
        return self._store(self._one("select * from stores where id = ?", (store_id,)))

    def svc_layout_by_key(self, store_id, key):
        return self._layout(self._one("select * from layouts where store_id = ? and key = ?", (store_id, key)))

    def svc_get_or_create_dataset(self, layout_id, label, source):
        d = self._one("select * from datasets where layout_id = ? and label = ?", (layout_id, label))
        return d or self._new_dataset(layout_id, label, source)

    def load_tracks(self, dataset, store):
        if dataset.get("storage_path"):
            return pd.read_csv(dataset["storage_path"])
        with self.lock:
            return pd.read_sql_query("select track_id, t, x, y from ingest_points where dataset_id = ? order by track_id, t",
                                     self.db, params=(dataset["id"],))

    def save_tracks(self, dataset, store, df):
        path = self._path("tracks", store["id"], f"{dataset['id']}.csv.gz")
        df[["track_id", "t", "x", "y"]].to_csv(path, index=False)
        self._run("update datasets set storage_path = ?, n_points = ?, n_tracks = ?, updated_at = ? where id = ?",
                  (path, int(len(df)), int(df.track_id.nunique()), _now(), dataset["id"]))
        return self._one("select * from datasets where id = ?", (dataset["id"],))

    def append_points(self, dataset, df):
        rows = [(dataset["id"], int(a), float(b), float(c), float(d)) for a, b, c, d in df[["track_id", "t", "x", "y"]].itertuples(index=False)]
        with self.lock:
            self.db.executemany("insert into ingest_points (dataset_id, track_id, t, x, y) values (?,?,?,?,?)", rows)
            self.db.execute("update datasets set n_points = n_points + ?, updated_at = ? where id = ?", (len(rows), _now(), dataset["id"]))
            self.db.commit()

    def save_plan(self, store, data, content_type):
        path = self._path("plans", store["id"], "plan")
        with open(path, "wb") as fh:
            fh.write(data)
        with open(path + ".type", "w") as fh:
            fh.write(content_type)
        self._run("update stores set plan_path = ? where id = ?", (path, store["id"]))

    def get_plan(self, store):
        path = store.get("plan_path")
        if not path or not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            data = fh.read()
        ctype = open(path + ".type").read() if os.path.exists(path + ".type") else "image/png"
        return data, ctype

    # ---------------------------------------------------------------- demo seeding
    def has_demo_stores(self):
        return bool(self._one("select id from stores where is_demo = 1"))

    def seed_demo_store(self, slug, spec, frames):
        s = self._one("select * from stores where slug = ?", (slug,))
        if not s:
            sid = str(uuid.uuid4())
            self._run("insert into stores (id, owner_id, slug, name, kind, blurb, width_m, height_m, window_s, hero_zone, is_demo, created_at) "
                      "values (?,?,?,?,?,?,?,?,?,?,1,?)",
                      (sid, None, slug, spec["name"], spec["kind"], spec["blurb"], spec["w"], spec["h"], spec["window_s"], spec["hero"], _now()))
            s = self._one("select * from stores where slug = ?", (slug,))
        s = self._store(s)
        for key, lay in spec["layouts"].items():
            zones = {n: list(z) for n, z in lay["zones"].items()}
            ex = self._one("select id from layouts where store_id = ? and key = ?", (s["id"], key))
            if ex:
                self._run("update layouts set label = ?, zones = ? where id = ?", (lay["label"], json.dumps(zones), ex["id"]))
                lid = ex["id"]
            else:
                lid = str(uuid.uuid4())
                self._run("insert into layouts (id, store_id, key, label, zones, created_at) values (?,?,?,?,?,?)",
                          (lid, s["id"], key, lay["label"], json.dumps(zones), _now()))
            ds = self.svc_get_or_create_dataset(lid, "Simulated day", "simulated")
            self.save_tracks(ds, s, frames[key])
        return s
