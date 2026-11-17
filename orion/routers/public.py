"""Marketing pages and health endpoints. No session required."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import db, sites
from ..config import settings
from ..deps import session_optional
from ..hostinfo import detect, headroom
from ..templating import render

router = APIRouter()


@router.get("/healthz")
async def healthz():
    from ..incus import incus

    reachable, detail = incus.available()
    return JSONResponse({
        "status": "ok",
        "incus": reachable,
        "incus_detail": detail,
        "host": db.get_setting("host_name"),
        "brand": settings.brand,
    })


@router.get("/")
async def landing(request: Request):
    session = session_optional(request)
    if session is not None:
        return RedirectResponse("/admin" if session["role"] == "admin" else "/app", status_code=303)

    info = detect()
    plans = db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order")
    return render(request, "public/landing.html", plans=plans, info=info,
                  headroom=headroom(info), signup_open=db.get_setting("signup_open") == "1")


@router.get("/tarifs")
async def pricing(request: Request):
    session = session_optional(request)
    info = detect()
    plans = db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order")
    return render(request, "public/pricing.html", plans=plans, info=info,
                  headroom=headroom(info), signup_open=db.get_setting("signup_open") == "1")


@router.get("/robots.txt")
async def robots():
    body = "User-agent: *\nDisallow: /app\nDisallow: /admin\n"
    return JSONResponse(body, media_type="text/plain")
