# Orion

> Lightweight, self-hosted VPS hosting panel. One host, many small servers.

Orion turns a single machine into a small hosting provider. It detects the
hardware it is deployed on, works out how many *virtual light servers* that
machine can genuinely carry, and provisions them as
[Incus](https://linuxcontainers.org/incus/) system containers — with real
CPU/RAM/disk quotas, snapshots and SSH access, the way a classic web host
works but far cheaper to run.

It ships two interfaces:

* an **admin dashboard** — host capacity, clients, instances, plans, invoices,
  activity log, per-instance console;
* a **client dashboard** — provision and manage instances, pick a template,
  start/stop/restart, snapshots, web console, SSH credentials, invoices.

Tenant websites are routed through a dedicated Caddy instance whose
configuration Orion generates and pushes over Caddy's admin API.

## Why

Renting a VPS is expensive for a small site. If you already run a machine, the
marginal cost of a container is nearly zero. Orion is the panel for that:
containerised VPS, one flat host, a simple dashboard instead of a complex
platform.

## Status

Proof of concept, deployed as `hosting.node29.fr`. See `docs/` for the
architecture, the deployment runbook and the security posture. Billing is
mocked (see `docs/BILLING.md`).

## Requirements

* Linux host with Incus installed (Debian 12 walkthrough in `docs/DEPLOYMENT.md`)
* Docker + Docker Compose for the panel itself
* Caddy in front for TLS (`deploy/caddy/`)

## Quick start

```bash
# 1. Host preparation (Incus repo, storage pool, network, Docker coexistence)
sudo deploy/incus-host-setup.sh

# 2. Panel
cd docker
cp .env.example .env          # set ORION_SECRET_KEY and friends
docker compose up -d --build
```

Then follow `docs/DEPLOYMENT.md` for the Caddy site block and first admin user.

## Licence

MIT — see `LICENSE`.
