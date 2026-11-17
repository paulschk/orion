"""One Jinja2 environment, shared by every router."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import settings
from .util import human_bytes, human_mb, money, pretty_date, pretty_datetime, slugify

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

templates.env.filters.update({
    "money": money,
    "human_mb": human_mb,
    "human_bytes": human_bytes,
    "pretty_date": pretty_date,
    "pretty_datetime": pretty_datetime,
    "slugify": slugify,
})

templates.env.globals.update(
    # Bump the ?v= on every release, or returning visitors keep the old CSS/JS.
    asset_version=__version__,
    brand=settings.brand,
    app_name=settings.app_name,
    domain=settings.domain,
    base_url=settings.base_url,
    currency=settings.currency,
    billing_mode=settings.billing_mode,
)


TEMPLATE_STATUS = {
    "provisioning": ("En cours", "warn"),
    "running": ("En ligne", "ok"),
    "stopped": ("Arr\u00eat\u00e9e", "idle"),
    "suspended": ("Suspendue", "warn"),
    "error": ("Erreur", "bad"),
    "destroyed": ("D\u00e9truite", "muted"),
}


def _nav_counts(session) -> dict:
    """Sidebar counters. Two cheap indexed counts, and only for admins."""
    if session is None or session["role"] != "admin":
        return {"nav_instances": "", "nav_clients": "", "nav_sites": ""}
    from . import db

    return {
        "nav_instances": db.scalar(
            "SELECT COUNT(*) FROM instances WHERE status != 'destroyed'", default=0),
        "nav_clients": db.scalar(
            "SELECT COUNT(*) FROM users WHERE role = 'client'", default=0),
        "nav_sites": db.scalar("SELECT COUNT(*) FROM sites WHERE enabled = 1", default=0),
    }


def render(request: Request, template: str, *, session=None, status_code: int = 200, **context):
    """Render with the common context every page needs."""
    base = {
        "request": request,
        "user": session,
        "csrf": session["csrf"] if session is not None else "",
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
        "path": request.url.path,
        "status_labels": TEMPLATE_STATUS,
    }
    base.update(_nav_counts(session))
    base.update(context)
    return templates.TemplateResponse(request, template, base, status_code=status_code)
