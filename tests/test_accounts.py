import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app import auth, db
from app.config import get_settings
from app.main import app
from app.repo import chat as chat_repo
from app.repo import shares as shares_repo
from app.repo import users as users_repo
from tests.conftest import ensure_user, login


@pytest.fixture(autouse=True)
def _clean_attempts(conn):
    yield
    conn.execute("DELETE FROM login_attempts")
    conn.commit()


@pytest.fixture
def no_users(conn):
    """An instance that has never had an account (other tests' users come back via ensure_user)."""
    conn.execute("DELETE FROM users")
    conn.commit()
    yield
    conn.execute("DELETE FROM users")
    conn.commit()


def _setup_form(**kw):
    return {"app_password": "pw", "username": "dana", "display_name": "Dana",
            "password": "correct horse", "password2": "correct horse", **kw}


# --- hashing ---------------------------------------------------------------

def test_password_hash_is_salted_scrypt():
    a, b = users_repo.hash_password("hunter22"), users_repo.hash_password("hunter22")
    assert a != b and a.startswith("scrypt$")
    assert users_repo.verify_password("hunter22", a) and not users_repo.verify_password("hunter23", a)
    assert not users_repo.verify_password("hunter22", "garbage")


# --- bootstrap -------------------------------------------------------------

def test_bootstrap_requires_app_password(conn, no_users):
    with TestClient(app) as c:
        page = c.get("/login").text
        assert "Create the first (admin) account" in page and 'action="/login/setup"' in page
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"

        r = c.post("/login/setup", data=_setup_form(app_password="wrong"), follow_redirects=False)
        assert r.status_code == 401 and users_repo.count(conn) == 0
        r = c.post("/login/setup", data=_setup_form(app_password=""), follow_redirects=False)
        assert r.status_code == 401 and users_repo.count(conn) == 0
        assert c.get("/", follow_redirects=False).status_code == 303

        r = c.post("/login/setup", data=_setup_form(), follow_redirects=False)
        assert r.status_code == 303
        u = users_repo.get_by_username(conn, "DANA")
        assert u["is_admin"] == 1 and u["display_name"] == "Dana"
        assert c.get("/").status_code == 200  # signed in straight away
        assert c.get("/settings/users").status_code == 200

    with TestClient(app) as c:  # once an account exists: normal login, and setup is closed
        page = c.get("/login").text
        assert "first (admin) account" not in page and 'name="username"' in page
        r = c.post("/login/setup", data=_setup_form(username="mallory"), follow_redirects=False)
        assert r.status_code == 303 and users_repo.get_by_username(conn, "mallory") is None
        assert c.get("/", follow_redirects=False).status_code == 303


