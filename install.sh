#!/usr/bin/env bash
#
# Orion - one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/paulschk/orion/main/install.sh \
#     | sudo bash -s -- --domain hosting.example.com --admin you@example.com
#
# It fetches the source, prepares the host (Incus, storage pool, bridge, the
# Docker forwarding fix), writes a .env with a generated secret key, starts the
# panel, and prints the two things it cannot do for you: the edge Caddy block and
# the DNS records. Nothing is deleted or overwritten: an existing .env is left
# alone, an existing directory that is not a git checkout is refused.
#
#   --domain HOST        panel hostname                 (prompted if absent)
#   --admin EMAIL        first admin account            (prompted if absent)
#   --admin-password PWD  generated if absent
#   --ingress-ip IP      public address clients point their own domains at
#                        (detected if absent)
#   --dir PATH           where to put the source        (default /opt/orion)
#   --network NAME       Docker network shared with the edge Caddy (default gpi)
#   --update             update an install that is already there
#   --dry-run            print every action, change nothing
#   --yes                never prompt
set -euo pipefail

REPO_URL="${ORION_REPO:-https://github.com/paulschk/orion.git}"
DIR="${ORION_DIR:-/opt/orion}"
DOMAIN="" ADMIN="" ADMIN_PASSWORD="" INGRESS_IP="" NETWORK="gpi"
DRY=0 UPDATE=0 ASSUME_YES=0

log()  { printf '\n\033[1;33m== %s\033[0m\n' "$1"; }
note() { printf '   %s\n' "$1"; }
die()  { printf '\nerror: %s\n' "$1" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

usage() {
  cat <<'HELP'
Orion installer.

  curl -fsSL https://raw.githubusercontent.com/paulschk/orion/main/install.sh \
    | sudo bash -s -- --domain hosting.example.com --admin you@example.com

  --domain HOST         panel hostname                 (prompted if absent)
  --admin EMAIL         first admin account            (prompted if absent)
  --admin-password PWD  generated if absent
  --ingress-ip IP       public address clients point their own domains at
                        (detected if absent)
  --dir PATH            where to put the source        (default /opt/orion)
  --network NAME        Docker network shared with the edge Caddy (default gpi)
  --update              update an install that is already there
  --dry-run             print every action, change nothing
  --yes                 never prompt

Nothing is deleted or overwritten: an existing .env is left alone, and a target
directory that is not a git checkout is refused rather than clobbered.
HELP
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)         DOMAIN="${2:-}"; shift 2 ;;
    --admin)          ADMIN="${2:-}"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="${2:-}"; shift 2 ;;
    --ingress-ip)     INGRESS_IP="${2:-}"; shift 2 ;;
    --dir)            DIR="${2:-}"; shift 2 ;;
    --network)        NETWORK="${2:-}"; shift 2 ;;
    --update)         UPDATE=1; shift ;;
    --dry-run)        DRY=1; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

[ "$(id -u)" = "0" ] || die "run this as root:  curl -fsSL <url> | sudo bash -s -- --domain ... --admin ..."
command -v git    >/dev/null 2>&1 || die "git is not installed"
command -v docker >/dev/null 2>&1 || die "Docker is not installed (or not in PATH)"

