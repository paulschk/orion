"""Sign in, sign up, sign out."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import activity, db, security
from ..config import settings
from ..deps import client_ip, fstr, session_optional
from ..templating import render
from ..util import now

router = APIRouter()


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/app"


def _home_for(session) -> str:
    return "/admin" if session["role"] == "admin" else "/app"


@router.get("/login")
async def login_form(request: Request):
    session = session_optional(request)
    if session is not None:
        return RedirectResponse(_home_for(session), status_code=303)
    return render(request, "public/login.html", next_url=_safe_next(fstr(request.query_params, "next", "/app")))


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = fstr(form, "email").lower()
    password = fstr(form, "password")
    next_url = _safe_next(fstr(form, "next", "/app"))

    user = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if user is None or not security.verify_password(password, user["password_hash"]):
        activity.log("auth.failed", target=email, ip=client_ip(request))
        return render(request, "public/login.html", status_code=401,
                      error="Identifiants incorrects.", email=email, next_url=next_url)
    if not user["is_active"]:
        return render(request, "public/login.html", status_code=403,
                      error="Ce compte est suspendu. Contactez l\u2019administrateur.",
                      email=email, next_url=next_url)

    session = security.create_session(user["id"], client_ip(request),
                                     request.headers.get("user-agent", ""))
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user["id"]))
    activity.log("auth.login", user_id=user["id"], ip=client_ip(request))

    response = RedirectResponse(_home_for(user), status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE, session["token"],
        max_age=security.SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
        secure=settings.base_url.startswith("https"),
        path="/",
    )
    if next_url not in ("/app", "/admin"):
        response.headers["Location"] = next_url
    return response


@router.get("/register")
async def register_form(request: Request):
    session = session_optional(request)
    if session is not None:
        return RedirectResponse(_home_for(session), status_code=303)
    plans = db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order")
    return render(request, "public/register.html", plans=plans,
                  signup_open=db.get_setting("signup_open") == "1")


@router.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    email = fstr(form, "email").lower()
    password = fstr(form, "password")
    name = fstr(form, "name")
    company = fstr(form, "company")

    if db.get_setting("signup_open") != "1":
        return render(request, "public/register.html", status_code=403,
                      error="Les inscriptions sont ferm\u00e9es.",
                      plans=db.query("SELECT * FROM plans ORDER BY sort_order"), signup_open=False)

    problems = []
    if "@" not in email or "." not in email.split("@")[-1]:
        problems.append("Adresse e-mail invalide.")
    if len(password) < 10:
        problems.append("Le mot de passe doit contenir au moins 10 caract\u00e8res.")
    if db.one("SELECT 1 FROM users WHERE email = ?", (email,)) is not None:
        problems.append("Un compte existe d\u00e9j\u00e0 avec cette adresse.")

    if problems:
        return render(request, "public/register.html", status_code=400,
                      error=" ".join(problems), email=email, name=name, company=company,
                      plans=db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order"),
                      signup_open=True)

    user_id = db.execute(
        "INSERT INTO users(email, password_hash, name, company, role, is_active, created_at) "
        "VALUES(?, ?, ?, ?, 'client', 1, ?)",
        (email, security.hash_password(password), name or email.split("@")[0], company, now()),
    )
    session = security.create_session(user_id, client_ip(request), request.headers.get("user-agent", ""))
    activity.log("auth.registered", user_id=user_id, target=email, ip=client_ip(request))

    response = RedirectResponse("/app?ok=Compte+cr%C3%A9%C3%A9", status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE, session["token"],
        max_age=security.SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
        secure=settings.base_url.startswith("https"),
        path="/",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(security.SESSION_COOKIE)
    session = security.get_session(token)
    if session is not None:
        activity.log("auth.logout", user_id=session["uid"], ip=client_ip(request))
    security.destroy_session(token)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return response
