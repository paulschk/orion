"""The client dashboard: instances, snapshots, console, sites, invoices."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import activity, billing, db, identity, provision, sites, tasks
from ..config import settings
from ..deps import client_ip, fint, fstr, guard_csrf, require_user
from ..hostinfo import allocation, detect, fits, headroom
from ..incus import IncusError, incus
from ..templating import render
from ..util import days_from_now, now, rand_password, slugify

router = APIRouter(prefix="/app")


# --------------------------------------------------------------------------- helpers

def _owned(instance_id: int, session):
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return None
    if session["role"] != "admin" and instance["user_id"] != session["uid"]:
        return None
    return instance


def _slug_available(candidate: str) -> bool:
    return db.one("SELECT 1 FROM instances WHERE slug = ? AND status != 'destroyed'", (candidate,)) is None


def unique_slug(base: str) -> str:
    slug = slugify(base)
    if _slug_available(slug):
        return slug
    for index in range(2, 200):
        candidate = f"{slug}-{index}"
        if _slug_available(candidate):
            return candidate
    return f"{slug}-{rand_password(4).lower()}"


def instance_context(instance, *, deep: bool = True) -> dict:
    """Everything the instance page needs, degrading gracefully if Incus is down."""
    metrics = {}
    live_status = instance["status"]
    snapshot_rows: list = []
    if deep and instance["status"] not in ("destroyed",):
        try:
            state = incus.state(instance["incus_name"])
            live_status = (state.get("status") or live_status).lower()
            metrics = {
                "memory_used": (state.get("memory") or {}).get("usage", 0),
                "memory_peak": (state.get("memory") or {}).get("usage_peak", 0),
                "cpu_ns": (state.get("cpu") or {}).get("usage", 0),
                "processes": state.get("processes", 0),
            }
            snapshot_rows = incus.snapshots(instance["incus_name"])
        except IncusError:
            pass
    plan_memory = ""
    if instance["plan_id"]:
        row = db.one("SELECT memory_mb FROM plans WHERE id = ?", (instance["plan_id"],))
        if row:
            plan_memory = f"{row['memory_mb']} MB"
    return {
        "instance": instance,
        "metrics": metrics,
        "live_status": live_status,
        "memory_cap": plan_memory,
        "snapshots": snapshot_rows,
        "site_rows": sites.list_sites(instance["id"]),
        "job": tasks.latest_for_instance(instance["id"]),
        "ssh_host": settings.ssh_host,
        "ssh_command": (
            identity.ssh_command(host=settings.ssh_host, port=instance["ssh_port"],
                                 user=instance["ssh_user"])
            if instance["ssh_port"] else ""
        ),
        "ssh_config": (
            identity.ssh_config_block(alias=instance["slug"], host=settings.ssh_host,
                                      port=instance["ssh_port"], user=instance["ssh_user"])
            if instance["ssh_port"] else ""
        ),
    }


# --------------------------------------------------------------------------- dashboard

@router.get("")
async def dashboard(request: Request):
    session = require_user(request)
    instances = db.query(
        "SELECT i.*, p.name AS plan_name, p.vcpu, p.memory_mb, p.disk_gb "
        "FROM instances i LEFT JOIN plans p ON p.id = i.plan_id "
        "WHERE i.user_id = ? AND i.status != 'destroyed' ORDER BY i.id DESC",
        (session["uid"],))
    invoices = billing.for_user_with_context(session["uid"])[:6]
    info = detect()
    return render(request, "app/dashboard.html", session=session,
                  instances=instances, invoices=invoices, info=info,
                  headroom=headroom(info),
                  allocation=allocation(),
                  templates=provision.template_list(),
                  running_jobs=tasks.running())


# --------------------------------------------------------------------------- instances

@router.get("/instances")
async def instances(request: Request):
    session = require_user(request)
    if session["role"] == "admin":
        rows = db.query(
            "SELECT i.*, p.name AS plan_name, u.email FROM instances i "
            "LEFT JOIN plans p ON p.id = i.plan_id JOIN users u ON u.id = i.user_id "
            "WHERE i.status != 'destroyed' ORDER BY i.id DESC")
    else:
        rows = db.query(
            "SELECT i.*, p.name AS plan_name, u.email FROM instances i "
            "LEFT JOIN plans p ON p.id = i.plan_id JOIN users u ON u.id = i.user_id "
            "WHERE i.user_id = ? AND i.status != 'destroyed' ORDER BY i.id DESC",
            (session["uid"],))
    info = detect()
    return render(request, "app/instances.html", session=session, instances=rows, info=info)


@router.get("/instances/new")
async def instance_new(request: Request):
    session = require_user(request)
    info = detect()
    plans = db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order")
    chosen = fstr(request.query_params, "plan", "")
    template_rows = []
    for tpl in provision.template_list():
        template_rows.append({
            "tpl": tpl,
            "capacity": None,
        })
    return render(request, "app/instance_new.html", session=session, plans=plans,
                  templates=template_rows, info=info, headroom=headroom(info),
                  preselect_plan=chosen, preselect_template=fstr(request.query_params, "template", "lemp"))


@router.post("/instances")
async def instance_create(request: Request):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)

    plan_id = fint(form, "plan_id")
    template_slug = fstr(form, "template", "bare")
    name = fstr(form, "name")
    ssh_key = fstr(form, "ssh_public_key")
    use_saved_key = fstr(form, "use_saved_key", "1") == "1"

    plan = db.one("SELECT * FROM plans WHERE id = ? AND is_active = 1", (plan_id,))
    if plan is None:
        return RedirectResponse("/app/instances/new?err=Offre+inconnue", status_code=303)
    tpl = provision.get_template(template_slug)

    if tpl.min_memory_mb and plan["memory_mb"] < tpl.min_memory_mb:
        message = (f"Le mod\u00e8le {tpl.label} demande au moins {tpl.min_memory_mb} MB "
                   f"de RAM ; l\u2019offre {plan['name']} en propose {plan['memory_mb']} MB.")
        return RedirectResponse(f"/app/instances/new?plan={plan['slug']}&err={message}", status_code=303)

    info = detect()
    will_fit, blocked_on = fits(info, plan["vcpu"], plan["memory_mb"], plan["disk_gb"])
    if not will_fit:
        message = (f"Capacit\u00e9 insuffisante sur {info.host_name} : plus assez de {blocked_on} "
                   f"disponible. Contactez l\u2019administrateur.")
        return RedirectResponse(f"/app/instances/new?plan={plan['slug']}&err={message}", status_code=303)

    if ssh_key and "ssh-" in ssh_key.split(" ")[0]:
        db.execute("UPDATE users SET ssh_public_key = ? WHERE id = ?", (ssh_key, session["uid"]))
    elif use_saved_key and session["ssh_public_key"]:
        ssh_key = session["ssh_public_key"]

    slug = unique_slug(name or f"{plan['slug']}-server")
    # Each machine gets its own SSH account and its own identifier: a client is
    # never "orion" on every box, and support can refer to one machine by name.
    taken = {r["ssh_user"] for r in db.query("SELECT ssh_user FROM instances")}
    ssh_user = identity.derive_ssh_user(slug, taken)
    instance_id = db.execute(
        "INSERT INTO instances(user_id, plan_id, name, slug, incus_name, machine_id, template, "
        "status, ssh_user, initial_password, created_at, expires_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)",
        (session["uid"], plan["id"], name or slug, slug,
         provision.incus_name_for(session["uid"], slug), identity.new_machine_id(), tpl.slug,
         ssh_user, rand_password(20), now(),
         days_from_now(plan["period_days"])),
    )

    _, invoice_id = billing.subscribe(session["uid"], plan["id"], instance_id)
    activity.log("instance.created", user_id=session["uid"], target=slug,
                 detail=f"{plan['slug']} / {tpl.slug} / invoice {invoice_id}", ip=client_ip(request))

    tasks.start("provision", instance_id, lambda job_id: provision.provision(job_id, instance_id))
    return RedirectResponse(
        f"/app/instances/{instance_id}?ok=Provisionnement+lanc%C3%A9+-+cela+prend+une+minute",
        status_code=303)


@router.get("/instances/{instance_id}")
async def instance_detail(request: Request, instance_id: int):
    session = require_user(request)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    context = instance_context(instance)
    wp_creds = db.get_setting(f"wp_creds_{instance_id}")
    return render(request, "app/instance_detail.html", session=session,
                  wp_creds=wp_creds, **context)


@router.post("/instances/{instance_id}/power")
async def instance_power(request: Request, instance_id: int):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    action = fstr(form, "action")
    ok, message = provision.power(instance_id, action)
    suffix = "ok=Action+appliqu%C3%A9e" if ok else f"err={message}"
    return RedirectResponse(f"/app/instances/{instance_id}?{suffix}", status_code=303)


@router.post("/instances/{instance_id}/destroy")
async def instance_destroy(request: Request, instance_id: int):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    if fstr(form, "confirm") != instance["slug"]:
        return RedirectResponse(
            f"/app/instances/{instance_id}?err=Confirmation+incorrecte+:+saisissez+le+nom+exact",
            status_code=303)
    tasks.start("destroy", instance_id, lambda job_id: provision.destroy(instance_id))
    return RedirectResponse("/app/instances?ok=Suppression+lanc%C3%A9e", status_code=303)


@router.post("/instances/{instance_id}/snapshots")
async def snapshot_create(request: Request, instance_id: int):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    label = slugify(fstr(form, "label", "") or f"snap-{now()[:10]}")
    try:
        incus.snapshot_create(instance["incus_name"], label)
        activity.log("instance.snapshot", user_id=session["uid"], target=instance["slug"], detail=label)
        return RedirectResponse(f"/app/instances/{instance_id}?ok=Sauvegarde+cr%C3%A9%C3%A9e", status_code=303)
    except IncusError as exc:
        return RedirectResponse(f"/app/instances/{instance_id}?err={exc.detail}", status_code=303)


@router.post("/instances/{instance_id}/snapshots/{snapshot}/restore")
async def snapshot_restore(request: Request, instance_id: int, snapshot: str):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    try:
        incus.snapshot_restore(instance["incus_name"], snapshot)
        activity.log("instance.snapshot_restore", user_id=session["uid"],
                     target=instance["slug"], detail=snapshot)
        return RedirectResponse(
            f"/app/instances/{instance_id}?ok=Restaur%C3%A9+depuis+{snapshot}", status_code=303)
    except IncusError as exc:
        return RedirectResponse(f"/app/instances/{instance_id}?err={exc.detail}", status_code=303)


@router.post("/instances/{instance_id}/snapshots/{snapshot}/delete")
async def snapshot_delete(request: Request, instance_id: int, snapshot: str):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    try:
        incus.snapshot_delete(instance["incus_name"], snapshot)
    except IncusError as exc:
        return RedirectResponse(f"/app/instances/{instance_id}?err={exc.detail}", status_code=303)
    return RedirectResponse(f"/app/instances/{instance_id}?ok=Sauvegarde+supprim%C3%A9e", status_code=303)


@router.post("/instances/{instance_id}/sites")
async def site_add(request: Request, instance_id: int):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)

    domain = fstr(form, "domain").lower().strip()
    upstream = fint(form, "upstream", 80)
    if not domain or "." not in domain:
        return RedirectResponse(f"/app/instances/{instance_id}?err=Nom+de+domaine+invalide", status_code=303)
    if db.one("SELECT 1 FROM sites WHERE domain = ?", (domain,)) is not None:
        return RedirectResponse(f"/app/instances/{instance_id}?err=Ce+domaine+est+d%C3%A9j%C3%A0+rout%C3%A9", status_code=303)

    plan = db.one("SELECT * FROM plans WHERE id = ?", (instance["plan_id"],)) if instance["plan_id"] else None
    current = len(sites.list_sites(instance_id))
    if plan and current >= plan["sites"]:
        return RedirectResponse(
            f"/app/instances/{instance_id}?err=Offre+limit%C3%A9e+%C3%A0+{plan['sites']}+site(s)", status_code=303)

    sites.add_site(instance_id, domain, upstream)
    ok, message = sites.sync_caddy()
    activity.log("site.added", user_id=session["uid"], target=domain, detail=instance["slug"])
    if not ok:
        return RedirectResponse(f"/app/instances/{instance_id}?err=Routage+non+appliqu%C3%A9:+{message}", status_code=303)
    return RedirectResponse(f"/app/instances/{instance_id}?ok=Domaine+ajout%C3%A9", status_code=303)


@router.post("/sites/{site_id}/delete")
async def site_delete(request: Request, site_id: int):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    site = db.one("SELECT * FROM sites WHERE id = ?", (site_id,))
    if site is None:
        return RedirectResponse("/app/instances?err=Route+introuvable", status_code=303)
    instance = _owned(site["instance_id"], session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)
    sites.remove_site(site_id)
    sites.sync_caddy()
    activity.log("site.removed", user_id=session["uid"], target=site["domain"])
    return RedirectResponse(f"/app/instances/{instance['id']}?ok=Domaine+retir%C3%A9", status_code=303)


# --------------------------------------------------------------------------- account

@router.get("/factures")
async def invoices(request: Request):
    session = require_user(request)
    rows = billing.for_user_with_context(session["uid"])
    total_due = sum(r["amount_cents"] for r in rows if r["status"] != "paid")
    return render(request, "app/invoices.html", session=session, invoices=rows, total_due=total_due)


@router.get("/compte")
async def account(request: Request):
    session = require_user(request)
    return render(request, "app/account.html", session=session)


@router.post("/compte")
async def account_save(request: Request):
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    name = fstr(form, "name")
    company = fstr(form, "company")
    ssh_key = fstr(form, "ssh_public_key")
    db.execute("UPDATE users SET name = ?, company = ?, ssh_public_key = ? WHERE id = ?",
               (name, company, ssh_key, session["uid"]))
    activity.log("account.updated", user_id=session["uid"], ip=client_ip(request))
    return RedirectResponse("/app/compte?ok=Profil+enregistr%C3%A9", status_code=303)


@router.post("/compte/password")
async def account_password(request: Request):
    from .. import security

    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    current = fstr(form, "current_password")
    new = fstr(form, "new_password")
    user = db.one("SELECT * FROM users WHERE id = ?", (session["uid"],))
    if not security.verify_password(current, user["password_hash"]):
        return RedirectResponse("/app/compte?err=Mot+de+passe+actuel+incorrect", status_code=303)
    if len(new) < 10:
        return RedirectResponse("/app/compte?err=Le+nouveau+mot+de+passe+est+trop+court", status_code=303)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (security.hash_password(new), session["uid"]))
    activity.log("account.password_changed", user_id=session["uid"], ip=client_ip(request))
    return RedirectResponse("/app/compte?ok=Mot+de+passe+mis+%C3%A0+jour", status_code=303)
