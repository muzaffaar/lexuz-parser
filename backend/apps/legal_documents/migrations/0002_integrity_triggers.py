from django.db import migrations

# A version's *content* is frozen forever (TZ 47: "an old edition must never be lost by UPDATE").
# Only its validity window and current flag may move when a newer/older edition arrives.
VERSION_GUARD = """
CREATE OR REPLACE FUNCTION legal_version_guard() RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.document_id IS DISTINCT FROM OLD.document_id
       OR NEW.version_number IS DISTINCT FROM OLD.version_number
       OR NEW.edition_on IS DISTINCT FROM OLD.edition_on
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.raw_snapshot_id IS DISTINCT FROM OLD.raw_snapshot_id
       OR NEW.normalized_text IS DISTINCT FROM OLD.normalized_text
       OR NEW.text_source IS DISTINCT FROM OLD.text_source
       OR NEW.parser_version IS DISTINCT FROM OLD.parser_version
       OR NEW.metadata IS DISTINCT FROM OLD.metadata
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'Content of legal document version % is immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
"""

FORWARD = VERSION_GUARD + """
CREATE TRIGGER version_guard BEFORE UPDATE ON legal_documents_legaldocumentversion
    FOR EACH ROW EXECUTE FUNCTION legal_version_guard();
CREATE TRIGGER section_immutable BEFORE UPDATE ON legal_documents_legaldocumentsection
    FOR EACH ROW EXECUTE FUNCTION forbid_update();

CREATE TRIGGER document_no_delete BEFORE DELETE ON legal_documents_legaldocument
    FOR EACH ROW EXECUTE FUNCTION forbid_hard_delete();
CREATE TRIGGER version_no_delete BEFORE DELETE ON legal_documents_legaldocumentversion
    FOR EACH ROW EXECUTE FUNCTION forbid_hard_delete();
CREATE TRIGGER section_no_delete BEFORE DELETE ON legal_documents_legaldocumentsection
    FOR EACH ROW EXECUTE FUNCTION forbid_hard_delete();
"""

REVERSE = """
DROP TRIGGER IF EXISTS version_guard ON legal_documents_legaldocumentversion;
DROP TRIGGER IF EXISTS section_immutable ON legal_documents_legaldocumentsection;
DROP TRIGGER IF EXISTS document_no_delete ON legal_documents_legaldocument;
DROP TRIGGER IF EXISTS version_no_delete ON legal_documents_legaldocumentversion;
DROP TRIGGER IF EXISTS section_no_delete ON legal_documents_legaldocumentsection;
DROP FUNCTION IF EXISTS legal_version_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0001_extensions_and_functions"),
        ("legal_documents", "0001_initial"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
