"""The client dashboard: instances, snapshots, console, sites, invoices."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from urllib.parse import quote

from .. import activity, billing, db, identity, metrics, provision, services, sites, tasks
from ..config import settings
from ..deps import client_ip, fint, fstr, guard_csrf, require_user
from ..hostinfo import detect, fits, headroom
from ..incus import IncusError, incus
from ..templating import render
from ..util import (days_from_now, friendly_name, now, rand_password, slugify,
                     snapshot_label)

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
    """Slugs must be unique across *all* rows, destroyed instances included.

    The container name is derived from the slug and instances.incus_name is
    UNIQUE, so reusing the name of a destroyed instance used to make the insert
    fail with a 500 rather than quietly picking a free name.
    """
    return db.one("SELECT 1 FROM instances WHERE slug = ?", (candidate,)) is None


def free_slug(preferred: str) -> str:
    """The address must never count its neighbours.

    A client may name an instance whatever they like, and that name is kept -
    but the slug becomes the address their visitors see, and a counter in it
    ("wordpress-2") publishes how many others exist and roughly when this one
    was made. So when the chosen name is taken, a fresh random pair is drawn
    instead of a suffix: the client keeps the name they typed, and can rename
    the address from the panel whenever they want.
    """
    slug = slugify(preferred)
    if _slug_available(slug):
        return slug
    for _ in range(20):
        candidate = friendly_name()
        if _slug_available(candidate):
            return candidate
    # 20 draws from ~1500 pairs all collided: something is wrong with the table,
    # and a counted suffix is better than a 500.
    return unique_slug(friendly_name())


def unique_slug(base: str) -> str:
    slug = slugify(base)
    if _slug_available(slug):
        return slug
    for index in range(2, 200):
        candidate = f"{slug}-{index}"
        if _slug_available(candidate):
            return candidate
    return f"{slug}-{rand_password(4).lower()}"


def _render_new(request: Request, session, *, error: str = "", **values):
    """Re-render the creation form with its values intact.

    Redirecting on a validation failure lost everything the client had typed -
    which is exactly what happened when the memory check refused a template.
    """
    info = detect()
    return render(request, "app/instance_new.html", session=session,
                  plans=db.query("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order"),
                  templates=[{"tpl": tpl, "capacity": None} for tpl in provision.template_list()],
                  info=info, headroom=headroom(info),
                  preselect_plan=values.get("plan_id", ""),
                  preselect_template=values.get("template", "lemp"),
                  form_name=values.get("name", ""),
                  form_key=values.get("ssh_key", ""),
                  error=error)


def _job_steps(job) -> list[str]:
    """The '== ' lines a provisioning job has logged, as readable steps."""
    if not job or not job["log"]:
        return []
    return [line[3:].strip() for line in job["log"].splitlines()
            if line.startswith("== ") and line[3:].strip()]


def instance_context(instance, *, deep: bool = True) -> dict:
    """Everything the instance page needs, degrading gracefully if Incus is down."""
    figures = {}
    live_status = instance["status"]
    snapshot_rows: list = []
    if deep and instance["status"] not in ("destroyed",):
        try:
            state = incus.state(instance["incus_name"])
            live_status = (state.get("status") or live_status).lower()
            figures = {
                "memory_used": (state.get("memory") or {}).get("usage", 0),
                "memory_peak": (state.get("memory") or {}).get("usage_peak", 0),
                "cpu_ns": (state.get("cpu") or {}).get("usage", 0),
                "processes": state.get("processes", 0),
            }
            # Seed the CPU counter now, so the metrics card's first poll has a
            # reading to difference against instead of showing a dash for ten
            # seconds. Incus publishes CPU as a cumulative counter, not a rate.
            metrics.remember(instance["incus_name"], figures["cpu_ns"])
            snapshot_rows = incus.snapshots(instance["incus_name"])
        except IncusError:
            pass
    plan = None
    if instance["plan_id"]:
        plan = db.one("SELECT name, vcpu, memory_mb, disk_gb FROM plans WHERE id = ?",
                      (instance["plan_id"],))

    job = tasks.latest_for_instance(instance["id"])
    return {
        # La carte d'adresses, sans détection : le rendu de la page ne paie pas
        # l'interrogation de chaque port, le fragment de la liste s'en charge.
        **_address_rows(instance),
        "instance": instance,
        "plan": plan,
        "metrics": figures,
        "live_status": live_status,
        "memory_cap": f"{plan['memory_mb']} MB" if plan else "",
        "snapshots": snapshot_rows,
        "steps": _job_steps(job),
        "job": job,
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

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024


def _client_usage(instances):
    """What the client rents, and what their instances actually consume.

    Every figure is relative to the quotas of the plans they pay for. The host's
    own headroom is deliberately not part of this: it is the operator's business
    and it confused the client's view of their own machine.

    Memory and disk, and nothing else: they are the two quantities with a
    "rented" to compare against, and the dashboard draws them as pies. CPU has
    no such denominator, so it is not reported here — a percentage of what?
    """
    totals = {"vcpu": 0, "ram_rented_mb": 0, "ram_used_mb": 0,
              "disk_rented_gb": 0.0, "disk_used_gb": 0.0, "running": 0}
    live = {}
    for row in instances:
        totals["vcpu"] += row["vcpu"] or 0
        totals["ram_rented_mb"] += row["memory_mb"] or 0
        totals["disk_rented_gb"] += row["disk_gb"] or 0
        if row["status"] == "running":
            totals["running"] += 1
        else:
            continue
        try:
            sample = incus.usage(row["incus_name"])
        except IncusError:
            sample = {}
        if not sample:
            continue
        live[row["id"]] = sample
        totals["ram_used_mb"] += round(sample["memory_used"] / MIB)
        totals["disk_used_gb"] += sample["disk_used"] / GIB

    totals["disk_used_gb"] = round(totals["disk_used_gb"], 1)
    totals["ram_free_mb"] = max(0, totals["ram_rented_mb"] - totals["ram_used_mb"])
    totals["disk_free_gb"] = max(0.0, round(totals["disk_rented_gb"] - totals["disk_used_gb"], 1))
    totals["ram_pct"] = (min(100, round(100 * totals["ram_used_mb"] / totals["ram_rented_mb"]))
                         if totals["ram_rented_mb"] else 0)
    totals["disk_pct"] = (min(100, round(100 * totals["disk_used_gb"] / totals["disk_rented_gb"]))
                          if totals["disk_rented_gb"] else 0)
    return totals, live


@router.get("")
async def dashboard(request: Request):
    session = require_user(request)
    instances = db.query(
        "SELECT i.*, p.name AS plan_name, p.vcpu, p.memory_mb, p.disk_gb "
        "FROM instances i LEFT JOIN plans p ON p.id = i.plan_id "
        "WHERE i.user_id = ? AND i.status != 'destroyed' ORDER BY i.id DESC",
        (session["uid"],))
    invoices = billing.for_user_with_context(session["uid"])[:6]
    usage, live = _client_usage(instances)

    rows = []
    for instance in instances:
        row = dict(instance)
        sample = live.get(instance["id"])
        if sample:
            row["ram_used_mb"] = round(sample["memory_used"] / MIB)
            row["disk_used_gb"] = round(sample["disk_used"] / GIB, 1)
            row["ram_free_mb"] = max(0, (instance["memory_mb"] or 0) - row["ram_used_mb"])
            row["disk_free_gb"] = max(0.0, round((instance["disk_gb"] or 0) - row["disk_used_gb"], 1))
        else:
            row["ram_used_mb"] = row["disk_used_gb"] = None
            row["ram_free_mb"] = row["disk_free_gb"] = None
        rows.append(row)

    return render(request, "app/dashboard.html", session=session,
                  rows=rows, usage=usage, invoices=invoices,
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
        return _render_new(request, session, error="Offre inconnue.",
                           plan_id=plan_id, template=template_slug, name=name, ssh_key=ssh_key)
    tpl = provision.get_template(template_slug)

    if tpl.min_memory_mb and plan["memory_mb"] < tpl.min_memory_mb:
        message = (f"Le mod\u00e8le {tpl.label} demande au moins {tpl.min_memory_mb} MB "
                   f"de RAM ; l\u2019offre {plan['name']} en propose {plan['memory_mb']} MB.")
        return _render_new(request, session, error=message, plan_id=plan["id"],
                           template=tpl.slug, name=name, ssh_key=ssh_key)

    info = detect()
    will_fit, blocked_on = fits(info, plan["vcpu"], plan["memory_mb"], plan["disk_gb"])
    if not will_fit:
        message = (f"Capacit\u00e9 insuffisante sur {info.host_name} : plus assez de {blocked_on} "
                   f"disponible. Contactez l\u2019administrateur.")
        return _render_new(request, session, error=message, plan_id=plan["id"],
                           template=tpl.slug, name=name, ssh_key=ssh_key)

    if ssh_key and "ssh-" in ssh_key.split(" ")[0]:
        db.execute("UPDATE users SET ssh_public_key = ? WHERE id = ?", (ssh_key, session["uid"]))
    elif use_saved_key and session["ssh_public_key"]:
        ssh_key = session["ssh_public_key"]

    # A counter in the name ("wordpress-2", "server-3") tells every visitor how
    # many other instances exist and roughly when this one was made. A readable
    # random pair gives nothing away; a client who wants their own name still
    # gets it, and it is what appears in the panel either way.
    slug = free_slug(name or friendly_name())
    # Each machine gets its own SSH account and its own identifier: a client is
    # never "orion" on every box, and support can refer to one machine by name.
    taken = {r["ssh_user"] for r in db.query("SELECT ssh_user FROM instances")}
    ssh_user = identity.derive_ssh_user(slug, taken)

    # Claim the SSH port here rather than in the worker: two instances
    # provisioning at the same time used to read the same table, pick the same
    # free port, and the second one died failing to bind its proxy device.
    try:
        ssh_port = provision.allocate_ssh_port()
    except RuntimeError:
        return _render_new(request, session,
                           error="Plus aucun port SSH disponible sur cet hôte. "
                                 "Prévenez l'administrateur.")

    try:
        instance_id = db.execute(
            "INSERT INTO instances(user_id, plan_id, name, slug, incus_name, machine_id, template, "
            "status, ssh_user, ssh_port, initial_password, created_at, expires_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?, ?)",
            (session["uid"], plan["id"], name or slug, slug,
             provision.incus_name_for(session["uid"], slug), identity.new_machine_id(), tpl.slug,
             ssh_user, ssh_port, rand_password(20), now(),
             days_from_now(plan["period_days"])),
        )
    except sqlite3.IntegrityError as exc:
        # Last line of defence: the name or port was taken in the meantime.
        activity.log("instance.create_conflict", user_id=session["uid"],
                     target=slug, detail=str(exc))
        return _render_new(request, session,
                           error="Ce nom vient d'être pris ou plus aucun port n'est libre. "
                                 "Réessayez avec un autre nom.",
                           plan_id=plan["id"], template=tpl.slug, name=name,
                           ssh_key=ssh_key)

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
    # Name snapshots after the instance and the minute they were taken. A bare
    # "snap-YYYY-MM-DD" collided the second time you tried on the same day and
    # Incus refused it with a raw database error.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    requested = fstr(form, "label", "")
    existing = {s.get("name") for s in (incus.snapshots(instance["incus_name"]) or [])}
    label = snapshot_label(instance["slug"], requested, stamp)
    suffix = 2
    while label in existing:
        label = snapshot_label(instance["slug"], requested, stamp, suffix=suffix)
        suffix += 1

    try:
        incus.snapshot_create(instance["incus_name"], label)
    except IncusError as exc:
        detail = (exc.detail or "").removeprefix("Error: ").strip()
        if "already exists" in detail:
            detail = f"Un instantané nommé {label} existe déjà. Réessayez dans une minute."
        else:
            detail = "Instantané refusé par Incus : " + detail
        return _refuse(request, session, instance, "partials/instance_snapshots.html",
                       detail, _snapshot_rows(instance))
    activity.log("instance.snapshot", user_id=session["uid"], target=instance["slug"], detail=label)
    if _wants_fragment(request):
        return _fragment(request, "partials/instance_snapshots.html", session,
                         message=f"Instantané {label} créé", **_snapshot_rows(instance))
    return RedirectResponse(
        f"/app/instances/{instance_id}?ok={quote(f'Instantané {label} créé')}", status_code=303)


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
    except IncusError as exc:
        return _refuse(request, session, instance, "partials/instance_snapshots.html",
                       exc.detail, _snapshot_rows(instance))
    activity.log("instance.snapshot_restore", user_id=session["uid"],
                 target=instance["slug"], detail=snapshot)
    if _wants_fragment(request):
        return _fragment(request, "partials/instance_snapshots.html", session,
                         message=f"Restauré depuis {snapshot}", **_snapshot_rows(instance))
    return RedirectResponse(
        f"/app/instances/{instance_id}?ok=Restaur%C3%A9+depuis+{snapshot}", status_code=303)


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
        return _refuse(request, session, instance, "partials/instance_snapshots.html",
                       exc.detail, _snapshot_rows(instance))
    if _wants_fragment(request):
        return _fragment(request, "partials/instance_snapshots.html", session,
                         message="Sauvegarde supprimée", **_snapshot_rows(instance))
    return RedirectResponse(f"/app/instances/{instance_id}?ok=Sauvegarde+supprim%C3%A9e", status_code=303)


@router.post("/sites/{site_id}/primary")
async def site_make_primary(request: Request, site_id: int):
    """Promote an address to the instance's main one, so it shows in Simple mode."""
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    site = db.one("SELECT s.*, i.user_id, i.slug FROM sites s "
                  "JOIN instances i ON i.id = s.instance_id WHERE s.id = ?", (site_id,))
    if site is None or (session["role"] != "admin" and site["user_id"] != session["uid"]):
        return RedirectResponse("/app/instances?err=Adresse+introuvable", status_code=303)

    db.execute("UPDATE sites SET is_primary = 0 WHERE instance_id = ?", (site["instance_id"],))
    db.execute("UPDATE sites SET is_primary = 1 WHERE id = ?", (site_id,))
    activity.log("site.primary", user_id=session["uid"], target=site["domain"],
                 detail=site["slug"])
    message = f"{site['domain']} est désormais l'adresse principale de {site['slug']}."
    instance = _owned(site["instance_id"], session)
    if instance is not None and _wants_fragment(request):
        return _fragment(request, "partials/instance_addresses.html", session,
                         message=message, **_address_rows(instance))
    return RedirectResponse(
        f"/app/instances/{site['instance_id']}?ok={quote(message)}", status_code=303)


