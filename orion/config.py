"""Runtime configuration, read from the environment.

Everything the panel needs to know about *where* it lives is configurable, so
the same image can be dropped on a different machine without a rebuild.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _s(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _i(key: str, default: int = 0) -> int:
    try:
        return int(_s(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Identity
    brand: str
    app_name: str
    domain: str
    base_url: str

    # Storage
    data_dir: Path
    secret_key: str
    admin_email: str
    admin_password: str

    # Incus
    incus_bin: str
    incus_project: str
    incus_pool: str
    incus_network: str
    incus_image: str
    incus_socket: str

    # Client access
    ssh_host: str
    ssh_port_min: int
    ssh_port_max: int
    ssh_user: str

    # Tenant web routing
    caddy_admin: str
    caddy_config: str
    tenant_listen: str
    ingress_ip: str

    # Capacity (held back from sale)
    reserve_vcpu: int
    reserve_ram_mb: int
    reserve_disk_gb: int
    cpu_total_override: int
    ram_total_override: int
    disk_total_override: int

    # Billing
    billing_mode: str
    currency: str

    # Anti-abuse: the Cloudflare Turnstile widget on the sign-in form. Empty
    # values mean the widget is not rendered and the check is skipped, so the
    # panel stays usable on a machine with no Cloudflare account.
    turnstile_site_key: str
    turnstile_secret: str

    # Phone confirmation of new accounts. `sms_backend=log` writes the code to the
    # panel's log so the flow can be exercised without a provider account; it is
    # not a delivery method, and the panel says so where it matters.
    require_phone: bool
    sms_backend: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "orion.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def caddy_config_path(self) -> Path:
        """Generated routing table.

        This path must be the *same host directory* the orion-caddy container
        mounts at /etc/caddy, so that a restart of either side comes back with
        the last known configuration.
        """
        return Path(self.caddy_config)


def load() -> Settings:
    return Settings(
        brand=_s("ORION_BRAND", "node29"),
        app_name=_s("ORION_APP_NAME", "Orion"),
        domain=_s("ORION_DOMAIN", "hosting.node29.fr"),
        base_url=_s("ORION_BASE_URL", "https://hosting.node29.fr").rstrip("/"),
        data_dir=Path(_s("ORION_DATA_DIR", "/data")),
        secret_key=_s("ORION_SECRET_KEY", ""),
        admin_email=_s("ORION_ADMIN_EMAIL", "admin@example.com").lower(),
        admin_password=_s("ORION_ADMIN_PASSWORD", ""),
        incus_bin=_s("ORION_INCUS_BIN", "incus"),
        incus_project=_s("ORION_INCUS_PROJECT", "default"),
        incus_pool=_s("ORION_INCUS_POOL", "orion"),
        incus_network=_s("ORION_INCUS_NETWORK", "incusbr0"),
        incus_image=_s("ORION_INCUS_IMAGE", "images:debian/12"),
        incus_socket=_s("ORION_INCUS_SOCKET", "/var/lib/incus/unix.socket"),
        ssh_host=_s("ORION_SSH_HOST", "hosting.node29.fr"),
        ssh_port_min=_i("ORION_SSH_PORT_MIN", 20000),
        ssh_port_max=_i("ORION_SSH_PORT_MAX", 20999),
        ssh_user=_s("ORION_SSH_USER", "orion"),
        caddy_admin=_s("ORION_CADDY_ADMIN", "http://orion-caddy:2019").rstrip("/"),
        caddy_config=_s("ORION_CADDY_CONFIG", "/caddy/caddy.json"),
        tenant_listen=_s("ORION_TENANT_LISTEN", ":8080"),
        ingress_ip=_s("ORION_INGRESS_IP", ""),
        reserve_vcpu=_i("ORION_RESERVE_VCPU", 1),
        reserve_ram_mb=_i("ORION_RESERVE_RAM_MB", 1536),
        reserve_disk_gb=_i("ORION_RESERVE_DISK_GB", 10),
        cpu_total_override=_i("ORION_CPU_TOTAL", 0),
        ram_total_override=_i("ORION_RAM_TOTAL_MB", 0),
        disk_total_override=_i("ORION_DISK_TOTAL_GB", 0),
        billing_mode=_s("ORION_BILLING_MODE", "mock"),
        currency=_s("ORION_CURRENCY", "EUR"),
        turnstile_site_key=_s("ORION_TURNSTILE_SITE_KEY", ""),
        turnstile_secret=_s("ORION_TURNSTILE_SECRET", ""),
        require_phone=_s("ORION_REQUIRE_PHONE", "1") == "1",
        sms_backend=_s("ORION_SMS_BACKEND", "log"),
    )


settings = load()
