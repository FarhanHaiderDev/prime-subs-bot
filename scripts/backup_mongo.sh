#!/usr/bin/env bash
# Timestamped, compressed backup of the bot's MongoDB database (mongodump).
#
#   MONGODB_URI='mongodb+srv://...' ./scripts/backup_mongo.sh
#
# Environment:
#   MONGODB_URI            required. Never printed, and not put on the command line (it would
#                          show up in `ps`): it is handed to mongodump through a private,
#                          short-lived config file.
#   MONGODB_DB             database to dump (default: prime_subs_bot)
#   BACKUP_DIR             where backups go (default: ./backups, created with mode 700)
#   BACKUP_RETENTION_DAYS  optional: delete this script's backups older than N days
#
# Output: $BACKUP_DIR/<db>_<UTC timestamp>.archive.gz (+ .sha256). Requires `mongodump`
# (MongoDB Database Tools). Restore steps: see DEPLOY.md, "Backup and restore".
#
# The archive contains customer ids, payment proofs and the ENCRYPTED credentials. It does
# NOT contain ENCRYPTION_KEY: back that key up separately, or the credentials in a backup
# cannot be read.
set -euo pipefail

: "${MONGODB_URI:?MONGODB_URI is not set}"
DB="${MONGODB_DB:-prime_subs_bot}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-}"

if ! command -v mongodump >/dev/null 2>&1; then
  echo "mongodump not found. Install the MongoDB Database Tools." >&2
  exit 127
fi
if [[ -n "$RETENTION_DAYS" && ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  echo "BACKUP_RETENTION_DAYS must be a whole number of days." >&2
  exit 2
fi

umask 077
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$BACKUP_DIR/${DB}_${stamp}.archive.gz"
partial="$final.partial"
config="$(mktemp)"
trap 'rm -f "$config" "$partial"' EXIT

# YAML single-quoted string: a single quote is written twice.
printf "uri: '%s'\n" "${MONGODB_URI//\'/\'\'}" > "$config"

echo "Backing up database '$DB' ..."
mongodump --config="$config" --nsInclude="${DB}.*" --gzip --archive="$partial"

if [[ ! -s "$partial" ]]; then
  echo "mongodump produced an empty archive; nothing was kept." >&2
  exit 1
fi
mv "$partial" "$final"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$BACKUP_DIR" && sha256sum "$(basename "$final")" > "$(basename "$final").sha256")
fi
echo "Backup written: $final ($(wc -c < "$final" | tr -d ' ') bytes)"

if [[ -n "$RETENTION_DAYS" ]]; then
  find "$BACKUP_DIR" -maxdepth 1 -type f \( -name "${DB}_*.archive.gz" -o -name "${DB}_*.archive.gz.sha256" \) \
    -mtime "+$RETENTION_DAYS" -print -delete | sed 's/^/Pruned old backup: /'
fi
