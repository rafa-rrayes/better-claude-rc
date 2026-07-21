#!/bin/sh
# Nightly Postgres backup for the bcrc platform. Runs INSIDE the postgres
# container (the image ships pg_dump; peer auth as the postgres user needs
# no password) — wire it up as a Dokploy Schedule, see docs/RUNBOOK.md.
#
#   BACKUP_DIR   where dumps land (default /var/lib/postgresql/data/backups —
#                inside the data volume so dumps survive container recreates)
#   KEEP_DAYS    rotation window in days (default 14)
#   POSTGRES_USER / POSTGRES_DB   standard postgres-image env (already set)
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/lib/postgresql/data/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
DB="${POSTGRES_DB:-bcrc}"
PGUSER="${POSTGRES_USER:-bcrc}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/bcrc-$STAMP.dump"

# -Fc: custom format — compressed, restorable table-by-table via pg_restore.
# Write to .tmp then mv so rotation never deletes a half-written dump's name.
pg_dump -Fc -U "$PGUSER" -d "$DB" -f "$OUT.tmp"
mv "$OUT.tmp" "$OUT"

find "$BACKUP_DIR" -name 'bcrc-*.dump' -type f -mtime +"$KEEP_DAYS" -delete
find "$BACKUP_DIR" -name '*.tmp' -type f -mtime +1 -delete

echo "backup ok: $OUT ($(du -h "$OUT" | cut -f1)); $(ls "$BACKUP_DIR" | wc -l | tr -d ' ') dumps retained"
