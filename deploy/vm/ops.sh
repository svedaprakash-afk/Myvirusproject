#!/usr/bin/env bash
# Day-2 operations. Run from deploy/vm on the VM.
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose"
case "${1:-help}" in
  status)   $C ps; echo; curl -fsS "https://$(. ./.env; echo "$DOMAIN")/health/ready" && echo; echo "last good backup: $($C exec -T backup cat /backups/LAST_OK 2>/dev/null || echo NONE)";;
  logs)     shift; $C logs --tail=200 -f "${@:-api}";;
  doctor)   $C run --rm -T api python -m app.cli doctor;;
  backup)   $C exec -T backup /scripts/backup.sh;;
  backups)  $C exec -T backup ls -1 /backups;;
  drill)    B="${2:-$($C exec -T backup cat /backups/LAST_OK | tr -d '\r')}"; $C exec -T backup /scripts/restore.sh "/backups/$B" --verify;;
  restore)  B="${2:?usage: ops.sh restore <backup-name>}"
            read -r -p "This REPLACES the live database with backup $B. Type RESTORE to continue: " a; [ "$a" = RESTORE ] || exit 1
            $C stop api worker web; $C exec -T backup /scripts/restore.sh "/backups/$B" --apply; $C run --rm migrate; $C up -d --wait api worker web;;
  upgrade)  $C exec -T backup /scripts/backup.sh          # always back up first
            git pull --ff-only; $C build; $C up -d --wait db; $C run --rm migrate; $C up -d --wait; $C run --rm -T api python -m app.cli doctor;;
  create-user)   shift; read -r -s -p "Password (min 12 chars): " P; echo; $C run --rm -T -e P="$P" api python -m app.cli create-user "$@" --password-env P;;
  reset-password) shift; read -r -s -p "New password (min 12 chars): " P; echo; $C run --rm -T -e P="$P" api python -m app.cli reset-password "$@" --password-env P;;
  create-entity) shift; $C run --rm -T api python -m app.cli create-entity "$@";;
  shell-db) $C exec db psql -U cashapp cashapp;;
  *) echo "usage: ./ops.sh <status|logs [svc]|doctor|backup|backups|drill [name]|restore <name>|upgrade|create-user <email> --name N --role R|reset-password <email>|create-entity ...|shell-db>";;
esac
