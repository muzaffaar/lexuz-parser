#!/usr/bin/env bash
# Deploy the latest code: run on the server after every push.   ./deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
docker compose --profile crawl --profile jobs build --pull
docker compose run --rm migrate          # apply new migrations first (idempotent)
docker compose up -d --remove-orphans    # recreate what changed (admin; the crawler too if it is running)
docker compose ps
echo
echo "Updated to $(git rev-parse --short HEAD)."
echo "If the release notes say the chunker changed:  ./deploy/ctl.sh rechunk"
