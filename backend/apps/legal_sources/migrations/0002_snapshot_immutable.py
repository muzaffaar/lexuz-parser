from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0001_extensions_and_functions"),
        ("legal_sources", "0001_initial"),
    ]
    operations = [
        migrations.RunSQL(
            """
            CREATE TRIGGER snapshot_immutable BEFORE UPDATE ON legal_sources_sourcesnapshot
                FOR EACH ROW EXECUTE FUNCTION forbid_update();
            CREATE TRIGGER snapshot_no_delete BEFORE DELETE ON legal_sources_sourcesnapshot
                FOR EACH ROW EXECUTE FUNCTION forbid_hard_delete();
            """,
            """
            DROP TRIGGER IF EXISTS snapshot_immutable ON legal_sources_sourcesnapshot;
            DROP TRIGGER IF EXISTS snapshot_no_delete ON legal_sources_sourcesnapshot;
            """,
        )
    ]
