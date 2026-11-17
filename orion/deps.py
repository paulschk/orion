"""Request dependencies: sessions, roles, CSRF, form reading."""

from __future__ import annotations

from fastapi import HTTPException, Request

from . import security


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def session_optional(request: Request):
    return security.get_session(request.cookies.get(security.SESSION_COOKIE))


def require_user(request: Request):
    session = session_optional(request)
    if session is None:
        target = request.url.path
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={target}"})
    return session


def require_admin(request: Request):
    session = require_user(request)
    if session["role"] != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/app?err=R\u00e9serv\u00e9+aux+administrateurs"})
    return session


def guard_csrf(request: Request, session, form) -> None:
    """Raise 400 when a state-changing request has no valid CSRF token."""
    submitted = form.get("csrf") or request.headers.get("x-csrf-token")
    if not security.csrf_ok(session, submitted):
        raise HTTPException(status_code=400, detail="Jeton CSRF invalide ou expir\u00e9.")


def fstr(form, key: str, default: str = "") -> str:
    value = form.get(key)
    return (value if isinstance(value, str) else default).strip()


def fint(form, key: str, default: int = 0) -> int:
    try:
        return int(fstr(form, key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def fbool(form, key: str) -> bool:
    return fstr(form, key).lower() in ("1", "true", "on", "yes")
