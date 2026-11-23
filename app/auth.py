import hmac
import re
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from . import db
from .config import get_settings
from .repo import users as users_repo

# Reachable without a session. Kept exact: the login page and its first-account form, the health check.
PUBLIC_PATHS = frozenset({"/login", "/login/setup", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)
# Public share pages: exactly /s/<token>, nothing below or beside it.
PUBLIC_SHARE_RE = re.compile(r"/s/[A-Za-z0-9_-]+")
WINDOW_S = 15 * 60
MAX_FAILURES = 10


def resolve_secret_key() -> str:
    s = get_settings()
    if s.secret_key:
        return s.secret_key
    path = s.data_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    return key


def users_exist() -> bool:
    """True once an account exists. False for a fresh or not-yet-migrated database."""
    path = get_settings().db_path
    if not path.exists():
        return False
    try:
        conn = db.connect(path)
        try:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def check_config() -> None:
    """APP_PASSWORD is only needed to create the first account; after that the app runs without it."""
    s = get_settings()
    if not s.app_password and not s.auth_disabled and not users_exist():
        raise RuntimeError("APP_PASSWORD must be set to create the first account "
                           "(or AUTH_DISABLED=true for local development).")


def current_user(request: Request):
    """The signed-in, enabled user (a sqlite Row), or None (logged out, or AUTH_DISABLED)."""
    return getattr(request.state, "user", None)


def current_user_id(request: Request) -> int | None:
    u = current_user(request)
    return u["id"] if u else None


def is_authed(request: Request) -> bool:
    return get_settings().auth_disabled or current_user(request) is not None


def is_admin(request: Request) -> bool:
    u = current_user(request)
    return get_settings().auth_disabled or bool(u and u["is_admin"])


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(403, "Admins only")


def _load_user(session: dict):
    """The session's user if it is still valid: exists, enabled, and on the current session generation."""
    uid, gen = session.get("uid"), session.get("gen")
    if not isinstance(uid, int) or not isinstance(gen, int):
        return None
    with db.get_conn() as conn:
        u = users_repo.get(conn, uid)
    if u is None or u["disabled_at"] is not None or u["session_gen"] != gen:
        return None
    return u


def start_session(request: Request, user) -> None:
    """Rotate: drop whatever the old cookie held and issue a fresh one for this user."""
    request.session.clear()
    request.session.update({"uid": user["id"], "gen": user["session_gen"], "sid": secrets.token_urlsafe(16)})


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or PUBLIC_SHARE_RE.fullmatch(path) is not None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.user = None
        if request.session:
            user = await run_in_threadpool(_load_user, request.session)
            if user is None:
                request.session.clear()  # disabled, password reset, or a pre-accounts cookie
            request.state.user = user
        if is_authed(request) or is_public(path):
            return await call_next(request)
        if path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=303)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def rate_limited(ip: str, username: str | None = None) -> bool:
    """Too many recent failures from this IP, or against this username from anywhere."""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE at < ?", (time.time() - WINDOW_S,))
        n = conn.execute("SELECT count(*) FROM login_attempts WHERE ip = ?", (ip,)).fetchone()[0]
        name = users_repo.normalize_username(username)
        if n < MAX_FAILURES and name:
            n = conn.execute("SELECT count(*) FROM login_attempts WHERE username = ?", (name,)).fetchone()[0]
    return n >= MAX_FAILURES


def record_failure(ip: str, username: str | None = None) -> None:
    with db.get_conn() as conn:
        conn.execute("INSERT INTO login_attempts (ip, at, username) VALUES (?, ?, ?)",
                     (ip, time.time(), users_repo.normalize_username(username) or None))


def password_ok(candidate: str) -> bool:
    """Checks the household APP_PASSWORD (first-account bootstrap only). Never matches when it is unset."""
    expected = get_settings().app_password
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())
