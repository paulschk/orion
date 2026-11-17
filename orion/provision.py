"""Instance lifecycle: templates, provisioning scripts, teardown.

A template is just a bash script that runs once inside a fresh container. We
keep them as data (``TEMPLATES``) so the dashboard can list them without
importing anything, and so adding a stack is a self-contained edit.

Scripts are pushed into the container as a file and executed there, which keeps
us clear of nested shell quoting entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import activity, db, sites, tasks
from .config import settings
from .hostinfo import detect
from .incus import IncusError, incus
from .util import now, slugify


# --------------------------------------------------------------------------- templates

@dataclass(frozen=True)
class Template:
    slug: str
    label: str
    tagline: str
    icon: str
    web: bool
    port: int = 80
    min_memory_mb: int = 0
    sets_web_root: bool = False
    extra: tuple[str, ...] = field(default=())


TEMPLATES: dict[str, Template] = {
    "bare": Template(
        slug="bare", label="Serveur nu", icon="terminal",
        tagline="Debian + SSH. Installez ce que vous voulez, comme sur un VPS.",
        web=False,
    ),
    "lemp": Template(
        slug="lemp", label="Site web (LEMP)", icon="globe",
        tagline="Nginx, PHP-FPM et MariaDB pr\u00eats \u00e0 l\u2019emploi, un site servi.",
        web=True, port=80, min_memory_mb=1024, sets_web_root=True,
    ),
    "wordpress": Template(
        slug="wordpress", label="WordPress", icon="wordpress",
        tagline="WordPress install\u00e9 et configur\u00e9, identifiants affich\u00e9s ici.",
        web=True, port=80, min_memory_mb=1024, sets_web_root=True,
    ),
    "node": Template(
        slug="node", label="Application Node.js", icon="node",
        tagline="Node LTS, une application exemple derri\u00e8re Nginx.",
        web=True, port=80, min_memory_mb=512,
    ),
    "docker": Template(
        slug="docker", label="Docker", icon="docker",
        tagline="Moteur Docker install\u00e9 (isolation imbriqu\u00e9e active).",
        web=False, min_memory_mb=1024,
    ),
}


def get_template(slug: str) -> Template:
    return TEMPLATES.get(slug) or TEMPLATES["bare"]


def template_list() -> list[Template]:
    return list(TEMPLATES.values())


# --------------------------------------------------------------------------- script base

BASE_SCRIPT = r"""#!/bin/bash
# Orion provisioning script - generated, do not edit in place.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export DEBCONF_NONINTERACTIVE_SEEN=true

step() { printf '\n\033[1;33m:: %s\033[0m\n' "$1"; }

# Containers sit on the hosting bridge, which carries no IPv6 route. Debian
# images resolve AAAA records first, so apt would otherwise hang.
printf 'Acquire::ForceIPv4 "true";\n' > /etc/apt/apt.conf.d/99orion-ipv4

step "Updating the package index"
apt-get update -qq

step "Installing base packages"
apt-get install -y -qq --no-install-recommends \
  openssh-server sudo curl ca-certificates gnupg unzip rsync \
  htop nano locales tzdata net-tools

step "Configuring locale, timezone and hostname"
sed -i 's/^# *@@LOCALE@@/@@LOCALE@@/' /etc/locale.gen 2>/dev/null || true
locale-gen @@LOCALE@@ >/dev/null 2>&1 || true
ln -sf "/usr/share/zoneinfo/@@TIMEZONE@@" /etc/localtime
echo "@@TIMEZONE@@" > /etc/timezone
printf '127.0.1.1\t@@HOSTNAME@@\n' >> /etc/hosts
hostname "@@HOSTNAME@@" || true

step "Creating the @@SSHUSER@@ account"
if ! id -u "@@SSHUSER@@" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "@@SSHUSER@@"
fi
usermod -aG sudo "@@SSHUSER@@"
echo "@@SSHUSER@@:@@PASSWORD@@" | chpasswd
install -d -m 0700 -o "@@SSHUSER@@" -g "@@SSHUSER@@" "/home/@@SSHUSER@@/.ssh"
cat > "/home/@@SSHUSER@@/.ssh/authorized_keys" <<'ORION_KEYS'
@@AUTHORIZED_KEYS@@
ORION_KEYS
chmod 0600 "/home/@@SSHUSER@@/.ssh/authorized_keys"
chown "@@SSHUSER@@:@@SSHUSER@@" "/home/@@SSHUSER@@/.ssh/authorized_keys"

