from django.contrib.postgres.operations import BtreeGistExtension, CreateExtension, TrigramExtension, UnaccentExtension
from django.db import migrations
from pgvector.django import VectorExtension

APP_CURRENT_ORG = """
CREATE OR REPLACE FUNCTION app_current_org() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.organization_id', true), '')::uuid $$;
"""

# Official law is append-only (TZ 47, 146): rows may be soft-deleted or superseded, never removed.
# Maintenance that truly needs a delete must opt in via apps.common.db.allow_hard_delete().
FORBID_DELETE = """
CREATE OR REPLACE FUNCTION forbid_hard_delete() RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
    IF coalesce(current_setting('app.allow_hard_delete', true), '') = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Hard delete of % is forbidden (use soft delete / supersede)', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$$;
"""

FORBID_UPDATE = """
CREATE OR REPLACE FUNCTION forbid_update() RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END
$$;
"""


# Normally created by deploy/db/init/00-roles.sql. Created here too when the migrating user is allowed
# to, so a fresh dev database works; otherwise fail early with a clear instruction.
ENSURE_ROLES = """
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['yurist_api', 'yurist_parser'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            BEGIN
                EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS', r);
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE EXCEPTION 'Role % is missing and this user cannot create it. Run deploy/db/init/00-roles.sql as an admin.', r;
            END;
        END IF;
    END LOOP;
END
$$;
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        VectorExtension(),
        TrigramExtension(),
        UnaccentExtension(),
        BtreeGistExtension(),
        CreateExtension("ltree"),
        migrations.RunSQL(ENSURE_ROLES, migrations.RunSQL.noop),
        migrations.RunSQL(APP_CURRENT_ORG, "DROP FUNCTION IF EXISTS app_current_org()"),
        migrations.RunSQL(FORBID_DELETE, "DROP FUNCTION IF EXISTS forbid_hard_delete()"),
        migrations.RunSQL(FORBID_UPDATE, "DROP FUNCTION IF EXISTS forbid_update()"),
    ]
