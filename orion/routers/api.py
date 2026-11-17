"""JSON and HTML fragments used by the dashboards."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import db, sites
from ..deps import require_user, session_optional
from ..hostinfo import allocation, detect, headroom
from ..incus import IncusError, incus
from ..templating import render

router = APIRouter(prefix="/api")


@router.get("/host")
async def host(request: Request):
    session = require_user(request)
    if session["role"] != "admin":
        return JSONResponse({"error": "admin only"}, status_code=403)
    info = detect(force=True)
    return JSONResponse({
        "host": info.as_dict(),
        "allocation": allocation().as_dict(),
        "headroom": headroom(info).as_dict(),
    })


@router.get("/instances/{instance_id}/status")
async def instance_status(request: Request, instance_id: int):
    session = require_user(request)
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if session["role"] != "admin" and instance["user_id"] != session["uid"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    payload = {
        "status": instance["status"],
        "ip": instance["container_ip"],
        "error": instance["error"],
    }
    try:
        state = incus.state(instance["incus_name"])
        payload["live_status"] = (state.get("status") or "").lower()
        payload["memory_used"] = (state.get("memory") or {}).get("usage", 0)
        payload["cpu_ns"] = (state.get("cpu") or {}).get("usage", 0)
    except IncusError as exc:
        payload["live_status"] = "unreachable"
        payload["detail"] = exc.detail
    return JSONResponse(payload)


@router.get("/instances/{instance_id}/partial")
async def instance_partial(request: Request, instance_id: int):
    """HTMX fragment: the live status block on an instance page."""
    session = require_user(request)
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if session["role"] != "admin" and instance["user_id"] != session["uid"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    metrics = {}
    live_status = instance["status"]
    try:
        state = incus.state(instance["incus_name"])
        live_status = (state.get("status") or live_status).lower()
        metrics = {
            "memory_used": (state.get("memory") or {}).get("usage", 0),
            "memory_peak": (state.get("memory") or {}).get("usage_peak", 0),
            "cpu_ns": (state.get("cpu") or {}).get("usage", 0),
            "processes": state.get("processes", 0),
        }
    except IncusError:
        pass

    job = db.one("SELECT * FROM jobs WHERE instance_id = ? ORDER BY id DESC LIMIT 1", (instance_id,))
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    plan = db.one("SELECT memory_mb FROM plans WHERE id = ?", (instance["plan_id"],)) if instance["plan_id"] else None
    steps = []
    if job and job["log"]:
        steps = [l[3:].strip() for l in job["log"].splitlines()
                 if l.startswith("== ") and l[3:].strip()]
    return render(request, "partials/instance_status.html", session=session,
                  instance=instance, metrics=metrics, live_status=live_status, job=job,
                  steps=steps,
                  memory_cap=(f"{plan['memory_mb']} MB" if plan else ""))


@router.post("/routing/sync")
async def routing_sync(request: Request):
    session = require_user(request)
    if session["role"] != "admin":
        return JSONResponse({"error": "admin only"}, status_code=403)
    ok, message = sites.sync_caddy()
    return JSONResponse({"ok": ok, "message": message})
