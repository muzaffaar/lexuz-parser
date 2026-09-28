from django.db import migrations

TABLE = "organizations_organization"

# A tenant may only see its OWN organization row. Names/slugs/ids of other tenants are confidential
# (TZ 36). The parser role has no grant on this table at all.
FORWARD = [
    f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY',
    f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY',
    f'CREATE POLICY "{TABLE}_api_select" ON "{TABLE}" FOR SELECT TO yurist_api USING (id = (SELECT app_current_org()))',
]
REVERSE = [
    f'DROP POLICY IF EXISTS "{TABLE}_api_select" ON "{TABLE}"',
    f'ALTER TABLE "{TABLE}" NO FORCE ROW LEVEL SECURITY',
    f'ALTER TABLE "{TABLE}" DISABLE ROW LEVEL SECURITY',
]


class Migration(migrations.Migration):
    dependencies = [("common", "0001_extensions_and_functions"), ("organizations", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