@router.get("/instances/{instance_id}/services")
async def instance_services(request: Request, instance_id: int):
    """La liste d'adresses, détection comprise.

    Chargée après la page pour qu'un balayage ne la retarde jamais, et
    re-sondée toutes les minutes par le fragment lui-même. C'est la même carte
    que le rendu initial remplit - une seule liste, une seule cible.
    """
    session = require_user(request)
    instance = _owned(instance_id, session)
    if instance is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return render(request, "partials/instance_addresses.html", session=session,
                  **_address_rows(instance, scan=True))


@router.post("/instances/{instance_id}/services/publish")
async def service_publish(request: Request, instance_id: int):
    """Put one of the client's ports on the internet, in one click."""
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)

    port = fint(form, "port")
    if not 1 <= port <= 65535:
        return RedirectResponse(
            f"/app/instances/{instance_id}?err={quote('Port invalide.')}", status_code=303)

    # Re-checked here rather than trusted from the form: the client may have
    # stopped the service between the scan and the click, and a route to a dead
    # port is a 502 they would have to debug themselves.
    if port not in services.listening_ports(instance["incus_name"]):
        return _refuse(request, session, instance, None,
                       f"Rien n'écoute sur le port {port} dans votre instance pour le moment.")

    domain = services.address_for(instance["slug"], port, settings.domain)
    clash = db.one("SELECT * FROM sites WHERE domain = ?", (domain,))
    if clash is not None and clash["instance_id"] != instance_id:
        return _refuse(request, session, instance, None, domain + " est déjà utilisé.")

    sites.add_site(instance_id, domain, port, primary=(port == 80))
    ok, message = sites.sync_caddy()
    activity.log("service.publish", user_id=session["uid"], target=domain,
                 detail=f"port {port}")
    if _wants_fragment(request):
        # Une seule carte, désormais : publier ajoute une adresse, donc la liste
        # elle-même est la réponse. Plus de second fragment envoyé hors bande
        # pour tenir à jour une carte qui n'existe plus.
        return _fragment(
            request, "partials/instance_addresses.html", session,
            message=(domain + f" publie le port {port}") if ok else "Routage non appliqué : " + message,
            tone="ok" if ok else "err",
            **_address_rows(instance, scan=True))
    if not ok:
        return RedirectResponse(
            f"/app/instances/{instance_id}?err={quote('Routage non appliqué : ' + message)}",
            status_code=303)
    return RedirectResponse(
        f"/app/instances/{instance_id}?ok={quote(domain + f' publie le port {port}')}",
        status_code=303)


