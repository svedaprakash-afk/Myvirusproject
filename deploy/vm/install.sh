#!/usr/bin/env bash
# First-time install on a fresh Linux VM that has Docker Engine + the compose plugin. Safe to re-run: it never overwrites an existing .env.
#   ./install.sh                       asks for the domain, region, company details
#   DOMAIN=cash.example.in DEPLOY_REGION=ap-south-1 ENTITY_NAME="Acme Pvt Ltd" ADMIN_EMAIL=you@acme.in ./install.sh   non-interactive
set -euo pipefail
cd "$(dirname "$0")"
need() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
need docker; need openssl; docker compose version >/dev/null || { echo "docker compose plugin missing"; exit 1; }
ask() { local var="$1" prompt="$2" def="${3:-}"; if [ -z "${!var:-}" ]; then read -r -p "$prompt${def:+ [$def]}: " v; printf -v "$var" '%s' "${v:-$def}"; fi; }

if [ ! -f .env ]; then
  ask DOMAIN "Domain name (DNS A record must already point at this VM)"
  ask DEPLOY_REGION "Indian region of this VM" "ap-south-1"
  [ -n "$DOMAIN" ] || { echo "a domain is required (for a pilot without one, use <public-ip>.sslip.io)"; exit 1; }
  umask 077
  cp .env.example .env
  set_env() { sed -i "s|^$1=.*|$1=$2|" .env; }
  set_env DOMAIN "$DOMAIN"; set_env DEPLOY_REGION "$DEPLOY_REGION"
  set_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  set_env JWT_SECRET "$(openssl rand -hex 32)"
  set_env BACKUP_PASSPHRASE "$(openssl rand -base64 33 | tr -d '\n')"
  chmod 600 .env
  echo ">> .env created (mode 600)."
  echo ">> COPY BACKUP_PASSPHRASE FROM .env TO A PASSWORD MANAGER OFF THIS MACHINE NOW. Without it no backup can be restored."
  read -r -p "Press Enter once you have saved it... " _
fi
set -a; . ./.env; set +a

echo ">> building images (first time takes a few minutes)"
docker compose build
echo ">> starting database, migrating schema, starting services"
docker compose up -d --wait db
docker compose run --rm migrate
docker compose up -d --wait api web caddy worker backup

# first administrator and company
if [ "$(docker compose run --rm -T api python -m app.cli doctor 2>/dev/null | grep -c 'FAIL.*administrator')" -ge 1 ]; then
  ask ADMIN_EMAIL "Administrator email"
  ask ADMIN_NAME "Administrator name" "Administrator"
  ADMIN_PASSWORD="$(openssl rand -base64 15 | tr -d '/+=\n')-Aa1"
  docker compose run --rm -T -e ADMIN_PW="$ADMIN_PASSWORD" api python -m app.cli create-user "$ADMIN_EMAIL" --name "$ADMIN_NAME" --role admin --password-env ADMIN_PW
  echo "================================================================"
  echo " First sign-in:  https://$DOMAIN"
  echo " Email:          $ADMIN_EMAIL"
  echo " Password:       $ADMIN_PASSWORD     (shown once; change it after signing in)"
  echo "================================================================"
fi
if docker compose run --rm -T api python -m app.cli doctor 2>/dev/null | grep -q 'FAIL.*legal entity'; then
  ask ENTITY_NAME "Company (legal entity) name"
  ask ENTITY_GSTIN "GSTIN (blank to skip)" ""
  ask BANK "Main bank account as 'Bank name|account number|IFSC|ledger code' (blank to add later)" ""
  args=(--name "$ENTITY_NAME"); [ -n "${ENTITY_GSTIN:-}" ] && args+=(--gstin "$ENTITY_GSTIN"); [ -n "${BANK:-}" ] && args+=(--bank "$BANK")
  docker compose run --rm -T api python -m app.cli create-entity "${args[@]}"
fi

echo ">> readiness report"
docker compose run --rm -T api python -m app.cli doctor || echo "(fix the FAIL lines above, then run ./ops.sh doctor)"
echo ">> done. Next: docs/RUNBOOK_VM.md, section 'First day'."
