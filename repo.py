"""Chooses the storage backend from the environment.

  SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY all set -> Supabase
  otherwise                                                        -> local SQLite (development only)
"""
import logging
import os

log = logging.getLogger("dwell")


def get_repo(local_dir):
    url = os.environ.get("SUPABASE_URL", "").strip()
    anon = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    service = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if url and anon and service:
        from repo_supabase import SupabaseRepo
        log.info("Storage: Supabase (%s)", url)
        return SupabaseRepo(url, anon, service)
    from repo_local import LocalRepo
    log.warning("Storage: LOCAL SQLite in %s. Set SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY to use Supabase.", local_dir)
    return LocalRepo(local_dir)
