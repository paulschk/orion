"""Sign in, sign up, sign out."""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import activity, db, security
from ..config import settings
from ..deps import client_ip, fstr, session_optional
from ..templating import render
from ..util import now

router = APIRouter()

# --------------------------------------------------------------- anti-abuse
# The panel put no limit on sign-in attempts, which is exactly what kept it off
# the internet. A counter per address, in memory: the panel runs as one process,
# and a failed attempt costs nobody anything.
_WINDOW_S = 300
_MAX_FAILS = 6
_fails: dict[str, list[float]] = {}


def _retry_after(ip: str) -> int:
    """Seconds this address has left to wait, or 0 when it may try again."""
    if not ip:
        return 0
    moment = time.time()
    recent = [t for t in _fails.get(ip, []) if moment - t < _WINDOW_S]
    if recent:
        _fails[ip] = recent
    else:
        _fails.pop(ip, None)
    if len(recent) < _MAX_FAILS:
        return 0
    return int(_WINDOW_S - (moment - recent[0])) + 1


def _note_failure(ip: str) -> None:
    if ip:
        _fails.setdefault(ip, []).append(time.time())


async def _captcha_ok(token: str, ip: str) -> bool:
    """Ask Cloudflare whether the widget's token is genuine.

    Server side only: the secret is sent from this machine and nowhere else. A
    Cloudflare outage refuses the attempt rather than waving it through.
    """
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            answer = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": settings.turnstile_secret, "response": token,
                      "remoteip": ip or ""})
        return bool(answer.json().get("success"))
    except Exception:  # noqa: BLE001 - a network fault must not open the door
        activity.log("auth.captcha_unreachable", target="cloudflare", ip=ip)
        return False


def _login_page(request: Request, *, status_code: int = 200, error: str = "",
                email: str = "", next_url: str = "/app"):
    """Every render of the sign-in page, so the widget key is never forgotten."""
    return render(request, "public/login.html", status_code=status_code,
                  error=error, email=email, next_url=next_url,
                  turnstile_site_key=settings.turnstile_site_key)


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
    return _login_page(request,
                       next_url=_safe_next(fstr(request.query_params, "next", "/app")))


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = fstr(form, "email").lower()
    password = fstr(form, "password")
    next_url = _safe_next(fstr(form, "next", "/app"))
    ip = client_ip(request)

    wait = _retry_after(ip)
    if wait:
        activity.log("auth.throttled", target=email, ip=ip)
        return _login_page(
            request, status_code=429, email=email, next_url=next_url,
            error="Trop de tentatives. Réessayez dans {} minute{}.".format(
                max(1, wait // 60), "s" if wait >= 120 else ""))

    if settings.turnstile_secret and not await _captcha_ok(
            fstr(form, "cf-turnstile-response"), ip):
        _note_failure(ip)
        activity.log("auth.captcha", target=email, ip=ip)
        return _login_page(
            request, status_code=403, email=email, next_url=next_url,
            error="La vérification « je ne suis pas un robot » a échoué. Réessayez.")

    user = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if user is None or not security.verify_password(password, user["password_hash"]):
        _note_failure(ip)
        activity.log("auth.failed", target=email, ip=ip)
        return _login_page(request, status_code=401, email=email, next_url=next_url,
                           error="Identifiants incorrects.")
    if not user["is_active"]:
        return _login_page(request, status_code=403, email=email, next_url=next_url,
                           error="Ce compte est suspendu. Contactez l\u2019administrateur.")

    _fails.pop(ip, None)          # une connexion réussie efface l'ardoise
    session = security.create_session(user["id"], ip,
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