# Passwordless sudo for the tenant account: this *is* their machine.
printf '@@SSHUSER@@ ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-@@SSHUSER@@
chmod 0440 /etc/sudoers.d/90-@@SSHUSER@@

step "Hardening SSH"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-orion.conf <<'ORION_SSHD'
PubkeyAuthentication yes
PermitRootLogin @@ROOT_LOGIN@@
PasswordAuthentication @@PASSWORD_AUTH@@
PermitEmptyPasswords no
X11Forwarding no
ClientAliveInterval 120
ORION_SSHD
systemctl enable ssh >/dev/null 2>&1 || true
systemctl restart ssh
systemctl is-active ssh

step "Writing the welcome message"
cat > /etc/motd <<'ORION_MOTD'

  @@BRAND@@ hosting
  ------------------------------------------------------------------
  Instance   : @@HOSTNAME@@@@DOMAIN_SUFFIX@@
  Plan       : @@PLAN_NAME@@ (@@PLAN_VCPU@@ vCPU / @@PLAN_RAM@@ MB / @@PLAN_DISK@@ GB)
  Created    : @@CREATED_AT@@
  Panel      : @@BASE_URL@@

  Backups are snapshots you take yourself from the panel.
  ------------------------------------------------------------------

ORION_MOTD
"""

# --------------------------------------------------------------------------- template bodies

LEMP_BODY = r"""
step "Installing Nginx, PHP-FPM and MariaDB"
apt-get install -y -qq --no-install-recommends   nginx mariadb-server php-fpm php-mysql php-cli php-curl php-gd php-mbstring   php-xml php-zip php-intl

systemctl enable --now nginx
systemctl enable --now mariadb

