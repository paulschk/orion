"""Per-instance identity.

Each instance gets its own SSH account and a short machine identifier, so a
client is never "orion on every box": they log in as a user named after their
own machine, and the identifier gives each instance a stable handle to refer to
in the panel, in support messages and in their SSH config.
"""

from __future__ import annotations

import re
import secrets

from .config import settings

#: account names that must never be handed to a tenant
RESERVED = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news",
    "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats", "nobody",
    "systemd-network", "systemd-resolve", "sshd", "messagebus", "app", "orion",
    "admin", "administrator", "postgres", "mysql", "redis", "docker",
}

MAX_LEN = 24


def _sanitise(value: str) -> str:
    """Reduce anything to a legal, readable Linux account name."""
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "", value)
    value = value.strip("-_")
    value = re.sub(r"[-_]{2,}", "-", value)
    if not value:
        value = "serveur"
    if not value[0].isalpha():
        value = f"u{value}"
    return value[:MAX_LEN]


def derive_ssh_user(slug: str, taken: set[str] | None = None) -> str:
    """A unique account name for an instance, based on its slug."""
    taken = {t.lower() for t in (taken or set())}
    base = _sanitise(slug)

    if base in RESERVED:
        base = f"{base}-user"

    candidate = base
    if candidate not in taken and candidate not in RESERVED:
        return candidate

    for _ in range(50):
        suffix = secrets.token_hex(2)          # 4 hex chars
        candidate = f"{base[:MAX_LEN - 5]}-{suffix}"
        if candidate not in taken and candidate not in RESERVED:
            return candidate
    # Practically unreachable.
    return f"u{secrets.token_hex(4)}"


def new_machine_id() -> str:
    """Short, stable, human-transcribable handle for a machine."""
    return "m-" + secrets.token_hex(3)         # m-a1b2c3


def ssh_config_block(*, alias: str, host: str, port: int, user: str) -> str:
    """Ready-to-paste ~/.ssh/config entry, so clients do not juggle ports."""
    return (
        f"Host {alias}\n"
        f"    HostName {host}\n"
        f"    Port {port}\n"
        f"    User {user}\n"
        f"    IdentitiesOnly yes\n"
        f"    IdentityFile ~/.ssh/id_ed25519\n"
    )


def ssh_command(*, host: str, port: int, user: str) -> str:
    return f"ssh -p {port} {user}@{host}"


def default_user_base() -> str:
    return settings.ssh_user
