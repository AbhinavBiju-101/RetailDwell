"""Agent setup page. Run:  python -m tests.test_agent_page"""
import os
import re
import sys
import tempfile

os.environ.setdefault("AUTO_SEED", "0")
os.environ.setdefault("WARM", "0")


def _login(c, email, pw):
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").data.decode()).group(1)
    return c.post("/login", data=dict(email=email, password=pw, csrf=tok))


def test_agent_page():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    sess = repo.sign_up("o@t.example", "password-1", "O")["session"]
    st = repo.create_store(sess, dict(name="My <b>Shop</b>", width_m=12.0, height_m=8.0))
    c = create_app(repo=repo).test_client()
    url = f"/app/s/{st['id']}/agent"
    assert c.get(url).status_code == 302                               # signed out
    _login(c, "o@t.example", "password-1")
    assert b"no layout yet" in c.get(url).data
    repo.upsert_layout(sess, st["id"], "A", "Before", {"Shelf": [1, 1, 3, 3, "merchandise"]})
    repo.upsert_layout(sess, st["id"], "B", "After", {})
    _, key = repo.create_api_key(sess, st["id"], "Door cam")
    t = c.get(url + "?layout=b").data.decode()
    assert "--layout B" in t and "Door cam" in t and "--width 12 --height 8" in t
    assert key not in t and "<b>Shop" not in t                          # key never re-shown, name escaped
    assert "--layout A" in c.get(url + "?layout=zzz").data.decode()      # unknown layout falls back
    for path in ("/app", url):                                           # signed-in pages link back to the main page
        assert 'class="back-link" href="/"' in c.get(path, follow_redirects=True).data.decode()


def test_demo_account_cannot_open_it():
    from app import create_app
    from repo_local import LocalRepo
    repo = LocalRepo(tempfile.mkdtemp())
    repo.ensure_user("demo@t.example", "demo-demo-1", "Demo", "demo")
    c = create_app(repo=repo).test_client()
    _login(c, "demo@t.example", "demo-demo-1")
    assert c.get("/app/s/anything/agent").status_code in (403, 404)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok   ", n)