# A second panel would fight the first for container names and ports. Refuse
# rather than build one, and say where the first one probably is.
if [ "$UPDATE" = 0 ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx orion; then
  die "a panel named 'orion' is already running.
       To update it:  re-run with --update --dir <its source directory>"
fi

log "Orion installer"
note "source : $DIR"
note "domain : ${DOMAIN:-<to be asked>}"
note "network: $NETWORK"
[ "$DRY" = 1 ] && note "dry run: nothing will be changed"

log "1/5  Fetching the source"
if [ -d "$DIR/.git" ]; then
  run git -C "$DIR" pull --ff-only --quiet
elif [ -e "$DIR" ]; then
  die "$DIR exists and is not a git checkout - refusing to touch it.
       Move it aside, or pass --dir to install somewhere else."
else
  run git clone --quiet --depth 1 "$REPO_URL" "$DIR"
fi

log "2/5  Preparing the host (Incus, storage pool, bridge, Docker forwarding)"
note "idempotent - safe to re-run"
run bash "$DIR/deploy/incus-host-setup.sh"

log "3/5  Writing $DIR/docker/.env"
ENV_FILE="$DIR/docker/.env"
if [ -f "$ENV_FILE" ] && [ "$UPDATE" = 1 ]; then
  note "already there - left untouched"
elif [ -f "$ENV_FILE" ]; then
  note "already there - left untouched (pass --update next time)"
else
  if [ -z "$DOMAIN" ]; then
    [ "$ASSUME_YES" = 1 ] && die "--domain is required with --yes"
    read -rp "   Panel domain (e.g. hosting.example.com): " DOMAIN
  fi
  if [ -z "$ADMIN" ]; then
    [ "$ASSUME_YES" = 1 ] && die "--admin is required with --yes"
    read -rp "   Admin email: " ADMIN
  fi
  [ -n "$DOMAIN" ] || die "a domain is required"
  [ -n "$ADMIN" ]  || die "an admin email is required"

  if [ -z "$ADMIN_PASSWORD" ]; then
    if command -v openssl >/dev/null 2>&1; then
      ADMIN_PASSWORD="$(openssl rand -base64 18)"
    else
      ADMIN_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | cut -c1-20)"
    fi
    GENERATED_PASSWORD=1
  else
    GENERATED_PASSWORD=0
  fi

  if [ -z "$INGRESS_IP" ]; then
    INGRESS_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    [ -n "$INGRESS_IP" ] && note "public address detected: $INGRESS_IP"
  fi

  SECRET="$(openssl rand -base64 48 2>/dev/null | tr -d '\n' || head -c 48 /dev/urandom | base64 | tr -d '\n')"
  if [ "$DRY" = 1 ]; then
    note "[dry-run] would write $ENV_FILE with:"
    note "  ORION_DOMAIN=$DOMAIN  ORION_ADMIN_EMAIL=$ADMIN"
    note "  ORION_SECRET_KEY=<48 random bytes>  ORION_INGRESS_IP=${INGRESS_IP:-}"
  else
    mkdir -p "$DIR/docker"
    sed -e "s|^ORION_DOMAIN=.*|ORION_DOMAIN=$DOMAIN|" \
        -e "s|^ORION_BASE_URL=.*|ORION_BASE_URL=https://$DOMAIN|" \
        -e "s|^ORION_SECRET_KEY=.*|ORION_SECRET_KEY=$SECRET|" \
        -e "s|^ORION_ADMIN_EMAIL=.*|ORION_ADMIN_EMAIL=$ADMIN|" \
        -e "s|^ORION_ADMIN_PASSWORD=.*|ORION_ADMIN_PASSWORD=$ADMIN_PASSWORD|" \
        -e "s|^ORION_INGRESS_IP=.*|ORION_INGRESS_IP=$INGRESS_IP|" \
        -e "s|^ORION_SSH_HOST=.*|ORION_SSH_HOST=$DOMAIN|" \
        "$DIR/docker/.env.example" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    note "written, mode 600"
  fi
fi

log "4/5  Docker network the panel joins"
if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  note "'$NETWORK' already exists"
else
  run docker network create "$NETWORK"
fi

log "5/5  Starting the panel"
run bash "$DIR/deploy/deploy.sh"

log "Done"
if [ "$DRY" = 1 ]; then
  note "dry run: nothing was changed"
  exit 0
fi

printf '   panel   : https://%s\n' "${DOMAIN:-<your domain>}"
if [ "${GENERATED_PASSWORD:-0}" = 1 ] && [ -f "$ENV_FILE" ]; then
  printf '   admin   : %s / %s\n' "$ADMIN" "$ADMIN_PASSWORD"
  printf '             (generated - change it after the first login)\n'
fi

cat <<NEXT

   Two things are yours to do, and the installer will not guess them:

   1. TLS. Append this block to your edge Caddyfile, with your real hostname:
        $DIR/deploy/caddy/hosting.node29.fr.caddy
      and reload that Caddy:
        docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
      It sends the apex to the panel and every subdomain to the tenant router.
      The apex answers 403 to anything outside the LAN; the tenant wildcard is a
      block of its own, because those are the clients' sites and are meant to be
      seen. Delete the `handle @external` block to expose the panel.

   2. DNS. Two A records, DNS-only (grey cloud) at Cloudflare, pointing at this
      host's public address - the wildcard is what gives every tenant site a
      certificate:
        ${DOMAIN:-<domain>}       A   ${INGRESS_IP:-<public address>}
        *.${DOMAIN:-<domain>}     A   ${INGRESS_IP:-<public address>}

   Then open https://${DOMAIN:-<your domain>} and log in.

NEXT
