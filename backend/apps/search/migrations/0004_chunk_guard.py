from django.db import migrations

# Chunk content is replace-only: text/search_text/hash/links never change in place. An embedding is derived
# from that content, so an in-place edit would leave a stale vector (re-chunking deletes + inserts instead).
# On INSERT the chunk's document must be the document its version belongs to.
GUARD = """
CREATE OR REPLACE FUNCTION chunk_guard() RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.version_id IS NOT NULL AND NEW.document_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM legal_documents_legaldocumentversion v WHERE v.id = NEW.version_id AND v.document_id = NEW.document_id
        ) THEN
            RAISE EXCEPTION 'Chunk document % does not own version %', NEW.document_id, NEW.version_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.text IS DISTINCT FROM OLD.text OR NEW.search_text IS DISTINCT FROM OLD.search_text
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash OR NEW.version_id IS DISTINCT FROM OLD.version_id
       OR NEW.document_id IS DISTINCT FROM OLD.document_id OR NEW.section_id IS DISTINCT FROM OLD.section_id
       OR NEW.source_type IS DISTINCT FROM OLD.source_type OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.language IS DISTINCT FROM OLD.language OR NEW.chunk_index IS DISTINCT FROM OLD.chunk_index
    THEN
        RAISE EXCEPTION 'Chunk content is immutable (re-chunk instead)' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER chunk_guard BEFORE INSERT OR UPDATE ON search_documentchunk
    FOR EACH ROW EXECUTE FUNCTION chunk_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("search", "0003_official_fts_function"), ("legal_documents", "0002_integrity_triggers")]
    operations = [
        migrations.RunSQL(GUARD, "DROP TRIGGER IF EXISTS chunk_guard ON search_documentchunk; DROP FUNCTION IF EXISTS chunk_guard();")
    ]