# --------------------------------------------------------------------------- fragments
#
# An action on the instance page answers with the part of the page it changed
# instead of a redirect, so acting never navigates away - which also means the
# terminal session survives whatever the client just did.
#
# The plain redirect has to stay for forms posted without JavaScript, and every
# refusal has to answer a fragment **too**: htmx follows a redirect, so a 303 on
# an htmx request would swap the whole page into the fragment's target.


def _wants_fragment(request: Request) -> bool:
    """htmx sets this header; a plain form post does not, and must keep working."""
    return request.headers.get("hx-request", "").lower() == "true"


def _fragment(request: Request, template: str | None, session, *, message: str = "",
              tone: str = "ok", **context) -> HTMLResponse:
    """A partial to swap in, carrying its own message.

    The message is rendered inside the fragment rather than into a slot at the
    top of the page: acting on a field and being told why it was refused are the
    same event, and the answer has to appear where the client is looking.
    """
    parts = []
    if message:
        parts.append(render(request, "partials/notice.html", session=session,
                            notice=message, notice_tone=tone).body.decode())
    if template:
        parts.append(render(request, template, session=session, **context).body.decode())
    return HTMLResponse("".join(parts))


def _refuse(request: Request, session, instance, template: str | None, message: str,
            context: dict | None = None):
    """The same refusal: in place when htmx asked, as a redirect otherwise."""
    if _wants_fragment(request):
        return _fragment(request, template, session, message=message, tone="err",
                         **(context or {}))
    if instance is None:
        return RedirectResponse("/app/instances?err=" + quote(message), status_code=303)
    return RedirectResponse(
        f"/app/instances/{instance['id']}?err={quote(message)}", status_code=303)


