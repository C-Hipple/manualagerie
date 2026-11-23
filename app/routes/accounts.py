"""Account settings: the admin's user list, and "change my password" for everyone.

CSRF: like every other form in the app, these rely on the session cookie being SameSite=Lax, so a
cross-site POST arrives without the cookie and is redirected to /login.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import auth, db
from ..repo import users as users_repo
from ..web import templates
from .auth import TOO_MANY

router = APIRouter()
admin = APIRouter(dependencies=[Depends(auth.require_admin)])

MESSAGES = {
    "added": "User added.",
    "password": "Password reset. Their existing sessions have been signed out.",
    "disabled": "User disabled and signed out.",
    "enabled": "User enabled.",
    "admin": "Admin rights updated.",
}
ERRORS = {
    "last_admin": "That is the last admin. Make someone else an admin first.",
    "taken": "That username is already taken.",
}


def _users_page(request: Request, error: str | None = None, status_code: int = 200, form: dict | None = None):
    with db.get_conn() as conn:
        users = users_repo.list_all(conn)
    q = request.query_params
    return templates.TemplateResponse(request, "users.html", {
        "users": users, "me": auth.current_user_id(request), "form": form or {},
        "error": error or ERRORS.get(q.get("error", "")), "message": MESSAGES.get(q.get("ok", "")),
    }, status_code=status_code)


def _get_or_404(conn, user_id: int):
    u = users_repo.get(conn, user_id)
    if u is None:
        raise HTTPException(404, "User not found")
    return u


def _back(ok: str | None = None, error: str | None = None):
    return RedirectResponse(f"/settings/users?{'error=' + error if error else 'ok=' + (ok or '')}", status_code=303)


def _refresh_own_session(request: Request, user_id: int) -> None:
    """After an admin changes their own account, keep them signed in on a fresh session."""
    if auth.current_user_id(request) == user_id:
        with db.get_conn() as conn:
            u = users_repo.get(conn, user_id)
        if u and u["disabled_at"] is None:
            auth.start_session(request, u)


@admin.get("/settings/users")
def users_page(request: Request):
    return _users_page(request)


@admin.post("/settings/users")
def user_add(request: Request, username: str = Form(""), display_name: str = Form(""),
             password: str = Form(""), is_admin: str = Form("")):
    username = users_repo.normalize_username(username)
    display_name = " ".join(display_name.split()) or username
    form = {"username": username, "display_name": display_name}
    error = users_repo.validate(username, display_name, password)
    if error:
        return _users_page(request, error, 400, form)
    with db.get_conn() as conn:
        if users_repo.get_by_username(conn, username):
            return _users_page(request, ERRORS["taken"], 400, form)
        users_repo.create(conn, username, display_name, password, is_admin=bool(is_admin))
    return _back("added")


@admin.post("/settings/users/{user_id}/password")
def user_reset_password(request: Request, user_id: int, password: str = Form("")):
    error = users_repo.password_error(password)
    if error:
        return _users_page(request, error, 400)
    with db.get_conn() as conn:
        _get_or_404(conn, user_id)
        users_repo.set_password(conn, user_id, password)
    _refresh_own_session(request, user_id)
    return _back("password")


@admin.post("/settings/users/{user_id}/disable")
def user_disable(user_id: int):
    with db.get_conn() as conn:
        _get_or_404(conn, user_id)
        if users_repo.is_last_admin(conn, user_id):
            return _back(error="last_admin")
        users_repo.set_disabled(conn, user_id, True)
    return _back("disabled")


@admin.post("/settings/users/{user_id}/enable")
def user_enable(user_id: int):
    with db.get_conn() as conn:
        _get_or_404(conn, user_id)
        users_repo.set_disabled(conn, user_id, False)
    return _back("enabled")


@admin.post("/settings/users/{user_id}/admin")
def user_toggle_admin(user_id: int):
    with db.get_conn() as conn:
        u = _get_or_404(conn, user_id)
        if u["is_admin"] and users_repo.is_last_admin(conn, user_id):
            return _back(error="last_admin")
        users_repo.set_admin(conn, user_id, not u["is_admin"])
    return _back("admin")


@router.get("/settings/password")
def password_page(request: Request):
    return templates.TemplateResponse(request, "password.html", {
        "error": None, "done": request.query_params.get("ok") is not None})


@router.post("/settings/password")
def password_change(request: Request, current: str = Form(""), password: str = Form(""),
                    password2: str = Form("")):
    me = auth.current_user(request)
    if me is None:  # AUTH_DISABLED: there is no account to change
        raise HTTPException(400, "Not signed in to an account")
    ip = auth.client_ip(request)

    def fail(msg: str, code: int):
        return templates.TemplateResponse(request, "password.html", {"error": msg, "done": False}, status_code=code)

    if auth.rate_limited(ip, me["username"]):
        return fail(TOO_MANY, 429)
    with db.get_conn() as conn:
        ok = users_repo.authenticate(conn, me["username"], current) is not None
    if not ok:
        auth.record_failure(ip, me["username"])
        return fail("Your current password is wrong.", 401)
    error = users_repo.password_error(password)
    if error is None and password != password2:
        error = "The two new passwords don't match."
    if error:
        return fail(error, 400)
    with db.get_conn() as conn:
        users_repo.set_password(conn, me["id"], password)  # signs out every other session
    _refresh_own_session(request, me["id"])
    return RedirectResponse("/settings/password?ok=1", status_code=303)


router.include_router(admin)
