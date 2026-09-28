#!/bin/bash
# Runs once, on the first start of an empty data volume (docker-entrypoint-initdb.d), after 00-roles.sql.
# Creates the extensions and the LOGIN roles the services use (TZ 140: nothing connects as the superuser):
#   yurist_owner  owns the database; runs migrations and the read-only admin  (member of the group roles)
#   svc_parser    ingestion / rechunk / statistics import                      (yurist_parser)
#   svc_api       the future API layer                                         (yurist_api, Row Level Security enforced)
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v owner_pw="$OWNER_DB_PASSWORD" -v parser_pw="$PARSER_DB_PASSWORD" -v api_pw="$API_DB_PASSWORD" <<'SQL'
-- vector is not a "trusted" extension, so it (and the others) are created here by the superuser once.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE ROLE yurist_owner LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'owner_pw'
    IN ROLE yurist_api, yurist_parser, yurist_search;   -- membership lets migrations ALTER ... OWNER TO yurist_search
CREATE ROLE svc_parser LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'parser_pw' IN ROLE yurist_parser;
CREATE ROLE svc_api LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'api_pw' IN ROLE yurist_api;

SELECT format('ALTER DATABASE %I OWNER TO yurist_owner', current_database()) \gexec
ALTER SCHEMA public OWNER TO yurist_owner;
-- ALTER FUNCTION ... OWNER TO yurist_search (search migration 0003) requires the new owner to hold CREATE on the schema.
GRANT USAGE, CREATE ON SCHEMA public TO yurist_search;
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO yurist_owner, svc_parser, svc_api;
SQL
