from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import auth, db
from ..repo import users as users_repo
from ..web import templates

router = APIRouter()

TOO_MANY = "Too many attempts. Try again in a few minutes."


def _login_page(request: Request, error: str | None = None, status_code: int = 200, **ctx):
    with db.get_conn() as conn:
        setup = users_repo.count(conn) == 0
    return templates.TemplateResponse(request, "login.html", {"error": error, "setup": setup, **ctx},
                                      status_code=status_code)


@router.get("/login")
def login_page(request: Request):
    if auth.is_authed(request):
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = auth.client_ip(request)
    if auth.rate_limited(ip, username):
        return _login_page(request, TOO_MANY, 429, username=username)
    with db.get_conn() as conn:
        user = users_repo.authenticate(conn, username, password) if username and password else None
    if user is None:
        auth.record_failure(ip, username)
        return _login_page(request, "Wrong username or password.", 401, username=username)
    auth.start_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/login/setup")
def setup_first_account(request: Request, app_password: str = Form(""), username: str = Form(""),
                        display_name: str = Form(""), password: str = Form(""), password2: str = Form("")):
    """Create the first (admin) account. Only while no account exists, and only with APP_PASSWORD."""
    ip = auth.client_ip(request)
    username = users_repo.normalize_username(username)
    display_name = " ".join(display_name.split()) or username
    form = {"username": username, "display_name": display_name}
    if auth.rate_limited(ip):
        return _login_page(request, TOO_MANY, 429, **form)
    if not auth.password_ok(app_password):
        auth.record_failure(ip)
        return _login_page(request, "Wrong household password (APP_PASSWORD).", 401, **form)
    error = users_repo.validate(username, display_name, password)
    if error is None and password != password2:
        error = "The two passwords don't match."
    if error:
        return _login_page(request, error, 400, **form)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")  # two racing setups can't both see an empty table
        if users_repo.count(conn):
            return RedirectResponse("/login", status_code=303)
        uid = users_repo.create(conn, username, display_name, password, is_admin=True)
        user = users_repo.get(conn, uid)
    auth.start_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
