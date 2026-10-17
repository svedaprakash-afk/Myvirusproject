#!/bin/bash
# Encrypted backup: pg_dump (custom format) + exports/feeds tarball, each AES-256-CBC with PBKDF2, then a SHA-256 manifest.
# Environment: PGHOST PGUSER PGPASSWORD PGDATABASE BACKUP_PASSPHRASE; optional BACKUP_DIR BACKUP_KEEP_DAYS DATA_DIR
set -euo pipefail
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is required}"
DIR="${BACKUP_DIR:-/backups}"; KEEP="${BACKUP_KEEP_DAYS:-14}"; DATA="${DATA_DIR:-/data}"
ts=$(date -u +%Y%m%dT%H%M%SZ); out="$DIR/$ts"; mkdir -p "$out.partial"; umask 077
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass env:BACKUP_PASSPHRASE; }
pg_dump -Fc --no-owner | enc > "$out.partial/db.dump.enc"
files=()
for d in exports feeds; do [ -d "$DATA/$d" ] && files+=("$d"); done
if [ ${#files[@]} -gt 0 ]; then tar -C "$DATA" -cf - "${files[@]}" | enc > "$out.partial/files.tar.enc"; fi
(cd "$out.partial" && sha256sum ./* > MANIFEST.sha256)
mv "$out.partial" "$out"
echo "$ts" > "$DIR/LAST_OK"
# retention: delete complete backups older than KEEP days, never the newest one
find "$DIR" -maxdepth 1 -mindepth 1 -type d -name '20*Z' -mtime +"$KEEP" ! -name "$ts" -exec rm -rf {} +
find "$DIR" -maxdepth 1 -type d -name '*.partial' -mmin +120 -exec rm -rf {} +
echo "backup $ts ok: $(du -sh "$out" | cut -f1)"
