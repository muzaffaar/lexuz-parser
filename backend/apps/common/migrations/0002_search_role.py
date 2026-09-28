from django.db import migrations

# yurist_search owns the SECURITY DEFINER search functions. BYPASSRLS lets them use the GIN full-text
# index (the `@@` operator is not leakproof, so under RLS the planner falls back to a sequential scan).
# It is safe because those functions can only ever read official, organization-less rows.
ENSURE_SEARCH_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yurist_search') THEN
        BEGIN
            CREATE ROLE yurist_search NOLOGIN NOSUPERUSER BYPASSRLS;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE EXCEPTION 'Role yurist_search is missing and this user cannot create a BYPASSRLS role. Run deploy/db/init/00-roles.sql as a superuser.';
        END;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("common", "0001_extensions_and_functions")]
    operations = [migrations.RunSQL(ENSURE_SEARCH_ROLE, migrations.RunSQL.noop)]