step "Creating the document root"
install -d -m 0755 -o www-data -g www-data /var/www/@@HOSTNAME@@
cat > /var/www/@@HOSTNAME@@/index.html <<'ORION_INDEX'
<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>@@HOSTNAME@@@@DOMAIN_SUFFIX@@</title>
<style>
 body{margin:0;background:#0f0b07;color:#f5e6d3;font:16px/1.6 system-ui,sans-serif;
      display:flex;min-height:100vh;align-items:center;justify-content:center}
 .c{max-width:34rem;padding:2rem}
 h1{margin:0 0 .5rem;font-size:1.6rem}
 .n{color:#f97316}
 p{color:#c4a882}
 code{background:#1f150d;border:1px solid #3d2814;padding:.15rem .4rem;border-radius:4px}
</style></head><body><div class="c">
<h1>@@BRAND@@<span class="n">Hosting</span></h1>
<p>Votre instance est en ligne. D&eacute;posez vos fichiers dans
<code>/var/www/@@HOSTNAME@@</code> ou connectez-vous en SSH.</p>
</div></body></html>
ORION_INDEX
chown -R www-data:www-data /var/www/@@HOSTNAME@@

step "Configuring the Nginx site"
cat > /etc/nginx/sites-available/@@HOSTNAME@@ <<'ORION_NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name @@HOSTNAME@@@@DOMAIN_SUFFIX@@ _;
    root /var/www/@@HOSTNAME@@;
    index index.php index.html;
    client_max_body_size 64m;

    location / { try_files $uri $uri/ /index.php?$args; }

    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php-fpm.sock;
        fastcgi_read_timeout 300;
    }

    location ~ /\.ht { deny all; }
}
ORION_NGINX
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/@@HOSTNAME@@ /etc/nginx/sites-enabled/@@HOSTNAME@@

# PHP-FPM listens on a unix socket owned by www-data on Debian; make sure the
# socket path used above actually exists.
for f in /etc/php/*/fpm/pool.d/www.conf; do
  [ -f "$f" ] || continue
  sed -i 's|^listen = .*|listen = /run/php/php-fpm.sock|' "$f"
  sed -i 's|^;*listen.owner = .*|listen.owner = www-data|' "$f"
  sed -i 's|^;*listen.group = .*|listen.group = www-data|' "$f"
done

systemctl restart php*-fpm 2>/dev/null || systemctl restart php-fpm 2>/dev/null || true
nginx -t
systemctl reload nginx
"""

WORDPRESS_BODY = r"""
@@LEMP@@

step "Installing WP-CLI"
curl -fsSL -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar
chmod +x /usr/local/bin/wp

step "Creating the WordPress database"
DB_NAME="wp_@@DBTOKEN@@"
DB_USER="wp_@@DBTOKEN@@"
DB_PASS="@@DBPASS@@"
mysql <<SQL
CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

step "Downloading WordPress"
cd /var/www/@@HOSTNAME@@
rm -f index.html index.nginx-debian.html
sudo -u www-data /usr/local/bin/wp core download --locale=fr_FR --force
sudo -u www-data /usr/local/bin/wp config create   --dbname="${DB_NAME}" --dbuser="${DB_USER}" --dbpass="${DB_PASS}"   --locale=fr_FR --skip-check --force
sudo -u www-data /usr/local/bin/wp core install   --url="https://@@HOSTNAME@@@@DOMAIN_SUFFIX@@"   --title="@@HOSTNAME@@ - @@BRAND@@"   --admin_user="@@WPADMIN@@"   --admin_password="@@WPPASS@@"   --admin_email="@@WPEMAIL@@"   --skip-email
sudo -u www-data /usr/local/bin/wp rewrite structure '/%postname%/' --hard
sudo -u www-data /usr/local/bin/wp option update blogdescription "H\u00e9berg\u00e9 par @@BRAND@@"

cat > /etc/nginx/sites-available/@@HOSTNAME@@ <<'ORION_NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name @@HOSTNAME@@@@DOMAIN_SUFFIX@@ _;
    root /var/www/@@HOSTNAME@@;
    index index.php;
    client_max_body_size 64m;

    location / { try_files $uri $uri/ /index.php?$args; }

    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php-fpm.sock;
        fastcgi_read_timeout 300;
    }

    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$ {
        expires max; log_not_found off;
    }
}
ORION_NGINX

chown -R www-data:www-data /var/www/@@HOSTNAME@@
systemctl reload nginx

printf '\nWordPress administrator: @@WPADMIN@@ / @@WPPASS@@\n'
printf 'WordPress URL         : https://@@HOSTNAME@@@@DOMAIN_SUFFIX@@\n'
"""

NODE_BODY = r"""
step "Installing Node.js LTS"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
apt-get install -y -qq nodejs
node --version

step "Writing the sample application"
useradd -m -s /bin/bash app 2>/dev/null || true
install -d -m 0755 -o app -g app /opt/app
cat > /opt/app/server.js <<'ORION_APP'
const http = require('http');
const os = require('os');
const port = process.env.PORT || 3000;
http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
  res.end(`<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>@@HOSTNAME@@</title>
<style>body{margin:0;background:#0f0b07;color:#f5e6d3;font:16px/1.6 system-ui,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}
.c{padding:2rem;max-width:34rem}h1{margin:0 0 .5rem;font-size:1.6rem}
.n{color:#f97316}p{color:#c4a882}code{background:#1f150d;border:1px solid #3d2814;
padding:.15rem .4rem;border-radius:4px}</style></head>
<body><div class="c"><h1>@@BRAND@@<span class="n">Hosting</span></h1>
<p>Application Node.js en ligne sur <code>@@HOSTNAME@@@@DOMAIN_SUFFIX@@</code>.</p>
<p>Node ${process.version} &middot; ${os.hostname()}</p></div></body></html>`);
}).listen(port, () => console.log('listening on ' + port));
ORION_APP
chown -R app:app /opt/app

cat > /etc/systemd/system/orion-app.service <<'ORION_UNIT'
[Unit]
Description=Sample Node application
After=network.target

[Service]
Type=simple
User=app
WorkingDirectory=/opt/app
ExecStart=/usr/bin/node /opt/app/server.js
Environment=PORT=3000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
ORION_UNIT
systemctl daemon-reload
systemctl enable --now orion-app

step "Fronting the application with Nginx"
apt-get install -y -qq --no-install-recommends nginx
cat > /etc/nginx/sites-available/@@HOSTNAME@@ <<'ORION_NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name @@HOSTNAME@@@@DOMAIN_SUFFIX@@ _;
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }
}
ORION_NGINX
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/@@HOSTNAME@@ /etc/nginx/sites-enabled/@@HOSTNAME@@
nginx -t
systemctl enable --now nginx
systemctl reload nginx
"""

DOCKER_BODY = r"""
step "Installing Docker"
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
usermod -aG docker "@@SSHUSER@@"
docker --version
docker run --rm hello-world >/dev/null && echo "Docker is working inside this instance."
"""

TEMPLATE_BODIES = {
    "bare": "",
    "lemp": LEMP_BODY,
    "wordpress": WORDPRESS_BODY,
    "node": NODE_BODY,
    "docker": DOCKER_BODY,
}

FOOTER = r"""
step "Cleaning up"
apt-get -y -qq autoremove >/dev/null 2>&1 || true
apt-get -y -qq clean >/dev/null 2>&1 || true
rm -f /root/orion-provision.sh

step "Provisioning finished"
uptime
"""


def build_script(*, template: str, hostname: str, ssh_user: str, password: str,
                 authorized_keys: str, plan, brand: str, base_url: str,
                 domain_suffix: str, timezone_name: str = "Europe/Paris",
                 locale: str = "fr_FR.UTF-8", wp_admin: str = "",
                 wp_pass: str = "", wp_email: str = "", db_token: str = "",
                 db_pass: str = "") -> str:
    tpl = get_template(template)

    has_keys = bool(authorized_keys.strip())
    body = TEMPLATE_BODIES.get(tpl.slug, "")
    if "@@LEMP@@" in body:
        body = body.replace("@@LEMP@@", LEMP_BODY)

    script = BASE_SCRIPT + body + FOOTER
    replacements = {
        "@@HOSTNAME@@": hostname,
        "@@SSHUSER@@": ssh_user,
        "@@PASSWORD@@": password,
        "@@AUTHORIZED_KEYS@@": authorized_keys.strip(),
        "@@ROOT_LOGIN@@": "prohibit-password" if has_keys else "yes",
        "@@PASSWORD_AUTH@@": "no" if has_keys else "yes",
        "@@TIMEZONE@@": timezone_name,
        "@@LOCALE@@": locale,
        "@@BRAND@@": brand,
        "@@BASE_URL@@": base_url,
        "@@DOMAIN_SUFFIX@@": domain_suffix,
        "@@PLAN_NAME@@": plan["name"] if plan else "custom",
        "@@PLAN_VCPU@@": str(plan["vcpu"] if plan else "-"),
        "@@PLAN_RAM@@": str(plan["memory_mb"] if plan else "-"),
        "@@PLAN_DISK@@": str(plan["disk_gb"] if plan else "-"),
        "@@CREATED_AT@@": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "@@WPADMIN@@": wp_admin,
        "@@WPPASS@@": wp_pass,
        "@@WPEMAIL@@": wp_email,
        "@@DBTOKEN@@": db_token,
        "@@DBPASS@@": db_pass,
    }
    for token, value in replacements.items():
        script = script.replace(token, value)
    return script


# --------------------------------------------------------------------------- engine

def incus_name_for(user_id: int, slug: str) -> str:
    return f"orion-{user_id}-{slug}"


def allocate_ssh_port() -> int:
    used = {row["ssh_port"] for row in db.query("SELECT ssh_port FROM instances WHERE ssh_port IS NOT NULL")}
    for port in range(settings.ssh_port_min, settings.ssh_port_max + 1):
        if port not in used:
            return port
    raise RuntimeError("no free SSH port in the configured range")


def _wait_for_ip(name: str, timeout: int = 120) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ip = incus.primary_ip(name)
        except IncusError:
            ip = ""
        if ip:
            return ip
        time.sleep(3)
    return ""


def _wait_for_exec(name: str, timeout: int = 180) -> bool:
    """The container is ready once it accepts a trivial exec.

    Deliberately not "wait for sshd": the base image ships no sshd, and it is
    the provisioning script itself that installs and starts it. Waiting for the
    daemon before running that script can only ever time out.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, _, _ = incus.exec(name, ["true"], timeout=20)
        except IncusError:
            rc = 1
        if rc == 0:
            return True
        time.sleep(3)
    return False


def provision(job_id: int, instance_id: int) -> str:
    """Create and configure one instance. Runs in a worker thread."""
    log: list[str] = []

    def emit(text: str) -> None:
        log.append(text)
        tasks.append_log(job_id, text + "\n")

    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        raise RuntimeError(f"instance {instance_id} vanished")

    owner = db.one("SELECT * FROM users WHERE id = ?", (instance["user_id"],))
    plan = db.one("SELECT * FROM plans WHERE id = ?", (instance["plan_id"],)) if instance["plan_id"] else None
    tpl = get_template(instance["template"])
    name = instance["incus_name"]

    def fail(message: str) -> None:
        db.execute("UPDATE instances SET status = 'error', error = ?, provision_log = ?, updated_at = ? WHERE id = ?",
                   (message, "\n".join(log)[-200_000:], now(), instance_id))
        activity.log("instance.provision_failed", user_id=instance["user_id"],
                     target=name, detail=message)

    try:
        emit(f"== Orion provisioning: {name} ({tpl.label})")
        info = detect(force=True)
        emit(f"== Host {info.host_name}: {info.cpu_total} vCPU, {info.ram_total_mb} MB RAM, pool {info.pool_total_gb} GB")

        # Ensure the account has somewhere to land before we create anything.
        if not incus.exists(name):
            emit(f"== Creating container (limits.cpu={plan['vcpu']}, limits.memory={plan['memory_mb']}MiB, root={plan['disk_gb']}GiB)")
            incus.create(name, vcpu=plan["vcpu"], memory_mb=plan["memory_mb"], disk_gb=plan["disk_gb"])
        else:
            emit("== Container already exists, reusing it")
            incus.apply_limits(name, vcpu=plan["vcpu"], memory_mb=plan["memory_mb"], disk_gb=plan["disk_gb"])

        ip = _wait_for_ip(name)
        emit(f"== Container address: {ip or 'unknown'}")
        if not ip:
            fail("The container never obtained an IP address.")
            return "\n".join(log)

        emit("== Waiting for the container to accept commands")
        if not _wait_for_exec(name):
            fail("The container never became responsive to exec.")
            return "\n".join(log)

        emit("== Publishing SSH on the host")
        ssh_port = instance["ssh_port"] or allocate_ssh_port()
        incus.set_ssh_proxy(name, ssh_port)

        db.execute("UPDATE instances SET container_ip = ?, ssh_port = ?, updated_at = ? WHERE id = ?",
                   (ip, ssh_port, now(), instance_id))

        # --- provisioning script ---
        from .util import rand_password

        wp_admin = "admin"
        wp_pass = rand_password(18)
        db_pass = rand_password(24)
        db_token = slugify(instance["slug"], "wp").replace("-", "")[:12]

        script = build_script(
            template=tpl.slug,
            hostname=instance["slug"],
            ssh_user=instance["ssh_user"],
            password=instance["initial_password"],
            authorized_keys=(owner["ssh_public_key"] if owner else "") or "",
            plan=plan,
            brand=settings.brand,
            base_url=settings.base_url,
            domain_suffix=f".{settings.domain}" if tpl.web else "",
            wp_admin=wp_admin, wp_pass=wp_pass,
            wp_email=(owner["email"] if owner else f"admin@{settings.domain}"),
            db_token=db_token, db_pass=db_pass,
        )

        if tpl.slug == "wordpress":
            db.set_setting(f"wp_creds_{instance_id}", f"{wp_pass}\n{db_pass}")

        emit("== Uploading and running the provisioning script")
        incus.push_text(name, script, "/root/orion-provision.sh", mode="0755")
        rc, out, err = incus.exec(name, "bash /root/orion-provision.sh", timeout=2400)
        emit(out)
        if err.strip():
            emit(err)
        if rc != 0:
            fail(f"Provisioning script exited with code {rc}. See the log above.")
            return "\n".join(log)

        rc, _, _ = incus.exec(name, "systemctl is-active ssh", timeout=30)
        if rc != 0:
            emit("!! sshd is not active after provisioning")

        # Order matters, and both wrong orders produce a wrong answer:
        #   route after "running" -> Caddy 404s for a few seconds
        #   route before "running" -> Caddy publishes a 503 page for an instance
        #                             it still sees as provisioning
        # So: register the route, mark the instance up, then publish the table -
        # the config Caddy receives already sees a running instance.
        if tpl.web:
            domain = sites.default_domain(instance["slug"])
            sites.add_site(instance_id, domain, tpl.port)

        db.execute("UPDATE instances SET status = 'running', error = '', updated_at = ? WHERE id = ?",
                   (now(), instance_id))

        if tpl.web:
            emit(f"== Website route created: {domain} -> {ip}:{tpl.port}")
            ok, message = sites.sync_caddy()
            emit(f"== Caddy routing {'applied' if ok else 'FAILED: ' + message}")

        emit("== Done. The instance is online.")
        activity.log("instance.provisioned", user_id=instance["user_id"], target=name,
                     detail=f"{tpl.label} · {ip} · ssh port {ssh_port}")
        return "\n".join(log)

    except IncusError as exc:
        fail(f"Incus error: {exc.detail}")
        return "\n".join(log)
    except Exception as exc:  # noqa: BLE001
        fail(f"Unexpected error: {exc}")
        raise


def destroy(instance_id: int) -> str:
    """Tear down an instance and everything pointing at it."""
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return "already gone"
    log = []
    for site in sites.list_sites(instance_id):
        sites.remove_site(site["id"])
        log.append(f"removed route {site['domain']}")
    try:
        if incus.exists(instance["incus_name"]):
            incus.delete(instance["incus_name"])
            log.append(f"deleted container {instance['incus_name']}")
    except IncusError as exc:
        log.append(f"incus: {exc.detail}")
    db.execute("UPDATE instances SET status = 'destroyed', updated_at = ? WHERE id = ?", (now(), instance_id))
    db.execute("DELETE FROM sites WHERE instance_id = ?", (instance_id,))
    sites.sync_caddy()
    activity.log("instance.destroyed", user_id=instance["user_id"], target=instance["incus_name"])
    return "\n".join(log)


def power(instance_id: int, action: str) -> tuple[bool, str]:
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return False, "instance not found"
    name = instance["incus_name"]
    try:
        if action == "start":
            incus.start(name)
            db.execute("UPDATE instances SET status = 'running', updated_at = ? WHERE id = ?", (now(), instance_id))
        elif action == "stop":
            incus.stop(name)
            db.execute("UPDATE instances SET status = 'stopped', updated_at = ? WHERE id = ?", (now(), instance_id))
        elif action == "restart":
            incus.restart(name)
            db.execute("UPDATE instances SET status = 'running', updated_at = ? WHERE id = ?", (now(), instance_id))
        else:
            return False, f"unknown action {action}"
    except IncusError as exc:
        return False, exc.detail
    if action in ("start", "stop"):
        ip = _wait_for_ip(name, timeout=30) if action == "start" else ""
        if ip:
            db.execute("UPDATE instances SET container_ip = ? WHERE id = ?", (ip, instance_id))
    sites.sync_caddy()
    activity.log(f"instance.{action}", user_id=instance["user_id"], target=name)
    return True, "ok"


def reprovision_web(instance_id: int) -> None:
    """Re-apply the SSH proxy after a move, used by the admin repair action."""
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance and instance["ssh_port"]:
        incus.set_ssh_proxy(instance["incus_name"], instance["ssh_port"])
