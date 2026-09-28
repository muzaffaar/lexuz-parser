from django.db import migrations

# Changing a profile's dimension/metric after vectors exist would leave a table of mixed dimensions and
# break the per-profile index cast. A different model = a NEW profile (TZ 66, 68).
GUARD = """
CREATE OR REPLACE FUNCTION embeddingprofile_guard() RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
    IF (NEW.dimension IS DISTINCT FROM OLD.dimension OR NEW.distance_metric IS DISTINCT FROM OLD.distance_metric)
       AND EXISTS (SELECT 1 FROM embeddings_chunkembedding e WHERE e.embedding_profile_id = OLD.id) THEN
        RAISE EXCEPTION 'Embedding profile % already has vectors; create a new profile instead', OLD.name
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER embeddingprofile_guard BEFORE UPDATE ON embeddings_embeddingprofile
    FOR EACH ROW EXECUTE FUNCTION embeddingprofile_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("embeddings", "0002_integrity_and_rls")]
    operations = [
        migrations.RunSQL(
            GUARD,
            "DROP TRIGGER IF EXISTS embeddingprofile_guard ON embeddings_embeddingprofile;"
            "DROP FUNCTION IF EXISTS embeddingprofile_guard();",
        )
    ]
