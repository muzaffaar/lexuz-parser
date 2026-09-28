"""Full-text candidate retrieval with legal-validity semantics (TZ 72-77).

Runs through the SECURITY DEFINER function `search_official_fts` (see search migration 0003): under Row Level
Security the `@@` operator is not leakproof, so a plain query could not use the GIN index (a sequential scan
per query at ~1M chunks). The function hard-codes `organization_id IS NULL`, so it can only ever return the
official corpus. Tenant-private (knowledge base) search is a separate, RLS-filtered path.

Validity is decided by *edition windows*, not by a status flag:
  * as_of defaults to today in Tashkent (UTC+5, the users' calendar day), so an act that lost force drops out
    because its last edition's window was closed at the loss-of-force date, and a not-yet-effective act stays
    out until it starts;
  * an act whose status we could not determine (no card yet) is still returned, with status='unknown'
    surfaced to the caller instead of being silently treated as in force (critical defect 7).

`plainto_tsquery` ANDs the words of a variant; the variants (Cyrillic/Latin) are OR-ed. Ranking runs over at most
`max_candidates` matches so a match-everything query cannot rank the whole table.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import connection

from apps.legal_documents.text.normalize import query_variants

TASHKENT = ZoneInfo("Asia/Tashkent")


def today_tashkent() -> date:
    return datetime.now(TASHKENT).date()


def fts_candidates(
    query: str, *, as_of: date | None = None, language: str | None = None, limit: int = 50, max_candidates: int = 5000
) -> list[dict]:
    variants = [v for v in query_variants(query) if v]
    if not variants:
        return []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT * FROM search_official_fts(%s::text[], %s::date, %s::text, %s::int, %s::int)",
            [variants, as_of or today_tashkent(), language, limit, max_candidates],
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
