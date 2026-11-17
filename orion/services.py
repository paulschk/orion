"""Discover the services a client is running, so they can publish them.

A client who starts a container does not think in upstream ports and hostnames:
they started something and they want to reach it. The panel therefore looks at
what is actually listening inside their machine and offers to publish it, rather
than asking a non-technical client to describe their own setup.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request

from .incus import IncusError, incus

# Parts of the machine itself, never a client service: the access they were
# given, and the stub resolver and LLMNR responder the templates install.
INTERNAL_PORTS = {22, 53, 5353, 5355}

# Ports a client may legitimately run but should almost never face the internet.
# Listed with a warning rather than hidden: it is their machine.
SENSITIVE_PORTS = {
    2375: "l'API Docker, sans authentification",
    2376: "l'API Docker",
    3306: "une base MySQL ou MariaDB",
    5432: "une base PostgreSQL",
    6379: "une base Redis",
    11211: "un cache Memcached",
    27017: "une base MongoDB",
    5672: "un courtier RabbitMQ",
}

# Probing these with HTTP only wastes a timeout: they never speak it.
_NEVER_HTTP = {2375, 2376, 3306, 5432, 6379, 11211, 27017, 5672}

_LISTEN = "0A"          # TCP_LISTEN, the state field in /proc/net/tcp


def parse_proc_net(text: str) -> set[int]:
    """Listening TCP ports out of the kernel's own table.

    Parsed here rather than by `ss` or `awk` inside the instance: the images
    disagree about what they ship - busybox awk has no `strtonum`, which is a
    parse error rather than an empty result - and this file is always present.
    """
    ports: set[int] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != _LISTEN:
            continue
        local = fields[1]
        try:
            ports.add(int(local.rsplit(":", 1)[1], 16))
        except (IndexError, ValueError):
            continue
    return ports


def listening_ports(name: str) -> set[int]:
    """Every TCP port in LISTEN state inside an instance."""
    try:
        _, out, _ = incus.exec(name, ["cat", "/proc/net/tcp", "/proc/net/tcp6"], timeout=30)
    except IncusError:
        return set()
    # /proc/net/tcp6 may be absent; the exit status is not worth trusting.
    return parse_proc_net(out)


def _status(url: str, timeout: float) -> int | None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code              # an HTTP error is still an HTTP service
    except Exception:
        return None


def probe_http(ip: str, port: int, timeout: float = 2.5) -> int | None:
    """The HTTP status if something answers as a web server, else None.

    This is what lets the panel say "port 8080 serves a website" instead of
    asking a client which of their own ports is the web one.
    """
    if not ip or port in _NEVER_HTTP:
        return None
    status = _status(f"http://{ip}:{port}/", timeout)
    if status is None and port in (443, 8443):
        status = _status(f"https://{ip}:{port}/", timeout)
    return status


def discover(instance) -> list[dict]:
    """Candidate services on an instance, each with a plain-language reading.

    Takes a row from the instances table: it uses `incus_name` and
    `container_ip`.
    """
    ip = instance["container_ip"] or ""
    found = []
    for port in sorted(listening_ports(instance["incus_name"])):
        if port in INTERNAL_PORTS:
            continue
        status = probe_http(ip, port)
        found.append({
            "port": port,
            "status": status,
            "web": status is not None,
            "caution": SENSITIVE_PORTS.get(port, ""),
        })
    return found


def address_for(slug: str, port: int, tenant_domain: str) -> str:
    """The address a published port gets. 80 and 443 keep the plain name."""
    label = slug if port in (80, 443) else f"{slug}-{port}"
    return f"{label}.{tenant_domain}"
