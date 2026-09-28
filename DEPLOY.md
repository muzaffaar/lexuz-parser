# Deploying to a server

One `docker-compose.yml` at the repo root runs the whole platform on a single Linux host.

```
                       Internet
                          |
        +-----------------+------------------+
        | public network  |  edge network    |
        |   crawler       |  caddy (optional TLS) -> admin
        +-----------------+------------------+
                          |  crawl_data volume (the archive)
        +-----------------+-------------------------------------+
        | private network (no route to the Internet)            |
        |   db (PostgreSQL 17 + pgvector)   s3 (raw snapshots)  |
        |   migrate (one-shot)   parser (ingest jobs)   admin   |
        +--------------------------------------------------------+
```

* The **crawler** is the only container that reaches lex.uz and never touches PostgreSQL. It writes the archive to the `crawl_data` volume.
* **Ingestion** (`parser`) reads that archive and writes PostgreSQL and the S3 store. It connects as the least-privilege role `svc_parser`.
* **Migrations** and the read-only **admin** connect as `yurist_owner`. Nothing connects as the PostgreSQL superuser after first start.
* PostgreSQL and the S3 store publish **no ports**. The admin listens on **127.0.0.1** only.

## Requirements

A Linux server with Docker Engine and the Compose plugin (`docker compose version`), git, and outbound HTTPS from the host.
Disk: the archive measured about 2.3 MB per document so far (3.4 GB for ~1,500 documents), so the whole site (165,000 documents)
is an estimated 350 GB before the database and snapshots: size the disk for that and watch `df`.

## First deployment

```bash
git clone https://github.com/muzaffaar/lexuz-parser.git /opt/yurist
cd /opt/yurist
./deploy/bootstrap.sh                       # or:  ./deploy/bootstrap.sh --domain admin.example.com
```

`bootstrap.sh` generates `.env` with random secrets (mode 600), builds the images, starts the database, the object store and the
migrations, waits for the admin to be healthy and creates an `admin` user. **The admin password is printed once.** It is safe to re-run.

* Without `--domain` the admin is at `http://127.0.0.1:8000/admin/` on the server. From your laptop:
  `ssh -L 8000:127.0.0.1:8000 you@server`, then open `http://127.0.0.1:8000/admin/`. Set `ADMIN_PORT=8010 ./deploy/bootstrap.sh` if 8000 is taken.
* With `--domain` (its DNS record must point at the server, ports 80 and 443 open) Caddy obtains a certificate and serves
  `https://<domain>/admin/`.

Then start the work:

```bash
./deploy/ctl.sh crawl        # start the crawler in the background (resumable)
./deploy/ctl.sh logs crawler # watch it
./deploy/ctl.sh ingest       # load whatever has been crawled so far (run repeatedly)
./deploy/ctl.sh compare      # coverage against lex.uz's own statistics
crontab -e                   # paste the lines from deploy/cron.example
```

## After every `git pull`

```bash
cd /opt/yurist && ./deploy/update.sh
```

That is: `git pull --ff-only`, rebuild the images, apply migrations, restart what changed. The crawler container is recreated too if it is
running; it resumes where it stopped. If a release changes how documents are chunked, also run `./deploy/ctl.sh rechunk`.

## Commands

| Command | What it does |
|---|---|
| `./deploy/ctl.sh crawl` / `crawl-stop` | start / pause the crawler |
| `./deploy/ctl.sh refresh` | recheck saved documents (stop the crawler first) |
| `./deploy/ctl.sh seed <lex.uz urls>` | queue specific documents first |
| `./deploy/ctl.sh stats` | fetch lex.uz's statistics page (one request) |
| `./deploy/ctl.sh ingest [--limit N]` | crawl archive -> PostgreSQL + S3 (unchanged documents are skipped) |
| `./deploy/ctl.sh compare` | lex.uz statistics vs. our database |
| `./deploy/ctl.sh rechunk` | rebuild stale search chunks |
| `./deploy/ctl.sh manage <cmd>` | any `manage.py` command as `svc_parser` |
| `./deploy/ctl.sh createsuperuser` | another admin account |
| `./deploy/ctl.sh psql` | SQL shell (superuser, inside the db container) |
| `./deploy/ctl.sh backup` | database dump into `./backups` (newest 14 kept) |
| `./deploy/ctl.sh status` / `ps` / `logs [svc]` / `up` / `down` | stack state (`down` keeps all data) |

## Moving an existing crawl to the server

Copy your local `data/` folder into the `crawl_data` volume once (archives written on Windows work: their backslash paths are handled):

```bash
docker compose up --no-start crawler        # creates the volume
docker run --rm --user root -v yurist_crawl_data:/data -v /path/to/data:/src:ro alpine \
  sh -c 'cp -a /src/. /data/ && chown -R 10001:10001 /data'
./deploy/ctl.sh crawl && ./deploy/ctl.sh ingest
```

## Backups and restore

`./deploy/backup.sh` dumps PostgreSQL only. Also back up the two volumes that hold everything else (the raw snapshots in `yurist_s3data`
and the archive in `yurist_crawl_data`) with your usual volume/snapshot tooling. The crawl can be repeated, the database can be rebuilt
from archive + snapshots, but that takes days: keep real backups.

Restore a dump into an **empty** deployment (fresh volume: the init script has already created the roles and extensions):

```bash
docker compose up -d db
docker compose exec -T db sh -c 'pg_restore -U postgres -d "$POSTGRES_DB" --no-owner --role=yurist_owner' < backups/yurist-XXXX.dump
docker compose up -d
```

Five `must be owner of extension ...` messages are expected and harmless (extension comments).

## Using AWS S3 (or another S3 API) instead of the bundled store

In `.env` set `S3_ENDPOINT_URL` (empty for AWS), `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`; create the bucket yourself
(enable versioning); then remove the `s3` service from `docker-compose.yml`. Raw snapshots are content-addressed and written with
`If-None-Match: *`, so keys are never overwritten.

## Secrets and passwords

* Everything secret lives in `.env` (git-ignored, mode 600). Never commit it.
* Database passwords are applied only when the data volume is first created. To rotate one later, change it inside PostgreSQL *and* in `.env`:
  `./deploy/ctl.sh psql` then `ALTER ROLE svc_parser PASSWORD '...';`, edit `PARSER_DB_PASSWORD`, `docker compose up -d`.
* Pin the PostgreSQL image by digest for production (TZ section 8): replace `pgvector/pgvector:pg17` in `docker-compose.yml` with
  `pgvector/pgvector@sha256:...` from `docker buildx imagetools inspect pgvector/pgvector:pg17`.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `bootstrap.sh` stops at "Admin did not become healthy" | `docker compose logs migrate admin db` |
| Ingest reports `EndpointConnectionError` | the `s3` container is down or `S3_ENDPOINT_URL` in `.env` is wrong |
| Ingest reports `Not a crawler archive` | the `crawl_data` volume is empty: start the crawler or copy an archive in |
| The crawl stops with "Blocked" | lex.uz answered 401/403/challenge; see `./deploy/ctl.sh logs crawler`, then `retry --status blocked` only once resolved |
| `permission denied` on the archive | volume ownership must be uid 10001 (`chown -R 10001:10001` as in the copy command above) |

## What this is not

A single-node deployment: no replication or failover, and the bundled SeaweedFS runs in its all-in-one `mini` mode (fine for one
server, use a managed S3 for anything more). There is no API service yet (the API layer is a later milestone); the admin is an internal,
read-only data browser and must not be exposed without TLS and a strong password.
