#!/usr/bin/env bash
# Day-to-day operations.   ./deploy/ctl.sh <command>     (./deploy/ctl.sh help)
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash on Windows must not rewrite /data

# no TTY (cron) => `docker compose run` needs -T
if [ -t 0 ]; then RUN=(docker compose run --rm); else RUN=(docker compose run --rm -T); fi

cmd="${1:-help}"; shift || true
case "$cmd" in
  up)              docker compose up -d --remove-orphans ;;
  down)            docker compose down ;;                    # keeps every volume (database, archive, snapshots)
  ps)              docker compose ps ;;
  status)          docker compose ps; echo; "${RUN[@]}" crawler status | head -40 ;;
  logs)            docker compose logs -f --tail=200 "$@" ;;

  crawl)           docker compose up -d crawler ;;           # resumable; `logs crawler` to watch, `crawl-stop` to pause
  crawl-stop)      docker compose stop crawler ;;
  refresh)         "${RUN[@]}" crawler refresh ;;            # recheck saved documents (run while the crawler is stopped)
  seed)            "${RUN[@]}" crawler seed "$@" ;;          # e.g. seed https://lex.uz/docs/5875370
  stats)           "${RUN[@]}" crawler stats ;;              # fetch lex.uz's own statistics page (one request)

  ingest)          "${RUN[@]}" parser ingest_crawl /data "$@" ;;
  compare)         "${RUN[@]}" parser compare_stats /data ;;
  rechunk)         "${RUN[@]}" parser rechunk "$@" ;;
  manage)          "${RUN[@]}" parser "$@" ;;                # any manage.py command as the svc_parser role
  createsuperuser) docker compose exec admin python manage.py createsuperuser ;;
  psql)            docker compose exec db sh -c 'psql -U postgres -d "$POSTGRES_DB"' ;;
  backup)          ./deploy/backup.sh ;;
  daily)           "$0" stats; "$0" ingest; "$0" compare ;;  # what cron runs (see deploy/cron.example)

  *)
    cat <<'TXT'
  up | down | ps | status | logs [service]   the stack
  crawl | crawl-stop                          start / pause the crawler
  refresh | seed <urls> | stats               crawler helpers
  ingest [--limit N]                          load the crawl into PostgreSQL
  compare                                     lex.uz statistics vs. our database
  rechunk                                     rebuild stale search chunks
  manage <cmd ...>                            any manage.py command (svc_parser role)
  createsuperuser | psql | backup             admin account, SQL shell, database dump
  daily                                       stats + ingest + compare
TXT
    ;;
esac
