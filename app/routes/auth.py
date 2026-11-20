from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import auth
from ..web import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    if auth.is_authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = auth.client_ip(request)
    if auth.rate_limited(ip):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Too many attempts. Try again in a few minutes."}, status_code=429)
    if not auth.password_ok(password):
        auth.record_failure(ip)
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