def _route_rows(instance) -> dict:
    """The addresses of an instance, and which one is its main one.

    Shared by the page and by every action that changes an address, so the
    fragment an action returns is built by the same rules as the page.
    """
    rows = sites.list_sites(instance["id"])
    default_site = next((row for row in rows if row["is_primary"]), None)
    if default_site is None and rows:
        # Nothing is marked as the instance's own address - typically because it
        # was removed. Prefer one that actually answers over one that may lead
        # nowhere, and only pay for the scan in this case.
        listening = services.listening_ports(instance["incus_name"])
        default_site = (next((row for row in rows if row["upstream"] in listening), None)
                        or rows[0])
    return {
        "instance": instance,
        "site_rows": rows,
        "default_site": default_site,
        "tenant_domain": settings.domain,
        "default_domain": (default_site["domain"] if default_site
                           else sites.default_domain(instance["slug"])),
    }


def _address_rows(instance, *, scan: bool = False) -> dict:
    """Tout ce que la carte d'adresses — une seule liste — demande, en un seul
    endroit.

    La page, le sondage et chaque action qui change une adresse la construisent
    par ici, donc un fragment et un rendu complet ne peuvent pas se contredire.

    `scan=False` saute la détection des services. C'est ce qu'utilisent le rendu
    de la page et les actions sur les adresses : la détection interroge en HTTP
    chaque port à l'écoute, et la payer à chaque renommage mettrait des secondes
    entre le clic et sa réponse. Sans détection, la carte dit seulement ce
    qu'elle sait — les adresses — et le sondage suivant (load, every 60s) y
    ajoute ce qui écoute.
    """
    rows = sites.list_sites(instance["id"])
    return {
        **_route_rows(instance),
        "scanned": scan,
        "found": services.discover(instance) if scan else [],
        "published": {row["upstream"]: row for row in rows},
        "ingress_ip": settings.ingress_ip,
    }


