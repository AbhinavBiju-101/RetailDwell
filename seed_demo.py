"""Create the demo account and the demo stores with simulated data.

  python seed_demo.py                 # uses .env (Supabase) or the local SQLite store
  python seed_demo.py --only corner-pharmacy

The demo account comes from DEMO_EMAIL and DEMO_PASSWORD in .env. It is read-only and sees only the
demo stores. Safe to run again: stores, layouts and recordings are updated in place.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import simulate  # noqa: E402
from repo import get_repo  # noqa: E402
from stores import STORES  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))


def seed(repo, only=None, log=print):
    for slug, spec in STORES.items():
        if only and slug != only:
            continue
        log(f"Simulating {spec['name']} ...")
        frames = {k: df[["track_id", "t", "x", "y"]] for k, (df, _pos) in simulate.generate(slug).items()}
        repo.seed_demo_store(slug, spec, frames)
        log(f"  stored {sum(len(f) for f in frames.values()):,} points in {len(frames)} layouts")


def ensure_demo_user(repo, log=print):
    email, password = os.environ.get("DEMO_EMAIL", "").strip(), os.environ.get("DEMO_PASSWORD", "")
    if not email or not password:
        log("DEMO_EMAIL and DEMO_PASSWORD are not set, so no demo account was created.")
        return False
    repo.ensure_user(email, password, "Demo", "demo")
    log(f"Demo account ready: {email}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="seed a single store slug")
    args = ap.parse_args()
    repo = get_repo(os.path.join(BASE, "data", "local"))
    print(f"Backend: {repo.mode}")
    ensure_demo_user(repo)
    seed(repo, args.only)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
