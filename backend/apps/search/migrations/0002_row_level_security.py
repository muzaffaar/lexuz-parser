from django.db import migrations

from apps.common.db import org_rls_reverse_sql, org_rls_sql

TABLE = "search_documentchunk"


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0001_extensions_and_functions"),
        ("search", "0001_initial"),
    ]
    operations = [migrations.RunSQL(org_rls_sql(TABLE), org_rls_reverse_sql(TABLE))]
