#!/bin/bash
# Orion — host preparation for the machine that will run tenant instances.
#
# Idempotent. Tested on Debian 12 (bookworm). Run as root on the *host*, not in
# a container. It installs Incus, creates the storage pool and bridge, and
# works around the known Incus/Docker forwarding conflict.
set -euo pipefail

POOL_NAME="${ORION_POOL:-orion}"
POOL_SIZE="${ORION_POOL_SIZE:-80GiB}"
POOL_FILE="${ORION_POOL_FILE:-/var/lib/incus/disks/${POOL_NAME}.img}"
BRIDGE="${ORION_BRIDGE:-incusbr0}"
BRIDGE_CIDR="${ORION_BRIDGE_CIDR:-10.42.0.1/24}"

log() { printf '\n\033[1;33m== %s\033[0m\n' "$1"; }

[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 1; }

log "Installing Incus (Zabbly repository, bookworm)"
if ! command -v incus >/dev/null 2>&1; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc
  cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources <<'SOURCES'
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: bookworm
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/zabbly.asc
SOURCES
  apt-get update -qq
  apt-get install -y --no-install-recommends incus btrfs-progs
fi
systemctl enable --now incus
sleep 3

log "Creating the storage pool (${POOL_NAME}, btrfs on ${POOL_FILE})"
# Root filesystems are usually ext4 without project-quota support, which means
# Incus' 'dir' driver cannot enforce per-instance disk quotas. A btrfs pool on a
# sparse loop file gives real quotas and snapshots with no reboot and no
# out-of-tree kernel module.
if ! incus storage list --format csv | cut -d, -f1 | grep -qx "${POOL_NAME}"; then
  install -d -m 0700 /var/lib/incus/disks
  incus storage create "${POOL_NAME}" btrfs source="${POOL_FILE}" size="${POOL_SIZE}"
fi

log "Creating the instance bridge (${BRIDGE})"
if ! incus network list --format csv | cut -d, -f1 | grep -qx "${BRIDGE}"; then
  incus network create "${BRIDGE}" \
    ipv4.address="${BRIDGE_CIDR}" ipv4.nat=true ipv6.address=none dns.mode=managed
fi

log "Configuring the default profile"
# Instances sit on a bridge with no IPv6 route, so AAAA lookups fail and apt
# hangs on Debian/Ubuntu images. Disable IPv6 inside the container instead.
cat > /tmp/orion-profile.yaml <<'YAML'
description: Orion default profile
config:
  security.nesting: "true"
  raw.lxc: |
    lxc.sysctl.net.ipv6.conf.all.disable_ipv6=1
    lxc.sysctl.net.ipv6.conf.default.disable_ipv6=1
YAML
incus profile edit default < /tmp/orion-profile.yaml
rm -f /tmp/orion-profile.yaml

log "Applying the Incus/Docker forwarding fix"
# Both Incus (nftables) and Docker (iptables-nft) install chains on the forward
# hook at priority "filter". Docker sets policy drop on its chain and it is
# evaluated first, so packets leaving the Incus bridge are dropped and tenant
# containers have no internet. DOCKER-USER is jumped from FORWARD ahead of that
# policy and Docker never flushes it, which makes it the correct place to fix.
cat > /usr/local/sbin/orion-incus-docker-forward.sh <<'FIX'
#!/bin/bash
set -e
iptables -C DOCKER-USER -i __BRIDGE__ -j ACCEPT 2>/dev/null || \
  iptables -I DOCKER-USER 1 -i __BRIDGE__ -j ACCEPT
iptables -C DOCKER-USER -o __BRIDGE__ -j ACCEPT 2>/dev/null || \
  iptables -I DOCKER-USER 2 -o __BRIDGE__ -j ACCEPT
FIX
sed -i "s|__BRIDGE__|${BRIDGE}|g" /usr/local/sbin/orion-incus-docker-forward.sh
chmod 0755 /usr/local/sbin/orion-incus-docker-forward.sh

cat > /etc/systemd/system/orion-incus-docker-forward.service <<UNIT
[Unit]
Description=Orion: allow ${BRIDGE} traffic through Docker's FORWARD policy
After=docker.service network-online.target
Wants=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/orion-incus-docker-forward.sh
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now orion-incus-docker-forward.service

log "Verifying"
incus storage list
incus network list --format compact | grep -E "NAME|${BRIDGE}" || true
incus profile show default | head -6

cat <<DONE

Host preparation finished.

Next:
  1. Deploy the panel (see docs/DEPLOYMENT.md) - it needs
     /var/lib/incus/unix.socket bind-mounted and nothing else.
  2. Give the panel host a DNS record; tenant subdomains are handled
     by a wildcard.

DONE