def _snapshot_rows(instance) -> dict:
    """The snapshots as they stand, for the fragment a snapshot action returns."""
    rows: list = []
    if instance["status"] != "destroyed":
        try:
            rows = incus.snapshots(instance["incus_name"]) or []
        except IncusError:
            rows = []
    return {"instance": instance, "snapshots": rows}


# Labels that must never become a tenant address: they either collide with
# something we serve ourselves or with the plumbing every registrar punishes.
RESERVED_LABELS = {
    "www", "mail", "smtp", "imap", "pop", "ftp", "ns", "ns1", "ns2", "dns",
    "admin", "panel", "api", "ssh", "host", "hosting", "webmail", "cpanel",
    "localhost", "autodiscover", "autoconfig", "vpn", "git", "status", "static",
}

_LABEL_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def label_problem(label: str, *, instance_id: int, site_id: int | None = None) -> str:
    """Why this subdomain label cannot be used, or '' when it can.

    Both callers - the simple-mode rename and the per-address rename - ask here,
    so the two cannot drift apart on what "already taken" means.
    """
    if not label:
        return "Indiquez un nom."
    if len(label) > 63:
        return "Un nom ne peut pas dépasser 63 caractères."
    if label.startswith("-") or label.endswith("-") or not set(label) <= _LABEL_CHARS:
        return ("Lettres minuscules, chiffres et tirets uniquement — "
                "sans tiret au début ni à la fin.")
    if label in RESERVED_LABELS:
        return f"« {label} » est réservé par le système."

    domain = f"{label}.{settings.domain}"
    taken = db.one(
        "SELECT s.id, s.instance_id, i.slug FROM sites s "
        "JOIN instances i ON i.id = s.instance_id WHERE s.domain = ?", (domain,))
    if taken and taken["id"] != site_id:
        if taken["instance_id"] == instance_id:
            return f"{domain} est déjà une adresse de cette instance."
        return f"{domain} est déjà utilisé par l'instance {taken['slug']}."

    # Another live instance generates this exact address for itself the day it is
    # published, so handing it out now would collide later.
    clash = db.one("SELECT slug FROM instances WHERE slug = ? AND id != ? "
                   "AND status != 'destroyed'", (label, instance_id))
    if clash:
        return f"« {label} » est le nom d'une autre instance — choisissez-en un autre."
    return ""


