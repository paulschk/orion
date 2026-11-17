# Architecture

## The shape of it

```
                    internet / LAN / tailnet
                              │
                    ┌─────────▼──────────┐
                    │  edge Caddy (host) │   TLS (DNS-01 wildcard), 403 off-LAN
                    │  :443 → :8443      │
                    └───┬────────────┬───┘
        hosting.node29.fr│            │*.hosting.node29.fr
                 ┌───────▼─────┐  ┌───▼──────────┐
                 │ orion:8000  │  │ orion-caddy  │  routing table pushed over
                 │  (panel)    │  │   :8080      │  Caddy's admin API
                 └──────┬──────┘  └───┬──────────┘
                        │             │ reverse_proxy
            ┌───────────┘             │
            │ Incus unix socket       │
   ┌────────▼─────────┐               │
   │  incusd (host)   │               │
   │  pool: orion     │               │
   │  bridge incusbr0 │               │
   └────────┬─────────┘               │
   ┌────────▼─────────────────────────▼──────────┐
   │ orion-<uid>-<slug>  × N   (Incus containers)│
   │   real quotas · systemd · sshd · HTTP :80   │
   └─────────────────────────────────────────────┘
```

## Why containers and not VMs

Incus **system containers** give a machine-like experience — `systemd` is PID 1,
`apt` works, the tenant is root — while costing a few tens of megabytes of
runtime each. On a single host that is the difference between selling four
instances and selling twenty. Kernel isolation is shared, which is an honest
trade for "cheap small VPS" and is stated plainly on the tariff page.

KVM is not required and is not used: `incus info` on the reference host reports
`Instance type not operational: KVM support is missing`, and containers are the
intended target anyway.

## Host detection

`orion/hostinfo.py` asks the Incus daemon, not `/proc`:

| figure | source |
|---|---|
| vCPU total, model | `GET /1.0/resources` |
| RAM total | `GET /1.0/resources` |
| storage total / used | `GET /1.0/storage-pools/<pool>/resources` |
| distro, kernel, Incus version, firewall | `GET /1.0` |

Sellable capacity is `total − reserve`, where the reserve (default 1 vCPU,
1536 MB, 10 GB) is what the panel and the OS keep for themselves. The admin
dashboard shows capacity per plan as `min(sellable_vcpu // plan.vcpu,
sellable_ram // plan.ram, sellable_disk // plan.disk)`, and instance creation is
refused when a plan would overshoot the host.

An operator override (`ORION_CPU_TOTAL`, `ORION_RAM_TOTAL_MB`,
`ORION_DISK_TOTAL_GB`) exists for deliberate oversubscription or capping.

The host fingerprint (`name:cpu:ram:pool`) is stored at first boot; if it
changes, the panel logs that it has been moved to a different machine.

## Storage

Tenant data lives in a **btrfs pool on a sparse loop file**
(`/var/lib/incus/disks/orion.img`, 80 GiB by default). This is deliberate:

* the host root filesystem is usually ext4 without project quotas, so Incus'
  `dir` driver cannot enforce per-instance disk limits;
* btrfs subvolume qgroups enforce them for real — verified by writing into a
  container with an 8 GiB quota until `dd` failed at 7.6 GiB;
* it also gives copy-on-write snapshots, which is what backs the
  "instant snapshot before an upgrade" feature, and needs no reboot and no
  out-of-tree kernel module.

Disk quota is set on the root disk device (`-d root,size=8GiB`), not via the
removed `limits.disk` key.

## Networking

Instances attach to `incusbr0` (10.42.0.0/24, NAT). IPv6 is disabled inside
instances at the LXC level, because the bridge carries no v6 route and Debian
images otherwise resolve AAAA records first and hang on `apt`.

Client web traffic goes: edge Caddy → `orion-caddy:8080` (Docker network) →
container IP on `incusbr0`. Client SSH goes: `ssh -p <20000+n> user@<host>` →
an Incus **proxy device** on the host → `127.0.0.1:22` inside the container.
Each instance therefore consumes exactly one high port on the host, allocated
from `ORION_SSH_PORT_MIN..MAX`.

### The Incus/Docker forwarding trap

Both daemons install chains on nftables' `forward` hook at priority `filter`.
Docker sets `policy drop` on its chain and Docker's chain is evaluated first, so
packets leaving `incusbr0` are dropped and **tenant containers have no internet
at all** — while the host itself works perfectly, which makes it look like a
DNS problem.

The fix lives in `DOCKER-USER`, which is jumped from `FORWARD` ahead of that
policy and is never flushed by Docker:

```
iptables -I DOCKER-USER 1 -i incusbr0 -j ACCEPT
iptables -I DOCKER-USER 2 -o incusbr0 -j ACCEPT
```

`deploy/incus-host-setup.sh` installs this plus a systemd unit so it is
re-applied on every boot, after `docker.service`.

## Tenant routing

Orion does not edit a Caddyfile. It generates the **entire** tenant routing
table as JSON and `POST`s it to `orion-caddy`'s admin API in one call, so a
routing change is atomic. The same JSON is written to
`/data/caddy/caddy.json`, which is bind-mounted next to the Caddy container, so
a restart of either side comes back with the last known config.

Each route is `match host == <domain>` → `reverse_proxy <container_ip>:<port>`;
when the instance is stopped or has no address, the route degrades to a
friendly 503 page instead of a connection error. Anything unmatched gets a 404
page in the panel's own style.

## Background work

Provisioning takes a minute or more, so it never happens inside a request.
`orion/tasks.py` runs one daemon thread per job and records progress in the
`jobs` table; the instance page polls a fragment endpoint while the status is
`provisioning`. Jobs survive nothing — a panel restart mid-provision leaves the
instance in `provisioning`, which is exactly what the admin **Réparer** action
is for.

## Data model

`users`, `plans`, `instances`, `sites`, `subscriptions`, `invoices`,
`sessions`, `activity`, `settings`, `jobs` — see `orion/db.py`. SQLite in WAL
mode; one writer, one host, comfortably within its envelope.

## Deliberate omissions

* No scheduler process, no message broker, no Redis.
* No generated API clients, no SPA build step.
* No multi-host orchestration. Orion is explicitly one host; that is the point.
