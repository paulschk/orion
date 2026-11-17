"""Tenant website routing.

Orion owns one Caddy instance dedicated to tenant traffic. Instead of editing a
Caddyfile and reloading, we generate the whole configuration as JSON and hand it
to Caddy's admin API in one call - so a routing change is atomic and needs no
file writes on the edge.
"""

from __future__ import annotations

import json
import logging

import httpx

from . import db
from .config import settings
from .util import now

log = logging.getLogger("orion.sites")

NOT_FOUND_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Site inconnu - {brand}</title>
<style>body{{margin:0;background:#0b0c10;color:#e0e0e0;font:16px/1.6 system-ui,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
.c{{max-width:32rem;padding:2rem}}h1{{margin:0 0 .5rem;font-size:1.5rem}}
.n{{color:#3b82f6}}p{{color:#9ca3af}}code{{background:#111827;border:1px solid #1f2937;
padding:.15rem .4rem;border-radius:4px}}</style></head><body><div class="c">
<h1>{brand}<span class="n">Hosting</span></h1>
<p>Ce nom d&apos;h&ocirc;te n&apos;est rattach&eacute; &agrave; aucune instance.</p>
<p><a style="color:#3b82f6" href="{base_url}">{base_url}</a></p>
</div></body></html>"""

UNAVAILABLE_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Instance arr&ecirc;t&eacute;e - {brand}</title>
<style>body{{margin:0;background:#0b0c10;color:#e0e0e0;font:16px/1.6 system-ui,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
.c{{max-width:32rem;padding:2rem}}h1{{margin:0 0 .5rem;font-size:1.5rem}}
.n{{color:#3b82f6}}p{{color:#9ca3af}}</style></head><body><div class="c">
<h1>{brand}<span class="n">Hosting</span></h1>
<p>Le site <code>{domain}</code> existe, mais son instance n&apos;est pas en ligne.</p>
<p>D&eacute;marrez-la depuis le panneau : <a style="color:#3b82f6" href="{base_url}">{base_url}</a></p>
</div></body></html>"""


def default_domain(slug: str) -> str:
    return f"{slug}.{settings.domain}"


def list_sites(instance_id: int) -> list:
    return db.query("SELECT * FROM sites WHERE instance_id = ? ORDER BY id", (instance_id,))


def all_sites() -> list:
    return db.query(
        "SELECT s.*, i.incus_name, i.status AS instance_status, i.container_ip, i.slug AS instance_slug "
        "FROM sites s JOIN instances i ON i.id = s.instance_id ORDER BY s.domain")


def add_site(instance_id: int, domain: str, upstream: int = 80, *, primary: bool = False) -> int:
    domain = domain.strip().lower()
    existing = db.one("SELECT * FROM sites WHERE domain = ?", (domain,))
    if existing:
        db.execute("UPDATE sites SET instance_id = ?, upstream = ?, enabled = 1, "
                   "is_primary = ? WHERE id = ?",
                   (instance_id, upstream, 1 if primary else existing["is_primary"],
                    existing["id"]))
        return existing["id"]
    return db.execute(
        "INSERT INTO sites(instance_id, domain, upstream, enabled, is_primary, created_at) "
        "VALUES(?, ?, ?, 1, ?, ?)",
        (instance_id, domain, upstream, 1 if primary else 0, now()))


def remove_site(site_id: int) -> None:
    db.execute("DELETE FROM sites WHERE id = ?", (site_id,))


def build_caddy_config() -> dict:
    routes: list[dict] = []
    for site in all_sites():
        if not site["enabled"]:
            continue
        domain = site["domain"]
        ip = site["container_ip"] or ""
        live = site["instance_status"] == "running" and ip
        handlers: list[dict]
        if live:
            handlers = [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"{ip}:{site['upstream']}"}],
                "headers": {"request": {"set": {"X-Forwarded-Proto": ["https"]}}},
                "transport": {"protocol": "http", "read_timeout": "300s", "write_timeout": "300s"},
            }]
        else:
            handlers = [{
                "handler": "static_response",
                "status_code": 503,
                "body": UNAVAILABLE_PAGE.format(
                    brand=settings.brand, domain=domain, base_url=settings.base_url),
                "headers": {"Content-Type": ["text/html; charset=utf-8"]},
            }]
        routes.append({
            "match": [{"host": [domain]}],
            "handle": handlers,
            "terminal": True,
        })

    routes.append({
        "handle": [{
            "handler": "static_response",
            "status_code": 404,
            "body": NOT_FOUND_PAGE.format(brand=settings.brand, base_url=settings.base_url),
            "headers": {"Content-Type": ["text/html; charset=utf-8"]},
        }],
        "terminal": True,
    })

    return {
        "admin": {"listen": "0.0.0.0:2019", "enforce_origin": False},
        "logging": {
            "logs": {
                "default": {
                    "level": "INFO",
                    "encoder": {"format": "json"},
                    "writer": {"output": "stdout"},
                }
            }
        },
        "apps": {
            "http": {
                "servers": {
                    "orion": {
                        "listen": [settings.tenant_listen],
                        "routes": routes,
                        "logs": {},
                        # TLS is terminated by the edge Caddy. Without this,
                        # Caddy sees the host matchers and tries to obtain a
                        # certificate for every tenant domain itself, and
                        # injects HTTP->HTTPS redirects on this plain listener.
                        "automatic_https": {"disable": True},
                    }
                }
            }
        },
    }


def sync_caddy() -> tuple[bool, str]:
    """Push the routing table to the tenant Caddy. Never fatal."""
    config = build_caddy_config()
    try:
        settings.caddy_config_path.parent.mkdir(parents=True, exist_ok=True)
        settings.caddy_config_path.write_text(json.dumps(config, indent=2))
    except OSError as exc:
        log.warning("could not persist caddy config: %s", exc)

    try:
        response = httpx.post(
            f"{settings.caddy_admin}/load",
            json=config,
            headers={"Content-Type": "application/json", "Cache-Control": "must-revalidate"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        return False, f"{settings.caddy_admin} unreachable: {exc}"
    if response.status_code >= 400:
        return False, f"caddy rejected the config: {response.status_code} {response.text[:300]}"
    return True, "ok"


def caddy_health() -> tuple[bool, str]:
    try:
        response = httpx.get(f"{settings.caddy_admin}/config/", timeout=6.0)
        return response.status_code == 200, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)
