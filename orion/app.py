"""Orion application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, sites
from .config import settings
from .routers import admin, api, auth, client, public
from .templating import BASE_DIR
from .util import now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("orion")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()

    from .hostinfo import detect, fingerprint

    info = detect(force=True)
    if info.reachable:
        key = fingerprint(info)
        if db.get_setting("host_fingerprint") != key:
            log.info("deployed on a new host: %s (%s)", info.host_name, key)
            db.set_setting("host_fingerprint", key)
            db.set_setting("host_name", info.host_name)
            db.set_setting("host_first_seen", now())
    else:
        log.warning("Incus unreachable at startup: %s", info.error)

    ok, message = sites.sync_caddy()
    log.info("tenant routing: %s (%s)", "applied" if ok else "failed", message)

    yield


app = FastAPI(
    title=f"{settings.app_name} - {settings.brand} hosting panel",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Turn our redirect-flavoured HTTPExceptions into real redirects."""
    location = (exc.headers or {}).get("Location")
    if location and 300 <= exc.status_code < 400:
        return RedirectResponse(location, status_code=exc.status_code)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    from .templating import render

    session = None
    try:
        from .deps import session_optional

        session = session_optional(request)
    except Exception:  # noqa: BLE001
        pass
    return render(request, "error.html", session=session, status_code=exc.status_code,
                  message=exc.detail or "Une erreur est survenue.", code=exc.status_code)


app.include_router(public.router)
app.include_router(auth.router)
app.include_router(client.router)
app.include_router(admin.router)
app.include_router(api.router)
