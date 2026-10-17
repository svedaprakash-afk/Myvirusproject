#!/bin/bash
# Decrypt and restore one backup.
#   restore.sh <backup-dir> --verify      restore the database into a throwaway database, count rows, drop it (a restore drill; touches nothing live)
#   restore.sh <backup-dir> --apply       REPLACE the live database (and exports/feeds) with the backup. Stop api/worker first.
# Environment: PGHOST PGUSER PGPASSWORD PGDATABASE BACKUP_PASSPHRASE; optional DATA_DIR
set -euo pipefail
B="${1:?usage: restore.sh <backup-dir> --verify|--apply}"; MODE="${2:?--verify or --apply}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is required}"; DATA="${DATA_DIR:-/data}"
dec() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass env:BACKUP_PASSPHRASE; }
(cd "$B" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo "MANIFEST check FAILED: the backup is damaged or incomplete" >&2; exit 2; }
[ "$(dec < "$B/db.dump.enc" 2>/dev/null | head -c 5 || true)" = "PGDMP" ] || { echo "cannot decrypt: wrong BACKUP_PASSPHRASE or damaged file" >&2; exit 3; }
case "$MODE" in
  --verify)
    tmpdb="restore_drill_$$"; psql -d postgres -qc "CREATE DATABASE $tmpdb"
    trap 'psql -d postgres -qc "DROP DATABASE IF EXISTS $tmpdb" >/dev/null' EXIT
    dec < "$B/db.dump.enc" | pg_restore --no-owner -d "$tmpdb"
    for t in users payment_transactions invoices audit_log; do printf '%-12s %s\n' "$t" "$(psql -d "$tmpdb" -Atc "SELECT count(*) FROM $t" 2>/dev/null || echo n/a)"; done
    [ -f "$B/files.tar.enc" ] && dec < "$B/files.tar.enc" | tar -tf - >/dev/null && echo "files archive readable"
    echo "RESTORE DRILL OK: $B";;
  --apply)
    dec < "$B/db.dump.enc" | pg_restore --no-owner --clean --if-exists -d "$PGDATABASE"
    [ -f "$B/files.tar.enc" ] && dec < "$B/files.tar.enc" | tar -C "$DATA" -xf -
    echo "RESTORED from $B. Start api and worker.";;
  *) echo "unknown mode $MODE" >&2; exit 1;;
esac
