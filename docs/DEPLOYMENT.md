# Deployment

Reference deployment: `hosting.node29.fr`, on `lilith` (Debian 12, i5-6500T,
16 GB RAM, 80 GiB pool, behind Caddy and Tailscale).

## 1. Host preparation

As root on the machine that will carry the instances:

```bash
git clone <repo> /root/orion && cd /root/orion
./deploy/incus-host-setup.sh
```

This installs Incus from the Zabbly repository, creates the btrfs pool and the
`incusbr0` bridge, configures the default profile (nesting on, IPv6 off inside
instances) and installs the Docker/Incus forwarding fix.

Verify:

```bash
incus storage list
incus launch images:debian/12 probe -c limits.cpu=1 -c limits.memory=1GiB -d root,size=8GiB
incus exec probe -- bash -c "nproc; free -m | sed -n 2p; curl -sI https://deb.debian.org | head -1"
incus delete -f probe
```

The container must reach the internet. If it cannot, the `DOCKER-USER` rules are
missing — see the architecture note.

## 2. Panel configuration

```bash
cd /opt/stacks/orion
cp docker/.env.example docker/.env
```

Set at minimum `ORION_SECRET_KEY`, `ORION_ADMIN_EMAIL` and
`ORION_ADMIN_PASSWORD`. Generate a key with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`ORION_SSH_HOST` is what clients are told to connect to. It must be reachable
from where clients are — on the reference deployment, the panel's own hostname.

## 3. Start the panel

```bash
cd /opt/stacks/orion/docker
docker compose up -d --build
docker compose logs -f orion
```

Look for:

```
tenant routing: applied (ok)
```

The stack is two containers:

* `orion` — the panel. Needs the host's `/var/lib/incus/unix.socket`
  bind-mounted, and nothing else. It never runs privileged.
* `orion-caddy` — the tenant router. Admins its own config over :2019, joined to
  the `gpi` network so the edge Caddy resolves it by name.

## 4. Edge Caddy

Append `deploy/caddy/hosting.node29.fr.caddy` to `/srv/caddy/Caddyfile`, then:

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

TLS is issued over the ACME DNS-01 challenge (already configured globally as
`acme_dns cloudflare`), so no inbound port is required for the certificate.

## 5. DNS

```
hosting.node29.fr.          A   <host address>
*.hosting.node29.fr.        A   <host address>
```

Both records DNS-only. Point them wherever "reachable by your clients" means:
the LAN address, or the host's Tailscale address to make the panel genuinely
tailnet-only (the Caddy block already returns 403 to anything outside
`192.168.1.0/24` and `100.64.0.0/10`).

## 6. First login

Open `https://hosting.node29.fr`, sign in with `ORION_ADMIN_EMAIL` /
`ORION_ADMIN_PASSWORD`, and change the password immediately.

The first boot seeds four plans (Starter 3 €, Standard 6 €, Pro 12 €,
Business 24 €) and records the host fingerprint.

## 7. End-to-end check

1. Register a client account at `/register`.
2. **Nouvelle instance** → Standard → *Site web (LEMP)* → SSH public key → create.
3. The status goes `En cours` → `En ligne`; the SSH block fills in.
4. Open `http://<slug>.hosting.node29.fr` — the placeholder page appears.
5. `ssh -p <port> orion@<ORION_SSH_HOST>` works with the supplied key.
6. Take a snapshot, restore it, destroy the instance, confirm the route
   disappears from `/admin/sites`.

## Operating notes

* **Backups.** The panel's state is `docker/data/orion.db` plus
  `/var/lib/incus/disks/orion.img`. Back up both; the pool holds tenant data.
* **Upgrades.** `docker compose up -d --build` then reload the edge Caddy if the
  site block changed. Instances are untouched.
* **Moving host.** Restore `orion.db` and the pool file, start the panel; the
  fingerprint check will log the move.
* **Pool growth.** `incus storage set orion size=120GiB` resizes the loop file.