@router.get("/instances/{instance_id}/metrics")
async def instance_metrics(request: Request, instance_id: int):
    """CPU, memory and storage for the state card. Polled, so it stays cheap."""
    session = require_user(request)
    instance = _owned(instance_id, session)
    if instance is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    figures = {}
    if instance["status"] not in ("destroyed", "provisioning"):
        plan = (db.one("SELECT memory_mb, vcpu FROM plans WHERE id = ?", (instance["plan_id"],))
                if instance["plan_id"] else None)
        try:
            figures = metrics.sample(
                instance["incus_name"],
                memory_mb=(plan["memory_mb"] if plan else 0),
                vcpu=(plan["vcpu"] if plan else 1),
                state=incus.state(instance["incus_name"]),
            )
        except IncusError:
            figures = {}
    return render(request, "partials/instance_metrics.html", session=session, m=figures)


@router.post("/instances/{instance_id}/sites/{site_id}/rename")
async def site_rename(request: Request, instance_id: int, site_id: int):
    """Give a published address a name of the client's own choosing."""
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)

    site = db.one("SELECT * FROM sites WHERE id = ?", (site_id,))
    if site is None or site["instance_id"] != instance_id:
        return RedirectResponse(
            f"/app/instances/{instance_id}?err=" + quote("Adresse introuvable"), status_code=303)

    suffix = f".{settings.domain}"
    if not site["domain"].endswith(suffix):
        return RedirectResponse(
            f"/app/instances/{instance_id}?err="
            + quote("Un nom de domaine personnel se change chez votre registrar."),
            status_code=303)

    previous = site["domain"]
    current = previous[:-len(suffix)]
    label = slugify(fstr(form, "label"))
    if label == current:
        return RedirectResponse(
            f"/app/instances/{instance_id}?ok=Adresse+inchang%C3%A9e", status_code=303)

    problem = label_problem(label, instance_id=instance_id, site_id=site_id)
    if problem:
        return _refuse(request, session, instance, "partials/instance_addresses.html",
                       problem, _address_rows(instance))

    domain = f"{label}{suffix}"
    try:
        db.execute("UPDATE sites SET domain = ? WHERE id = ?", (domain, site_id))
    except sqlite3.IntegrityError:
        # The checks above can lose a race against a create in another tab.
        return _refuse(request, session, instance, "partials/instance_addresses.html",
                       f"{domain} vient d'être pris par une autre instance.",
                       _address_rows(instance))

    ok, message = sites.sync_caddy()
    activity.log("site.renamed", user_id=session["uid"], target=domain, detail=previous)
    if not ok:
        return _refuse(request, session, instance, "partials/instance_addresses.html",
                       "Routage non appliqué : " + message, _address_rows(instance))
    text = previous + " devient " + domain
    if _wants_fragment(request):
        return _fragment(request, "partials/instance_addresses.html", session,
                         message=text, **_address_rows(instance))
    return RedirectResponse(
        f"/app/instances/{instance_id}?ok={quote(text)}", status_code=303)