def test_bootstrap_refused_when_app_password_unset(conn, no_users, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_password", "")
    with TestClient(app) as c:
        r = c.post("/login/setup", data=_setup_form(app_password=""), follow_redirects=False)
        assert r.status_code == 401 and users_repo.count(conn) == 0


def test_app_password_only_needed_until_an_account_exists(conn, no_users, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_password", "")
    with pytest.raises(RuntimeError, match="APP_PASSWORD"):
        auth.check_config()
    ensure_user("firstadmin")
    auth.check_config()  # no error


def test_bootstrap_validates_input(conn, no_users):
    with TestClient(app) as c:
        assert c.post("/login/setup", data=_setup_form(password2="different")).status_code == 400
        assert c.post("/login/setup", data=_setup_form(password="short", password2="short")).status_code == 400
        assert c.post("/login/setup", data=_setup_form(username="no spaces!")).status_code == 400
    assert users_repo.count(conn) == 0


# --- login -----------------------------------------------------------------

def test_username_login_is_case_insensitive_and_rotates_session():
    ensure_user("casey", "casey-password", is_admin=False)
    with TestClient(app) as c:
        c.get("/login")
        before = c.cookies.get("manualagerie")
        r = c.post("/login", data={"username": "  Casey ", "password": "casey-password"}, follow_redirects=False)
        assert r.status_code == 303
        first = c.cookies.get("manualagerie")
        assert first and first != before
        assert c.get("/").status_code == 200
        c.post("/login", data={"username": "casey", "password": "casey-password"}, follow_redirects=False)
        assert c.cookies.get("manualagerie") != first  # a fresh session on every login


def test_wrong_password_and_unknown_user_fail():
    ensure_user("wendy", "wendy-password")
    with TestClient(app) as c:
        assert c.post("/login", data={"username": "wendy", "password": "nope"}).status_code == 401
        assert c.post("/login", data={"username": "nobody", "password": "nope"}).status_code == 401
        assert c.post("/login", data={"password": "pw"}).status_code == 401  # the old password-only form
        assert c.get("/", follow_redirects=False).status_code == 303


def test_wrong_password_is_rate_limited_per_ip():
    ensure_user("ivy", "ivy-password")
    ip = {"x-forwarded-for": "203.0.113.7"}
    with TestClient(app) as c:
        for _ in range(auth.MAX_FAILURES):
            assert c.post("/login", data={"username": "ivy", "password": "bad"}, headers=ip).status_code == 401
        r = c.post("/login", data={"username": "ivy", "password": "ivy-password"}, headers=ip)
        assert r.status_code == 429 and "Too many attempts" in r.text
        # A different IP and username is unaffected.
        ensure_user("other", "other-password")
        r = c.post("/login", data={"username": "other", "password": "other-password"},
                   headers={"x-forwarded-for": "203.0.113.8"}, follow_redirects=False)
        assert r.status_code == 303


def test_wrong_password_is_rate_limited_per_username():
    ensure_user("uma", "uma-password")
    with TestClient(app) as c:
        for i in range(auth.MAX_FAILURES):  # spread over many IPs
            r = c.post("/login", data={"username": "UMA", "password": "bad"},
                       headers={"x-forwarded-for": f"198.51.100.{i}"})
            assert r.status_code == 401
        r = c.post("/login", data={"username": "uma", "password": "uma-password"},
                   headers={"x-forwarded-for": "198.51.100.200"})
        assert r.status_code == 429


def test_pre_accounts_cookie_no_longer_authenticates():
    payload = base64.b64encode(json.dumps({"auth": True}).encode())
    cookie = TimestampSigner("test-secret").sign(payload).decode()
    with TestClient(app) as c:
        c.cookies.set("manualagerie", cookie)
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"


# --- disabling, passwords --------------------------------------------------

def test_disabled_user_is_logged_out(conn):
    with TestClient(app) as admin, TestClient(app) as member:
        login(admin, "alice", "alice-password")
        assert login(member, "bob", "bob-password", is_admin=False).status_code == 303
        assert member.get("/").status_code == 200
        bob = users_repo.get_by_username(conn, "bob")["id"]

        assert admin.post(f"/settings/users/{bob}/disable", follow_redirects=False).status_code == 303
        assert member.get("/", follow_redirects=False).headers["location"] == "/login"
        assert member.post("/api/conversations").status_code == 401
        r = member.post("/login", data={"username": "bob", "password": "bob-password"})
        assert r.status_code == 401  # can't sign back in either

        admin.post(f"/settings/users/{bob}/enable")
        assert member.get("/", follow_redirects=False).status_code == 303  # old session stays dead
        assert login(member, "bob", "bob-password").status_code == 303
        assert member.get("/").status_code == 200


def test_password_reset_signs_out_existing_sessions(conn):
    with TestClient(app) as admin, TestClient(app) as member:
        login(admin, "alice", "alice-password")
        login(member, "carol", "carol-password", is_admin=False)
        carol = users_repo.get_by_username(conn, "carol")["id"]
        assert admin.post(f"/settings/users/{carol}/password", data={"password": "short"}).status_code == 400
        admin.post(f"/settings/users/{carol}/password", data={"password": "new-carol-pw"})
        assert member.get("/", follow_redirects=False).status_code == 303
        assert login(member, "carol", "new-carol-pw").status_code == 303
        # Resetting your own password keeps you signed in.
        alice = users_repo.get_by_username(conn, "alice")["id"]
        admin.post(f"/settings/users/{alice}/password", data={"password": "alice-password"})
        assert admin.get("/settings/users").status_code == 200


def test_change_my_password():
    with TestClient(app) as c, TestClient(app) as other_device:
        login(c, "dave", "dave-password", is_admin=False)
        login(other_device, "dave", "dave-password", is_admin=False)
        assert "Change my password" in c.get("/settings/password").text
        r = c.post("/settings/password", data={"current": "wrong", "password": "dave-new-pw", "password2": "dave-new-pw"})
        assert r.status_code == 401
        r = c.post("/settings/password", data={"current": "dave-password", "password": "dave-new-pw",
                                               "password2": "nope-nope"})
        assert r.status_code == 400
        r = c.post("/settings/password", data={"current": "dave-password", "password": "dave-new-pw",
                                               "password2": "dave-new-pw"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/").status_code == 200  # this session continues
        assert other_device.get("/", follow_redirects=False).status_code == 303  # others end
        assert c.post("/login", data={"username": "dave", "password": "dave-password"}).status_code == 401
        assert login(c, "dave", "dave-new-pw").status_code == 303


# --- admin pages -----------------------------------------------------------

def test_non_admins_cannot_reach_admin_pages(conn):
    target = ensure_user("erin", "erin-password", is_admin=False)
    with TestClient(app) as c:
        login(c, "erin", "erin-password", is_admin=False)
        page = c.get("/").text
        assert 'href="/settings/users"' not in page and 'href="/settings/password"' in page
        assert c.get("/settings/users").status_code == 403
        assert c.post("/settings/users", data={"username": "x1", "password": "xxxxxxxx"}).status_code == 403
        assert c.post(f"/settings/users/{target}/admin").status_code == 403
        assert c.post(f"/settings/users/{target}/password", data={"password": "yyyyyyyy"}).status_code == 403
        assert c.post(f"/settings/users/{target}/disable").status_code == 403
    assert users_repo.get(conn, target)["is_admin"] == 0
    with TestClient(app) as c:  # logged out: redirected to login
        assert c.get("/settings/users", follow_redirects=False).headers["location"] == "/login"
        assert c.get("/settings/password", follow_redirects=False).headers["location"] == "/login"


def test_admin_adds_users(conn):
    with TestClient(app) as c:
        login(c, "alice", "alice-password")
        assert 'href="/settings/users"' in c.get("/").text
        r = c.post("/settings/users", data={"username": "Frank", "display_name": "Frank F",
                                            "password": "frank-password"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Frank F" in c.get("/settings/users").text
        assert c.post("/settings/users", data={"username": "frank", "password": "frank-password"}).status_code == 400
        assert c.post("/settings/users", data={"username": "gina", "password": "short"}).status_code == 400
    u = users_repo.get_by_username(conn, "frank")
    assert u["is_admin"] == 0
    with TestClient(app) as c:
        assert c.post("/login", data={"username": "frank", "password": "frank-password"},
                      follow_redirects=False).status_code == 303


def test_last_admin_cannot_be_disabled_or_demoted(conn, no_users):
    solo = ensure_user("solo", "solo-password")
    with TestClient(app) as c:
        login(c, "solo", "solo-password")
        r = c.post(f"/settings/users/{solo}/disable")
        assert r.status_code == 200 and "last admin" in r.text
        r = c.post(f"/settings/users/{solo}/admin")
        assert "last admin" in r.text
        u = users_repo.get(conn, solo)
        assert u["disabled_at"] is None and u["is_admin"] == 1

        # A disabled admin doesn't count: the only *active* admin is still protected.
        ghost = ensure_user("ghost", "ghost-password", is_admin=True)
        users_repo.set_disabled(conn, ghost, True)
        conn.commit()
        assert "last admin" in c.post(f"/settings/users/{solo}/disable").text

        second = ensure_user("second", "second-password", is_admin=True)
        c.post(f"/settings/users/{second}/disable")
        assert users_repo.get(conn, second)["disabled_at"] is not None
        c.post(f"/settings/users/{second}/enable")
        c.post(f"/settings/users/{solo}/admin")  # demote solo: second is still an admin
        assert users_repo.get(conn, solo)["is_admin"] == 0
        assert c.get("/settings/users").status_code == 403  # solo is now a member


# --- attribution and scoping -----------------------------------------------

def test_conversations_are_scoped_by_user(conn):
    ensure_user("hana", "hana-password", is_admin=False, display_name="Hana")
    ensure_user("ian", "ian-password", is_admin=False, display_name="Ian")
    with TestClient(app) as hana, TestClient(app) as ian:
        login(hana, "hana", "hana-password")
        login(ian, "ian", "ian-password")
        h = hana.post("/api/conversations").json()["id"]
        i = ian.post("/api/conversations").json()["id"]
        chat_repo.set_title(conn, h, "Hana furnace question")
        chat_repo.add_message(conn, h, "user", "how do I relight the zorbulator pilot")
        chat_repo.set_title(conn, i, "Ian fridge question")
        chat_repo.add_message(conn, i, "user", "zorbulator water filter size")
        legacy = chat_repo.create_conversation(conn, "Legacy chat")  # from before accounts
        conn.commit()

        assert conn.execute("SELECT user_id FROM conversations WHERE id = ?", (h,)).fetchone()[0] == \
            users_repo.get_by_username(conn, "hana")["id"]

        mine = hana.get("/").text
        assert "Hana furnace question" in mine and "Ian fridge question" not in mine and "Legacy chat" not in mine
        assert 'href="/?all=1"' in mine

        everyone = hana.get("/?all=1").text
        assert "Hana furnace question" in everyone and "Ian fridge question" in everyone
        assert "· Ian" in everyone and "Legacy chat" in everyone and "· unknown" in everyone

        # A household: anyone can open anyone's conversation.
        assert hana.get(f"/c/{i}").status_code == 200

        # Search follows the same toggle.
        r = hana.get("/api/conversations/search", params={"q": "zorbulator"})
        assert f'href="/c/{h}"' in r.text and f'href="/c/{i}' not in r.text
        r = hana.get("/api/conversations/search", params={"q": "zorbulator", "all": "1"})
        assert f'href="/c/{h}?all=1"' in r.text and f'href="/c/{i}?all=1"' in r.text
        r = hana.get("/api/conversations/search", params={"q": ""})
        assert "Hana furnace question" in r.text and "Ian fridge question" not in r.text
    for cid in (h, i, legacy):
        chat_repo.delete_conversation(conn, cid)
    conn.commit()


def test_shares_notes_and_maintenance_record_who(conn):
    ensure_user("jo", "jo-password", is_admin=False, display_name="Jo")
    with TestClient(app) as c:
        login(c, "jo", "jo-password")
        cid = c.post("/api/conversations").json()["id"]
        chat_repo.add_message(conn, cid, "user", "Drain hose size?")
        mid = chat_repo.add_message(conn, cid, "assistant", "It is **5/8 inch**.")
        shares_repo.create(conn, "Old share", "from before accounts")
        conn.commit()
        assert c.post(f"/api/messages/{mid}/share").status_code == 200
        page = c.get("/shares").text
        assert "Shared by Jo" in page and "Shared by unknown" in page

        c.post("/notes", data={"title": "Jo's paint colour", "body": "Hallway: Farrow & Ball 239"})
        notes = c.get("/notes").text
        assert "Added by Jo" in notes

        c.post("/maintenance", data={"title": "Jo descales kettle", "interval_days": "30"})
        tid = conn.execute("SELECT id FROM maintenance_tasks WHERE title = 'Jo descales kettle'").fetchone()[0]
        c.post(f"/maintenance/{tid}/done")
        row = conn.execute("SELECT created_by FROM maintenance_log WHERE task_id = ?", (tid,)).fetchone()
        assert row[0] == users_repo.get_by_username(conn, "jo")["id"]
        assert "by Jo" in c.get("/maintenance").text

        token = shares_repo.list_active(conn)[0]["token"]
    with TestClient(app) as anon:  # the public page doesn't say who shared it
        r = anon.get(f"/s/{token}")
        assert r.status_code == 200 and "Jo" not in r.text


# --- public routes ---------------------------------------------------------

def test_public_routes_stay_public_and_narrow(conn):
    _, token = shares_repo.create(conn, "Filter", "Use filter **XWF**.")
    conn.commit()
    with TestClient(app) as c:
        assert c.get(f"/s/{token}").status_code == 200
        assert c.get("/healthz").json() == {"ok": True}
        assert c.get("/static/app.css").status_code == 200
        assert c.get("/login").status_code == 200
        for path in ("/loginx", "/login/other", "/healthzz", "/settings/users", "/settings/password",
                     f"/s/{token}/x", "/s/"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path
        assert c.post("/settings/users", data={"username": "x", "password": "xxxxxxxx"},
                      follow_redirects=False).status_code == 303


def test_public_paths_list_is_exact():
    assert auth.is_public("/login") and auth.is_public("/login/setup") and auth.is_public("/s/abc_-1")
    assert not auth.is_public("/login/setup/x") and not auth.is_public("/static")
    assert not auth.is_public("/settings/users") and not auth.is_public("/s/abc/def")


def test_db_has_users_migration(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 13
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert "user_id" in cols
    for table in ("shares", "notes", "maintenance_log"):
        assert "created_by" in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    with db.get_conn() as c:
        c.execute("SELECT username, display_name, password_hash, is_admin, created_at, disabled_at FROM users")
