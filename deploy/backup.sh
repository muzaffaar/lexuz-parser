#!/usr/bin/env bash
# Database dump (custom format) into ./backups, keeping the newest $KEEP (default 14).   ./deploy/backup.sh
# The raw snapshots (s3data volume) and the crawler archive (crawl_data volume) are not in the dump: see DEPLOY.md.
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP="${KEEP:-14}"
mkdir -p backups
out="backups/yurist-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db sh -c 'pg_dump -U postgres -Fc "$POSTGRES_DB"' > "$out.part"
mv "$out.part" "$out"
ls -1t backups/yurist-*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "Wrote $out ($(du -h "$out" | cut -f1))"
