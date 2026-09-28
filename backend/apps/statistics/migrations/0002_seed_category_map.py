from django.db import migrations

# card "document type" wording (as stored on the legal-analysis card) -> category name on https://lex.uz/uz/statistic.
# The keys are literal on purpose (a migration must not change meaning when the folding code is later improved);
# tests/test_statistics.py proves they still equal category_key(label). Only pairs seen on real cards are seeded;
# lawyers add the rest in the admin.
SEEDS = [
    ("akti prezidenta", "Prezident hujjatlari", "Акты Президента"),
    ("resheniya pravitelstva", "Hukumat qarorlari", "Решения Правительства"),
    ("akti sudov", "Sud hujjatlari", "Акты судов"),
    ("texnicheskiye dokumenti", "Texnik hujjatlar", "Технические документы"),
    ("xalqaro hujjatlar", "Xalqaro hujjatlar", "Халқаро ҳужжатлар"),
]


def seed(apps, schema_editor):
    model = apps.get_model("statistics", "DocumentCategoryMap")
    for key, category, label in SEEDS:
        model.objects.get_or_create(type_key=key, defaults={"category": category, "label": label, "notes": "seeded from real cards"})


class Migration(migrations.Migration):
    dependencies = [("statistics", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
