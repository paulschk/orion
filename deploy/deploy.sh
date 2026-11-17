#!/bin/bash
# Deploy or refresh the Orion panel on the machine that will host it.
# Idempotent: safe to re-run after a git pull or a config change.
set -euo pipefail

STACK_DIR="${ORION_STACK_DIR:-/opt/stacks/orion/docker}"
cd "${STACK_DIR}"

if [ ! -f .env ]; then
  echo "error: ${STACK_DIR}/.env is missing." >&2
  echo "       cp .env.example .env and set ORION_SECRET_KEY / admin credentials." >&2
  exit 1
fi

mkdir -p data caddy

# The tenant Caddy needs a syntactically valid config on first start; the panel
# replaces it immediately afterwards over the admin API.
if [ ! -f caddy/caddy.json ]; then
  cp caddy/bootstrap.json caddy/caddy.json
  echo "seeded caddy/caddy.json from bootstrap.json"
fi

docker compose up -d --build
echo
docker compose ps
echo
echo "panel logs:  docker compose logs -f orion"
