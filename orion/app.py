"""Orion application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import activity, db, incus, sites, tasks
from .config import settings
from .routers import admin, api, auth, client, public, terminal
from .templating import BASE_DIR
from .util import now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("orion")


def reconcile_provisions() -> int:
    """Settle rows a restart left saying `provisioning`.

    The job that owned them died with the panel, so nothing will ever write
    them again: the instance shows a loader that cannot finish, reports itself
    as not running, gives no live figure and keeps its vCPU out of the sellable
    pool. The daemon is the authority on what is actually there — which is why
    this runs only once it has answered.
    """
    settled = 0
    for row in db.query("SELECT * FROM instances WHERE status = 'provisioning'"):
        job = tasks.latest_for_instance(row["id"])
        if job and job["status"] in ("queued", "running"):
            continue                      # a live job still owns this row
        try:
            state = incus.state(row["incus_name"])
        except incus.IncusError:
            state = {}
        status, message = tasks.settle_provisioning(state)
        db.execute("UPDATE instances SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                   (status, message, now(), row["id"]))
        activity.log("provisioning.reconcile", target=row["slug"],
                     detail=f"provisionnement interrompu — statut porté à {status}")
        settled += 1
    return settled


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

    # Only once the daemon has answered: with it silent, every stuck row would
    # look like an interrupted provision and be marked in error.
    if info.reachable:
        settled = reconcile_provisions()
        if settled:
            log.info("settled %d interrupted provisioning row(s)", settled)

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
    # Un en-tête posé par l'exception doit survivre au rendu : `HX-Redirect` est
    # ce qui demande à htmx une navigation réelle au lieu de remplacer sa cible
    # par la page reçue. Pour ce cas le corps ne compte pas.
    if (exc.headers or {}).get("HX-Redirect"):
        return JSONResponse({}, status_code=exc.status_code, headers=exc.headers)
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
app.include_router(terminal.router)
