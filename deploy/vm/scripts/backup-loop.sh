#!/bin/bash
# Runs inside the backup container: one encrypted backup a day at BACKUP_HOUR_UTC, plus one right at start-up if none exists.
set -u
HOUR="${BACKUP_HOUR_UTC:-20}"
while true; do
  if [ ! -e /backups/LAST_OK ]; then /scripts/backup.sh || true; fi
  now=$(date -u +%s); target=$(date -u -d "today ${HOUR}:00" +%s)
  [ "$target" -le "$now" ] && target=$(date -u -d "tomorrow ${HOUR}:00" +%s)
  sleep $((target - now))
  /scripts/backup.sh || echo "backup FAILED at $(date -u +%FT%TZ)" >&2
done
