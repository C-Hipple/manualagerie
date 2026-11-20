import hmac
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import db
from .config import get_settings

PUBLIC_PREFIXES = ("/login", "/healthz", "/static/")
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


def check_config() -> None:
    s = get_settings()
    if not s.app_password and not s.auth_disabled:
        raise RuntimeError("APP_PASSWORD must be set (or AUTH_DISABLED=true for local development).")


def is_authed(request: Request) -> bool:
    return get_settings().auth_disabled or request.session.get("auth") is True


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if is_authed(request) or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        if path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=303)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def rate_limited(ip: str) -> bool:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE at < ?", (time.time() - WINDOW_S,))
        n = conn.execute("SELECT count(*) FROM login_attempts WHERE ip = ?", (ip,)).fetchone()[0]
    return n >= MAX_FAILURES


def record_failure(ip: str) -> None:
    with db.get_conn() as conn:
        conn.execute("INSERT INTO login_attempts (ip, at) VALUES (?, ?)", (ip, time.time()))


def password_ok(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), get_settings().app_password.encode())
