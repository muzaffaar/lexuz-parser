-- Cluster-level roles (TZ 140: Django must not connect as superuser; least privilege).
--
--   yurist_api     Django API / Celery workers acting for an organization.
--                  Row Level Security is enforced; official law is read-only.
--   yurist_parser  Parser + ingestion. Writes the official (organization-less)
--                  legal corpus; can never see or write organization data.
--
-- These are NOLOGIN group roles. Ops creates real login roles and grants
-- membership, e.g.:  CREATE ROLE svc_api LOGIN PASSWORD '...' IN ROLE yurist_api;
-- Table privileges and RLS policies are applied by `manage.py migrate` (see
-- apps/common/db.py), because the table list is owned by the Django models.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yurist_api') THEN
        CREATE ROLE yurist_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yurist_parser') THEN
        CREATE ROLE yurist_parser NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    -- Owner of the SECURITY DEFINER official-search function. It needs BYPASSRLS so the GIN full-text index
    -- can be used, and is safe because that function can only ever read official rows. Nobody logs in as it.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yurist_search') THEN
        CREATE ROLE yurist_search NOLOGIN NOSUPERUSER BYPASSRLS;
    END IF;
END
$$;
