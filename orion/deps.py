"""Request dependencies: sessions, roles, CSRF, form reading."""

from __future__ import annotations

from fastapi import HTTPException, Request

from . import security


def client_ip(request: Request) -> str:
    """The visitor's address, as far as the edge can honestly tell.

    Behind Cloudflare the true address arrives in `CF-Connecting-IP`, which
    Cloudflare overwrites on every request; the origin is reachable only through
    Cloudflare or the LAN, so it can be trusted there. `X-Forwarded-For` is the
    edge Caddy's chain and is the fallback, not the first choice: behind a proxy
    it would collapse every visitor onto the proxy's own address.
    """
    from_cloudflare = request.headers.get("cf-connecting-ip", "").strip()
    if from_cloudflare:
        return from_cloudflare
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
        # Une requête htmx ne doit pas voir la page de connexion atterrir dans la
        # coquille du panneau : htmx suivrait un 303 et remplacerait la cible du
        # swap par la page publique. `HX-Redirect` demande au navigateur une
        # vraie navigation, ce qui est le comportement voulu. C'est le seul
        # endroit où le panneau renvoie hors de sa propre coquille.
        if request.headers.get("HX-Request"):
            raise HTTPException(status_code=200, headers={"HX-Redirect": f"/login?next={target}"})
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
