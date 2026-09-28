from django.db import migrations

# Official-corpus full-text search. SECURITY DEFINER + a BYPASSRLS owner so the GIN index is usable, and
# the function itself hard-codes `organization_id IS NULL`: it can never return a tenant's private row,
# whatever the caller passes. Validity (as-of edition window) is filtered BEFORE the candidate cap so the
# cap cannot be filled with editions that are about to be discarded.
FUNCTION = """
CREATE OR REPLACE FUNCTION search_official_fts(
    p_queries text[], p_as_of date, p_language text, p_limit integer, p_max_candidates integer
) RETURNS TABLE (
    chunk_id uuid, version_id uuid, document_id uuid, external_id text, title text, document_number text,
    status text, legal_rank smallint, text text, rank real
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS
$$
    WITH q AS (
        SELECT (SELECT string_agg('(' || plainto_tsquery('simple'::regconfig, v)::text || ')', ' | ')
                  FROM unnest(p_queries) AS v)::tsquery AS tsq
    ), hits AS (
        SELECT c.id AS cid, c.version_id AS vid, c.document_id AS did, c.text AS ctext, c.search_tsv AS tsv
          FROM search_documentchunk c
          JOIN legal_documents_legaldocumentversion v ON v.id = c.version_id
          JOIN legal_documents_legaldocument d ON d.id = c.document_id
         WHERE c.organization_id IS NULL
           AND c.source_type IN ('official_law', 'official_guidance')
           AND c.search_tsv @@ (SELECT tsq FROM q)
           AND v.valid_period @> p_as_of
           AND d.deleted_at IS NULL
           AND (p_language IS NULL OR c.language = p_language)
         LIMIT p_max_candidates
    )
    SELECT h.cid, h.vid, h.did, d.external_id::text, d.title, d.document_number::text, d.status::text,
           d.legal_rank::smallint, h.ctext, ts_rank_cd(h.tsv, (SELECT tsq FROM q)) AS rank
      FROM hits h
      JOIN legal_documents_legaldocument d ON d.id = h.did
     ORDER BY rank DESC, h.cid
     LIMIT p_limit
$$;
ALTER FUNCTION search_official_fts(text[], date, text, integer, integer) OWNER TO yurist_search;
REVOKE ALL ON FUNCTION search_official_fts(text[], date, text, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION search_official_fts(text[], date, text, integer, integer) TO yurist_api;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0002_search_role"),
        ("search", "0002_row_level_security"),
        ("legal_documents", "0002_integrity_triggers"),
    ]
    operations = [migrations.RunSQL(FUNCTION, "DROP FUNCTION IF EXISTS search_official_fts(text[], date, text, integer, integer)")]
