"""Cross-document bookkeeping done once per ingestion run."""
import uuid

from django.db import connection

from apps.common.ids import uuid7

from ..constants import RelationType
from ..models import LegalDocument, LegalDocumentRelation


def resolve_relation_targets(source) -> int:
    """Point relations at documents that were ingested after the link was first seen."""
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE legal_documents_legaldocumentrelation r
               SET target_document_id = d.id
              FROM legal_documents_legaldocument d, legal_documents_legaldocument s
             WHERE r.target_document_id IS NULL
               AND r.target_external_id = d.external_id
               AND d.source_id = %s
               AND s.id = r.source_document_id AND s.source_id = %s
            """,
            [source.id, source.id],
        )
        return cur.rowcount


def link_language_groups(source) -> int:
    """Give every set of language/script variants of one act a shared group_id.

    Variants are the connected components of the LANGUAGE_VARIANT relations (the site's own language
    switcher). Existing group ids are kept (the smallest wins on a merge) so ids stay stable.
    """
    edges = LegalDocumentRelation.objects.filter(
        source_document__source=source, relation_type=RelationType.LANGUAGE_VARIANT, target_document__isnull=False
    ).values_list("source_document_id", "target_document_id")

    parent: dict[uuid.UUID, uuid.UUID] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    if not parent:
        return 0

    current = dict(LegalDocument.objects.filter(id__in=parent.keys()).values_list("id", "group_id"))
    components: dict[uuid.UUID, list[uuid.UUID]] = {}
    for node in parent:
        components.setdefault(find(node), []).append(node)

    changed = 0
    for members in components.values():
        existing = sorted(str(current[m]) for m in members if current.get(m))
        group = uuid.UUID(existing[0]) if existing else uuid7()
        to_update = [m for m in members if current.get(m) != group]
        if to_update:
            LegalDocument.objects.filter(id__in=to_update).update(group_id=group)
            changed += len(to_update)
    return changed
