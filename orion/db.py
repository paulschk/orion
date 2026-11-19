"""SQLite access layer.

Deliberately plain: sqlite3 from the standard library, a small helper surface,
and one schema string. Orion runs on one host with one writer, so this is well
inside SQLite's comfort zone and keeps the deployment dependency-free.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Mapping
from typing import Any, Iterable

from .config import settings
from .util import now, rand_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    name            TEXT    NOT NULL DEFAULT '',
    company         TEXT    NOT NULL DEFAULT '',
    role            TEXT    NOT NULL DEFAULT 'client',
    is_active       INTEGER NOT NULL DEFAULT 1,
    ssh_public_key  TEXT    NOT NULL DEFAULT '',
    email_verified_at TEXT,
    created_at      TEXT    NOT NULL,
    last_login_at   TEXT
);

-- Single-use confirmation links. The token itself is never stored: only its
-- hash, with an expiry and a used-at stamp.
CREATE TABLE IF NOT EXISTS email_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    token_hash      TEXT    NOT NULL,
    used_at         TEXT,
    expires_at      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    vcpu         INTEGER NOT NULL,
    memory_mb    INTEGER NOT NULL,
    disk_gb      INTEGER NOT NULL,
    price_cents  INTEGER NOT NULL,
    currency     TEXT    NOT NULL DEFAULT 'EUR',
    period_days  INTEGER NOT NULL DEFAULT 30,
    sites        INTEGER NOT NULL DEFAULT 1,
    description  TEXT    NOT NULL DEFAULT '',
    features     TEXT    NOT NULL DEFAULT '',
    is_active    INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS instances (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    plan_id          INTEGER REFERENCES plans(id),
    name             TEXT    NOT NULL,
    slug             TEXT    NOT NULL,
    incus_name       TEXT    NOT NULL UNIQUE,
    machine_id       TEXT    NOT NULL DEFAULT '',
    template         TEXT    NOT NULL DEFAULT 'bare',
    status           TEXT    NOT NULL DEFAULT 'provisioning',
    container_ip     TEXT    NOT NULL DEFAULT '',
    ssh_port         INTEGER,
    ssh_user         TEXT    NOT NULL DEFAULT 'orion',
    initial_password TEXT    NOT NULL DEFAULT '',
    provision_log    TEXT    NOT NULL DEFAULT '',
    error            TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT,
    expires_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_instances_user ON instances(user_id);

CREATE TABLE IF NOT EXISTS sites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    domain       TEXT    NOT NULL UNIQUE,
    upstream     INTEGER NOT NULL DEFAULT 80,
    enabled      INTEGER NOT NULL DEFAULT 1,
    is_primary   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    plan_id      INTEGER NOT NULL,
    instance_id  INTEGER,
    status       TEXT    NOT NULL DEFAULT 'active',
    started_at   TEXT    NOT NULL,
    renews_at    TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT    NOT NULL UNIQUE,
    user_id         INTEGER NOT NULL,
    subscription_id INTEGER,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'EUR',
    status          TEXT    NOT NULL DEFAULT 'sent',
    description     TEXT    NOT NULL DEFAULT '',
    issued_at       TEXT    NOT NULL,
    due_at          TEXT,
    paid_at         TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    csrf        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    ip          TEXT    NOT NULL DEFAULT '',
    user_agent  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    action     TEXT    NOT NULL,
    target     TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '',
    ip         TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    instance_id INTEGER,
    status      TEXT    NOT NULL DEFAULT 'queued',
    progress    INTEGER NOT NULL DEFAULT 0,
    step        TEXT    NOT NULL DEFAULT '',
    log         TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    finished_at TEXT
);
"""

SEED_PLANS = [
    dict(slug="starter", name="Starter", vcpu=1, memory_mb=512, disk_gb=5,
         price_cents=300, sites=1, sort_order=10,
         description="Une petite vitrine, un blog, un projet perso.",
         features=["1 vCPU", "512 MB RAM", "5 GB NVMe", "1 site web",
                   "SSH + console web", "Sauvegardes \u00e0 la demande"]),
    dict(slug="standard", name="Standard", vcpu=1, memory_mb=1024, disk_gb=10,
         price_cents=600, sites=3, sort_order=20,
         description="Le tier le plus courant : WordPress et sites dynamiques.",
         features=["1 vCPU", "1 GB RAM", "10 GB NVMe", "3 sites web",
                   "SSH + console web", "Sauvegardes \u00e0 la demande"]),
    dict(slug="pro", name="Pro", vcpu=2, memory_mb=2048, disk_gb=25,
         price_cents=1200, sites=10, sort_order=30,
         description="Applications, bases de donn\u00e9es, trafic r\u00e9gulier.",
         features=["2 vCPU", "2 GB RAM", "25 GB NVMe", "10 sites web",
                   "SSH + console web", "Sauvegardes planifi\u00e9es"]),
    dict(slug="business", name="Business", vcpu=4, memory_mb=4096, disk_gb=50,
         price_cents=2400, sites=25, sort_order=40,
         description="Charge soutenue, plusieurs applications, staging.",
         features=["4 vCPU", "4 GB RAM", "50 GB NVMe", "25 sites web",
                   "SSH + console web", "Sauvegardes planifi\u00e9es"]),
]

