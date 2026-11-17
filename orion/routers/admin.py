"""The admin dashboard: the host, the clients, the money, the routing."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import activity, billing, db, identity, provision, security, sites, tasks
from ..config import settings
from ..deps import client_ip, fint, fstr, guard_csrf, require_admin
from ..hostinfo import allocation, detect, headroom, plan_capacity
from ..incus import IncusError, incus
from ..templating import render
from ..util import now, rand_password

router = APIRouter(prefix="/admin")


@router.get("")
async def dashboard(request: Request):
    session = require_admin(request)
    info = detect()
    alloc = allocation()
    counters = {
        "clients": db.scalar("SELECT COUNT(*) FROM users WHERE role = 'client'", default=0),
        "instances": db.scalar("SELECT COUNT(*) FROM instances WHERE status != 'destroyed'", default=0),
        "running": db.scalar("SELECT COUNT(*) FROM instances WHERE status = 'running'", default=0),
        "errors": db.scalar("SELECT COUNT(*) FROM instances WHERE status = 'error'", default=0),
        "unpaid": db.scalar("SELECT COUNT(*) FROM invoices WHERE status != 'paid'", default=0),
        "outstanding": db.scalar("SELECT COALESCE(SUM(amount_cents), 0) FROM invoices WHERE status != 'paid'", default=0),
        "sites": db.scalar("SELECT COUNT(*) FROM sites WHERE enabled = 1", default=0),
    }
    plans = db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order")
    capacities = {plan["id"]: plan_capacity(info, plan) for plan in plans}
    caddy_ok, caddy_detail = sites.caddy_health()
    return render(request, "admin/dashboard.html", session=session, info=info, alloc=alloc,
                  headroom=headroom(info), counters=counters, plans=plans, capacities=capacities,
                  caddy_ok=caddy_ok, caddy_detail=caddy_detail,
                  activity=activity.recent(20), running_jobs=tasks.running())


# --------------------------------------------------------------------------- instances

@router.get("/instances")
async def instances(request: Request):
    session = require_admin(request)
    rows = db.query(
        "SELECT i.*, p.name AS plan_name, u.email FROM instances i "
        "LEFT JOIN plans p ON p.id = i.plan_id JOIN users u ON u.id = i.user_id "
        "ORDER BY i.id DESC")
    info = detect()
    return render(request, "admin/instances.html", session=session, instances=rows, info=info)


@router.get("/instances/{instance_id}")
async def instance_detail(request: Request, instance_id: int):
    from .client import instance_context

    session = require_admin(request)
    instance = db.one("SELECT i.*, p.name AS plan_name, u.email FROM instances i "
                      "LEFT JOIN plans p ON p.id = i.plan_id JOIN users u ON u.id = i.user_id "
                      "WHERE i.id = ?", (instance_id,))
    if instance is None:
        return RedirectResponse("/admin/instances?err=Instance+introuvable", status_code=303)
    context = instance_context(instance)
    return render(request, "admin/instance_detail.html", session=session, **context)


@router.post("/instances/{instance_id}/power")
async def instance_power(request: Request, instance_id: int):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    action = fstr(form, "action")
    ok, message = provision.power(instance_id, action)
    suffix = "ok=Action+appliqu%C3%A9e" if ok else f"err={message}"
    return RedirectResponse(f"/admin/instances/{instance_id}?{suffix}", status_code=303)


@router.post("/instances/{instance_id}/destroy")
async def instance_destroy(request: Request, instance_id: int):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return RedirectResponse("/admin/instances?err=Instance+introuvable", status_code=303)
    if fstr(form, "confirm") != instance["slug"]:
        return RedirectResponse(
            f"/admin/instances/{instance_id}?err=Confirmation+incorrecte", status_code=303)
    tasks.start("destroy", instance_id, lambda job_id: provision.destroy(instance_id))
    return RedirectResponse("/admin/instances?ok=Suppression+lanc%C3%A9e", status_code=303)


@router.post("/instances/{instance_id}/repair")
async def instance_repair(request: Request, instance_id: int):
    """Re-apply limits, the SSH port mapping and the web route."""
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = db.one("SELECT i.*, p.vcpu, p.memory_mb, p.disk_gb FROM instances i "
                      "LEFT JOIN plans p ON p.id = i.plan_id WHERE i.id = ?", (instance_id,))
    if instance is None:
        return RedirectResponse("/admin/instances?err=Instance+introuvable", status_code=303)
    notes = []
    try:
        if instance["vcpu"]:
            incus.apply_limits(instance["incus_name"], vcpu=instance["vcpu"],
                               memory_mb=instance["memory_mb"], disk_gb=instance["disk_gb"])
            notes.append("limites")
        port = instance["ssh_port"] or provision.allocate_ssh_port()
        incus.set_ssh_proxy(instance["incus_name"], port)
        db.execute("UPDATE instances SET ssh_port = ? WHERE id = ?", (port, instance_id))
        notes.append(f"port {port}")

        # An instance created before per-machine identities still carries the
        # shared default account. Repair is the deliberate migration path.
        target = instance["ssh_user"]
        if not target or target == settings.ssh_user:
            taken = {r["ssh_user"] for r in db.query(
                "SELECT ssh_user FROM instances WHERE id != ?", (instance_id,))}
            target = identity.derive_ssh_user(instance["slug"], taken)
        account, note = provision.ensure_ssh_account(instance, target)
        if account and account != instance["ssh_user"]:
            db.execute("UPDATE instances SET ssh_user = ? WHERE id = ?", (account, instance_id))
        notes.append(note)
        ip = incus.primary_ip(instance["incus_name"])
        if ip:
            db.execute("UPDATE instances SET container_ip = ? WHERE id = ?", (ip, instance_id))
        ok, message = sites.sync_caddy()
        notes.append("routage" + ("" if ok else f" ({message})"))
        activity.log("instance.repaired", user_id=session["uid"], target=instance["slug"],
                     detail=", ".join(notes))
        return RedirectResponse(
            f"/admin/instances/{instance_id}?ok=R%C3%A9par%C3%A9+:+{',+'.join(notes)}", status_code=303)
    except IncusError as exc:
        return RedirectResponse(f"/admin/instances/{instance_id}?err={exc.detail}", status_code=303)


# --------------------------------------------------------------------------- clients

@router.get("/clients")
async def clients(request: Request):
    session = require_admin(request)
    rows = db.query(
        "SELECT u.*, "
        "  (SELECT COUNT(*) FROM instances i WHERE i.user_id = u.id AND i.status != 'destroyed') AS instance_count, "
        "  (SELECT COALESCE(SUM(inv.amount_cents), 0) FROM invoices inv "
        "     WHERE inv.user_id = u.id AND inv.status != 'paid') AS due_cents "
        "FROM users u ORDER BY u.role, u.id")
    return render(request, "admin/clients.html", session=session, clients=rows)


@router.get("/clients/{user_id}")
async def client_detail(request: Request, user_id: int):
    session = require_admin(request)
    client = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if client is None:
        return RedirectResponse("/admin/clients?err=Client+introuvable", status_code=303)
    rows = db.query(
        "SELECT i.*, p.name AS plan_name FROM instances i LEFT JOIN plans p ON p.id = i.plan_id "
        "WHERE i.user_id = ? ORDER BY i.id DESC", (user_id,))
    invoices = billing.for_user_with_context(user_id)
    return render(request, "admin/client_detail.html", session=session, client=client,
                  instances=rows, invoices=invoices,
                  activity=activity.recent(30, user_id=user_id))


@router.post("/clients")
async def client_create(request: Request):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    email = fstr(form, "email").lower()
    name = fstr(form, "name")
    company = fstr(form, "company")
    role = fstr(form, "role", "client")
    password = fstr(form, "password") or rand_password(16)

    if "@" not in email:
        return RedirectResponse("/admin/clients?err=Adresse+e-mail+invalide", status_code=303)
    if db.one("SELECT 1 FROM users WHERE email = ?", (email,)) is not None:
        return RedirectResponse("/admin/clients?err=Ce+compte+existe+d%C3%A9j%C3%A0", status_code=303)

    user_id = db.execute(
        "INSERT INTO users(email, password_hash, name, company, role, is_active, created_at) "
        "VALUES(?, ?, ?, ?, ?, 1, ?)",
        (email, security.hash_password(password), name or email.split("@")[0], company,
         "admin" if role == "admin" else "client", now()))
    activity.log("client.created", user_id=session["uid"], target=email, detail=f"role={role}")
    return RedirectResponse(
        f"/admin/clients/{user_id}?ok=Compte+cr%C3%A9%C3%A9+-+mot+de+passe+initial+:+{password}",
        status_code=303)


@router.post("/clients/{user_id}")
async def client_update(request: Request, user_id: int):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    client = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if client is None:
        return RedirectResponse("/admin/clients?err=Client+introuvable", status_code=303)

    name = fstr(form, "name")
    company = fstr(form, "company")
    role = fstr(form, "role", client["role"])
    is_active = 1 if fstr(form, "is_active") == "on" else 0
    if client["id"] == session["uid"] and not is_active:
        return RedirectResponse(
            f"/admin/clients/{user_id}?err=Vous+ne+pouvez+pas+vous+suspendre+vous-m%C3%AAme",
            status_code=303)

    db.execute("UPDATE users SET name = ?, company = ?, role = ?, is_active = ? WHERE id = ?",
               (name, company, "admin" if role == "admin" else "client", is_active, user_id))
    activity.log("client.updated", user_id=session["uid"], target=client["email"],
                 detail=f"role={role} active={is_active}")
    return RedirectResponse(f"/admin/clients/{user_id}?ok=Compte+mis+%C3%A0+jour", status_code=303)


@router.post("/clients/{user_id}/password")
async def client_password(request: Request, user_id: int):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    password = fstr(form, "password") or rand_password(16)
    if len(password) < 10:
        return RedirectResponse(
            f"/admin/clients/{user_id}?err=Mot+de+passe+trop+court+(10+caract%C3%A8res)", status_code=303)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (security.hash_password(password), user_id))
    activity.log("client.password_reset", user_id=session["uid"], target=f"user:{user_id}")
    return RedirectResponse(
        f"/admin/clients/{user_id}?ok=Nouveau+mot+de+passe+:+{password}", status_code=303)


# --------------------------------------------------------------------------- plans

@router.get("/plans")
async def plans(request: Request):
    session = require_admin(request)
    info = detect()
    rows = db.query("SELECT * FROM plans ORDER BY sort_order")
    capacities = {plan["id"]: plan_capacity(info, plan) for plan in rows}
    return render(request, "admin/plans.html", session=session, plans=rows, info=info,
                  capacities=capacities)


@router.post("/plans")
async def plan_save(request: Request):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    plan_id = fint(form, "plan_id")
    values = {
        "name": fstr(form, "name"),
        "vcpu": fint(form, "vcpu", 1),
        "memory_mb": fint(form, "memory_mb", 512),
        "disk_gb": fint(form, "disk_gb", 5),
        "price_cents": fint(form, "price_cents", 0),
        "sites": fint(form, "sites", 1),
        "description": fstr(form, "description"),
        "is_active": 1 if fstr(form, "is_active", "on") == "on" else 0,
        "sort_order": fint(form, "sort_order", 100),
    }
    if plan_id:
        db.execute(
            "UPDATE plans SET name=:name, vcpu=:vcpu, memory_mb=:memory_mb, disk_gb=:disk_gb, "
            "price_cents=:price_cents, sites=:sites, description=:description, "
            "is_active=:is_active, sort_order=:sort_order WHERE id=:id",
            {**values, "id": plan_id})
        activity.log("plan.updated", user_id=session["uid"], target=str(plan_id))
    else:
        slug = fstr(form, "slug") or values["name"]
        db.execute(
            "INSERT INTO plans(slug, name, vcpu, memory_mb, disk_gb, price_cents, currency, "
            "period_days, sites, description, features, is_active, sort_order) "
            "VALUES(:slug, :name, :vcpu, :memory_mb, :disk_gb, :price_cents, :currency, "
            "30, :sites, :description, '', :is_active, :sort_order)",
            {**values, "slug": slug, "currency": settings.currency})
        activity.log("plan.created", user_id=session["uid"], target=slug)
    return RedirectResponse("/admin/plans?ok=Offre+enregistr%C3%A9e", status_code=303)


# --------------------------------------------------------------------------- invoices

@router.get("/invoices")
async def invoices(request: Request):
    session = require_admin(request)
    rows = billing.all_invoices()
    outstanding = sum(r["amount_cents"] for r in rows if r["status"] != "paid")
    return render(request, "admin/invoices.html", session=session, invoices=rows,
                  outstanding=outstanding)


@router.post("/invoices/{invoice_id}/paid")
async def invoice_paid(request: Request, invoice_id: int):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    billing.mark_paid(invoice_id, session["uid"])
    return RedirectResponse("/admin/invoices?ok=Facture+marqu%C3%A9e+pay%C3%A9e", status_code=303)


@router.post("/invoices/{invoice_id}/void")
async def invoice_void(request: Request, invoice_id: int):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    db.execute("UPDATE invoices SET status = 'void' WHERE id = ?", (invoice_id,))
    activity.log("billing.invoice_voided", user_id=session["uid"], target=f"invoice:{invoice_id}")
    return RedirectResponse("/admin/invoices?ok=Facture+annul%C3%A9e", status_code=303)


# --------------------------------------------------------------------------- routing, activity, settings

@router.get("/sites")
async def routing(request: Request):
    session = require_admin(request)
    rows = sites.all_sites()
    ok, detail = sites.caddy_health()
    return render(request, "admin/sites.html", session=session, sites=rows,
                  caddy_ok=ok, caddy_detail=detail, domain=settings.domain)


@router.post("/sites/sync")
async def routing_sync(request: Request):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    ok, message = sites.sync_caddy()
    activity.log("routing.synced", user_id=session["uid"], detail=message)
    suffix = "ok=Routage+appliqu%C3%A9" if ok else f"err={message}"
    return RedirectResponse(f"/admin/sites?{suffix}", status_code=303)


@router.get("/activity")
async def activity_log(request: Request):
    session = require_admin(request)
    return render(request, "admin/activity.html", session=session, activity=activity.recent(300))


@router.get("/settings")
async def settings_page(request: Request):
    session = require_admin(request)
    info = detect()
    return render(request, "admin/settings.html", session=session, info=info,
                  cfg=settings,
                  signup_open=db.get_setting("signup_open") == "1",
                  host_first_seen=db.get_setting("host_first_seen"),
                  host_fingerprint=db.get_setting("host_fingerprint"))


@router.post("/settings")
async def settings_save(request: Request):
    session = require_admin(request)
    form = await request.form()
    guard_csrf(request, session, form)
    db.set_setting("signup_open", "1" if fstr(form, "signup_open") == "on" else "0")
    if fstr(form, "resync") == "1":
        sites.sync_caddy()
    activity.log("settings.updated", user_id=session["uid"])
    return RedirectResponse("/admin/settings?ok=R%C3%A9glages+enregistr%C3%A9s", status_code=303)