@router.post("/instances/{instance_id}/subdomain")
async def subdomain_set(request: Request, instance_id: int):
    """Simple mode: change only the label of the instance's own subdomain."""
    session = require_user(request)
    form = await request.form()
    guard_csrf(request, session, form)
    instance = _owned(instance_id, session)
    if instance is None:
        return RedirectResponse("/app/instances?err=Instance+introuvable", status_code=303)

    label = slugify(fstr(form, "label"))
    primary = db.one("SELECT * FROM sites WHERE instance_id = ? AND is_primary = 1", (instance_id,))
    problem = label_problem(label, instance_id=instance_id,
                            site_id=(primary["id"] if primary else None))
    if problem:
        return _refuse(request, session, instance, "partials/instance_addresses.html",
                       problem, _address_rows(instance))

    domain = f"{label}.{settings.domain}"
    if primary is None:
        sites.add_site(instance_id, domain, 80, primary=True)
    else:
        db.execute("UPDATE sites SET domain = ? WHERE id = ?", (domain, primary["id"]))

    ok, message = sites.sync_caddy()
    activity.log("site.subdomain_set", user_id=session["uid"], target=domain,
                 detail=instance["slug"])
    if not ok:
        return _refuse(request, session, instance, "partials/instance_addresses.html",
                       "Routage non appliqué : " + message, _address_rows(instance))
    text = f"Adresse mise à jour : {domain}"
    if _wants_fragment(request):
        return _fragment(request, "partials/instance_addresses.html", session,
                         message=text, **_address_rows(instance))
    return RedirectResponse(
        f"/app/instances/{instance_id}?ok={quote(text)}", status_code=303)


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
    if _wants_fragment(request):
        # The service list shows a "publié" badge for this route; it re-scans on
        # its own within the minute rather than paying for a scan on every click.
        return _fragment(request, "partials/instance_addresses.html", session,
                         message=f"{site['domain']} retiré", **_address_rows(instance))
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