SETTING_DEFAULTS = {
    "host_fingerprint": "",
    "host_name": "",
    "host_first_seen": "",
    "signup_open": "1",
    "provider_name": "node29",
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextlib.contextmanager
def cursor(commit: bool = False):
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bind(params: Any) -> Any:
    """sqlite3 wants a tuple for positional params and a dict for named ones."""
    if params is None:
        return ()
    if isinstance(params, Mapping):
        return dict(params)
    if isinstance(params, (tuple, list)):
        return tuple(params)
    return (params,)


def query(sql: str, params: Any = ()) -> list[sqlite3.Row]:
    with cursor() as cur:
        return cur.execute(sql, bind(params)).fetchall()


def one(sql: str, params: Any = ()) -> sqlite3.Row | None:
    with cursor() as cur:
        return cur.execute(sql, bind(params)).fetchone()


def scalar(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    row = one(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def execute(sql: str, params: Any = ()) -> int:
    with cursor(commit=True) as cur:
        cur.execute(sql, bind(params))
        return cur.lastrowid or 0


def expand(sql: str, params: Any = ()) -> list[sqlite3.Row]:
    """Alias kept for readability at call sites that read many rows."""
    return query(sql, params)


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return SETTING_DEFAULTS.get(key, default)
    return row["value"]


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _columns(cur, table: str) -> set:
    return {row[1] for row in cur.execute("PRAGMA table_info(" + table + ")").fetchall()}


def migrate() -> None:
    """Bring an existing database up to the current schema and backfill it.

    Small enough to hand-roll: the panel runs on one host, and a migration that
    silently does nothing is worse than an explicit one.
    """
    from . import identity

    added_email_verification = False
    with cursor(commit=True) as cur:
        if "machine_id" not in _columns(cur, "instances"):
            cur.execute("ALTER TABLE instances ADD COLUMN machine_id TEXT NOT NULL DEFAULT ''")
            print("[orion] schema: added instances.machine_id")

        job_columns = _columns(cur, "jobs")
        if "progress" not in job_columns:
            cur.execute("ALTER TABLE jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            print("[orion] schema: added jobs.progress")
        if "step" not in job_columns:
            cur.execute("ALTER TABLE jobs ADD COLUMN step TEXT NOT NULL DEFAULT ''")
            print("[orion] schema: added jobs.step")

        site_columns = _columns(cur, "sites")
        if "is_primary" not in site_columns:
            cur.execute("ALTER TABLE sites ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")
            print("[orion] schema: added sites.is_primary")
            added_primary = True
        else:
            added_primary = False

        # Email confirmation, which replaced a phone check that lasted an hour and
        # was never used by anybody. The old table and columns go: a schema that
        # keeps describing something the panel no longer does is worse than one
        # that says what it does. Wrapped because DROP COLUMN needs SQLite 3.35,
        # and a database that cannot do it should still come up.
        user_columns = _columns(cur, "users")
        if "email_verified_at" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
            print("[orion] schema: added users.email_verified_at")
            added_email_verification = True
        for obsolete in ("phone", "phone_verified_at"):
            if obsolete in user_columns:
                try:
                    cur.execute("ALTER TABLE users DROP COLUMN {}".format(obsolete))
                    print("[orion] schema: dropped users.{}".format(obsolete))
                except Exception as exc:                     # noqa: BLE001
                    print("[orion] schema: could not drop users.{} ({})".format(obsolete, exc))
        try:
            cur.execute("DROP TABLE IF EXISTS phone_codes")
        except Exception as exc:                             # noqa: BLE001
            print("[orion] schema: phone_codes left in place ({})".format(exc))
        cur.execute(
            "CREATE TABLE IF NOT EXISTS email_tokens ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id INTEGER NOT NULL,"
            " token_hash TEXT NOT NULL,"
            " used_at TEXT,"
            " expires_at TEXT NOT NULL,"
            " created_at TEXT NOT NULL)"
        )

    # One-shot backfill: the site whose domain is exactly the instance's
    # generated name is the instance's own address. It must run only when the
    # column appears. Re-running it on every boot silently re-promotes whatever
    # route a client happens to have under their instance's own name - including
    # one they pointed at a port where nothing listens.
    if added_primary:
        for row in query("SELECT s.id, s.domain, i.slug FROM sites s "
                         "JOIN instances i ON i.id = s.instance_id"):
            if row["domain"] == f"{row['slug']}.{settings.domain}":
                execute("UPDATE sites SET is_primary = 1 WHERE id = ?", (row["id"],))

    # One-shot backfill: accounts that existed before the confirmation check are
    # taken as confirmed. Without it every existing client would be locked out by
    # a rule that arrived after them - and it must run only when the column
    # appears, never on every boot, or it would confirm each new sign-up too.
    if added_email_verification:
        execute("UPDATE users SET email_verified_at = created_at WHERE email_verified_at IS NULL")
        print("[orion] schema: existing accounts marked as confirmed")

    # The SSH port must be unique: two instances provisioning at once used to
    # pick the same one and the second failed to bind its proxy device.
    duplicates = query(
        "SELECT ssh_port, COUNT(*) AS n FROM instances WHERE ssh_port IS NOT NULL "
        "GROUP BY ssh_port HAVING n > 1")
    if duplicates:
        print("[orion] WARNING: duplicate ssh_port values present, not enforcing uniqueness:",
              ", ".join(f"{r['ssh_port']}x{r['n']}" for r in duplicates))
    else:
        with cursor(commit=True) as cur:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_ssh_port "
                        "ON instances(ssh_port) WHERE ssh_port IS NOT NULL")

    # Every instance gets a machine id. The SSH account name can only be changed
    # safely for instances that were never provisioned: on a live container the
    # account already exists as the old name, so renaming the row alone would
    # lock the client out. Those are fixed by the admin "Réparer" action, which
    # renames the account inside the container.
    rows = query("SELECT id, slug, ssh_user, machine_id, ssh_port FROM instances "
                 "WHERE status != 'destroyed'")
    taken = {r["ssh_user"].lower() for r in rows if r["ssh_user"]}
    for row in rows:
        updates, params = [], []
        if not row["machine_id"]:
            updates.append("machine_id = ?")
            params.append(identity.new_machine_id())
        if row["ssh_port"] is None and (
                not row["ssh_user"] or row["ssh_user"] == settings.ssh_user):
            fresh = identity.derive_ssh_user(row["slug"], taken)
            taken.add(fresh.lower())
            updates.append("ssh_user = ?")
            params.append(fresh)
        if updates:
            params.append(row["id"])
            execute("UPDATE instances SET " + ", ".join(updates) + " WHERE id = ?", params)


def init() -> None:
    """Create the schema and seed the first run. Idempotent."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.caddy_config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # sites.sync_caddy() already degrades gracefully when it cannot persist
        # the routing table, so a missing or unwritable mount must not stop the
        # panel from starting.
        pass

    with cursor(commit=True) as cur:
        cur.executescript(SCHEMA)

    migrate()

    for key, value in SETTING_DEFAULTS.items():
        if one("SELECT 1 FROM settings WHERE key = ?", (key,)) is None:
            execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, value))

    for seed in SEED_PLANS:
        if one("SELECT 1 FROM plans WHERE slug = ?", (seed["slug"],)) is None:
            plan = {k: v for k, v in seed.items() if k != "features"}
            execute(
                "INSERT INTO plans(slug, name, vcpu, memory_mb, disk_gb, price_cents, "
                "currency, period_days, sites, description, features, is_active, sort_order) "
                "VALUES(:slug, :name, :vcpu, :memory_mb, :disk_gb, :price_cents, "
                ":currency, 30, :sites, :description, :features, 1, :sort_order)",
                {**plan, "currency": settings.currency,
                 "features": "\n".join(seed["features"])},
            )

    from .security import hash_password  # local import: avoids a cycle

    if one("SELECT 1 FROM users WHERE role = 'admin'") is None:
        password = settings.admin_password or rand_password(16)
        execute(
            "INSERT INTO users(email, password_hash, name, role, is_active, created_at) "
            "VALUES(?, ?, 'Administrator', 'admin', 1, ?)",
            (settings.admin_email, hash_password(password), now()),
        )
        if not settings.admin_password:
            print(f"[orion] admin account created: {settings.admin_email} / {password}")
