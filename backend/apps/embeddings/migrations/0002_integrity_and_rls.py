from django.db import migrations

from apps.common.db import org_rls_reverse_sql, org_rls_sql

TABLE = "embeddings_chunkembedding"

# The untyped `vector` column cannot itself pin a dimension per profile, so a trigger does (TZ 66).
DIMENSION_GUARD = """
CREATE OR REPLACE FUNCTION chunkembedding_dimension_guard() RETURNS trigger
LANGUAGE plpgsql AS
$$
DECLARE expected int;
BEGIN
    SELECT dimension INTO expected FROM embeddings_embeddingprofile WHERE id = NEW.embedding_profile_id;
    IF expected IS NULL THEN
        RAISE EXCEPTION 'Unknown embedding profile %', NEW.embedding_profile_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF vector_dims(NEW.embedding) <> expected THEN
        RAISE EXCEPTION 'Embedding has % dimensions but profile expects %', vector_dims(NEW.embedding), expected
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER chunkembedding_dimension BEFORE INSERT OR UPDATE OF embedding, embedding_profile_id
    ON embeddings_chunkembedding FOR EACH ROW EXECUTE FUNCTION chunkembedding_dimension_guard();
"""

# An embedding must carry the same organization as its chunk. Otherwise a vector of a private
# org chunk could be stored as "official" and become visible to every tenant. RESTRICTIVE policies
# are ANDed with the permissive ones above, and the EXISTS is itself filtered by the chunk's RLS.
# NOTE: the outer columns MUST be table-qualified; an unqualified `organization_id` would bind to the
# subquery's own `c.organization_id` and turn the check into a tautology (caught by RowLevelSecurityTests).
ORG_MATCHES_CHUNK = """
CREATE POLICY chunkembedding_org_matches_chunk_ins ON embeddings_chunkembedding AS RESTRICTIVE
    FOR INSERT TO yurist_api, yurist_parser
    WITH CHECK (EXISTS (SELECT 1 FROM search_documentchunk c
                        WHERE c.id = embeddings_chunkembedding.chunk_id
                          AND c.organization_id IS NOT DISTINCT FROM embeddings_chunkembedding.organization_id));
CREATE POLICY chunkembedding_org_matches_chunk_upd ON embeddings_chunkembedding AS RESTRICTIVE
    FOR UPDATE TO yurist_api, yurist_parser
    WITH CHECK (EXISTS (SELECT 1 FROM search_documentchunk c
                        WHERE c.id = embeddings_chunkembedding.chunk_id
                          AND c.organization_id IS NOT DISTINCT FROM embeddings_chunkembedding.organization_id));
"""
ORG_MATCHES_CHUNK_REVERSE = """
DROP POLICY IF EXISTS chunkembedding_org_matches_chunk_ins ON embeddings_chunkembedding;
DROP POLICY IF EXISTS chunkembedding_org_matches_chunk_upd ON embeddings_chunkembedding;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0001_extensions_and_functions"),
        ("search", "0002_row_level_security"),
        ("embeddings", "0001_initial"),
    ]
    operations = [
        migrations.RunSQL(
            DIMENSION_GUARD,
            "DROP TRIGGER IF EXISTS chunkembedding_dimension ON embeddings_chunkembedding;"
            "DROP FUNCTION IF EXISTS chunkembedding_dimension_guard();",
        ),
        migrations.RunSQL(org_rls_sql(TABLE), org_rls_reverse_sql(TABLE)),
        migrations.RunSQL(ORG_MATCHES_CHUNK, ORG_MATCHES_CHUNK_REVERSE),
    ]
