#!/usr/bin/env bash
# First-time setup on a fresh server. Safe to re-run (keeps the existing .env and all data).
#   ./deploy/bootstrap.sh [--domain example.com] [--admin-user admin]
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN=""; ADMIN_USER="admin"
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null || { echo "Docker is not installed (https://docs.docker.com/engine/install/)." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (the 'docker compose' plugin)." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Cannot talk to the Docker daemon (is it running? are you in the docker group?)." >&2; exit 1; }

rand() { # $1 = number of hex characters
  if command -v openssl >/dev/null; then openssl rand -hex "$(( $1 / 2 ))"; else head -c 256 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c "$1"; fi
}

if [ ! -f .env ]; then
  echo "==> Creating .env with freshly generated secrets"
  cp deploy/env.example .env
  for name in SECRET POSTGRES OWNER PARSER API; do
    sed -i "s/CHANGE_ME_${name}\$/$(rand 64)/" .env
  done
  sed -i "s/CHANGE_ME_S3_ACCESS\$/$(rand 20)/; s/CHANGE_ME_S3_SECRET\$/$(rand 40)/" .env
  chmod 600 .env
  if [ -n "${ADMIN_PORT:-}" ]; then sed -i "s|^ADMIN_PORT=.*|ADMIN_PORT=${ADMIN_PORT}|" .env; fi  # e.g. ADMIN_PORT=8010 ./deploy/bootstrap.sh
  if [ -n "$DOMAIN" ]; then
    sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|; s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=${DOMAIN},localhost,127.0.0.1|; s|^DJANGO_HTTPS=.*|DJANGO_HTTPS=1|; s|^DJANGO_CSRF_TRUSTED_ORIGINS=.*|DJANGO_CSRF_TRUSTED_ORIGINS=https://${DOMAIN}|; s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=proxy|" .env
  fi
else
  echo "==> Keeping the existing .env"
fi
if grep -qE 'CHANGE_ME_[A-Z]' .env; then echo "The .env still contains CHANGE_ME placeholders; fix them and re-run." >&2; exit 1; fi

echo "==> Building images"
docker compose --profile crawl --profile jobs build

echo "==> Starting PostgreSQL, object storage, migrations and the admin"
docker compose up -d --remove-orphans

echo "==> Waiting for the admin to become healthy"
state=""
for _ in $(seq 1 90); do
  state="$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q admin)" 2>/dev/null || true)"
  [ "$state" = "healthy" ] && break
  sleep 2
done
if [ "$state" != "healthy" ]; then echo "Admin did not become healthy. See: docker compose logs migrate admin db" >&2; exit 1; fi

PASSWORD=""
CHECK="import sys; from django.contrib.auth import get_user_model as g; sys.exit(0 if g().objects.filter(username='${ADMIN_USER}').exists() else 1)"
if docker compose exec -T admin python manage.py shell -c "$CHECK" >/dev/null 2>&1; then
  echo "==> Admin user '${ADMIN_USER}' already exists"
else
  PASSWORD="$(rand 24)"
  docker compose exec -T -e DJANGO_SUPERUSER_PASSWORD="$PASSWORD" admin \
    python manage.py createsuperuser --noinput --username "$ADMIN_USER" --email "${ADMIN_USER}@localhost" >/dev/null
  echo "==> Created admin user '${ADMIN_USER}'"
fi

PORT="$(grep -E '^ADMIN_PORT=' .env | cut -d= -f2)"; PORT="${PORT:-8000}"
HOST="$(grep -E '^DOMAIN=' .env | cut -d= -f2)"
echo
echo "Yurist is up."
if [ -n "$HOST" ]; then
  echo "  Admin:  https://${HOST}/admin/"
else
  echo "  Admin:  http://127.0.0.1:${PORT}/admin/   (loopback only; from your laptop: ssh -L ${PORT}:127.0.0.1:${PORT} <server>)"
fi
if [ -n "$PASSWORD" ]; then echo "  Login:  ${ADMIN_USER} / ${PASSWORD}     <- shown once, store it now"; fi
echo
echo "Next:"
echo "  ./deploy/ctl.sh crawl        # start the crawler (resumable, runs in the background)"
echo "  ./deploy/ctl.sh ingest       # load what has been crawled into PostgreSQL (repeat any time)"
echo "  ./deploy/ctl.sh compare      # how much of lex.uz we hold"
echo "  cat deploy/cron.example      # schedule the recurring jobs"
