"""Database-level security helpers: org context, RLS policy SQL, role grants.

Threat model (TZ 35/36): organization isolation must not depend on a developer
remembering `.filter(organization=...)`. So org-scoped tables get PostgreSQL Row
Level Security, keyed off a transaction-local setting `app.organization_id`.

Two group roles (see deploy/db/init/00-roles.sql):
  yurist_api     RLS enforced. Reads official law, reads/writes only its own org's rows.
  yurist_parser  Writes the official (organization-less) corpus. Cannot touch org rows.
Superusers and BYPASSRLS roles skip RLS entirely, so Django must never connect as one
in production (TZ 140).
"""
import logging
from contextlib import contextmanager

from django.apps import apps
from django.db import connections, transaction

log = logging.getLogger(__name__)

ORG_SETTING = "app.organization_id"
HARD_DELETE_SETTING = "app.allow_hard_delete"


@contextmanager
def organization_context(organization_id, using="default"):
    """Scope the current transaction to an organization for RLS.

    Residual risk (documented, inherent to the GUC pattern): the setting is freely settable by any SQL the
    application role can run, so RLS is only as strong as the application's control over its own SQL.
    Never build SQL from user input; a per-tenant DB role would be the stronger (costlier) alternative.

    Uses set_config(..., is_local=true) so it is transaction-scoped: safe behind
    PgBouncer transaction pooling (TZ 153), where a session-level SET would leak to
    the next client of the same server connection.
    """
    with transaction.atomic(using=using):
        with connections[using].cursor() as cur:
            cur.execute("SELECT current_setting(%s, true)", [ORG_SETTING])
            previous = cur.fetchone()[0] or ""
            cur.execute("SELECT set_config(%s, %s, true)", [ORG_SETTING, str(organization_id) if organization_id else ""])
        try:
            yield
        finally:
            # restore, so leaving a nested context returns to the OUTER organization, not to "none"
            with connections[using].cursor() as cur:
                cur.execute("SELECT set_config(%s, %s, true)", [ORG_SETTING, previous])


@contextmanager
def allow_hard_delete(using="default"):
    """Explicit escape hatch for maintenance. Normal code must soft-delete (TZ 146)."""
    with transaction.atomic(using=using):
        with connections[using].cursor() as cur:
            cur.execute("SELECT set_config(%s, 'on', true)", [HARD_DELETE_SETTING])
        yield


# --------------------------------------------------------------------------- RLS SQL


def org_rls_sql(table: str) -> list[str]:
    """Statements enabling org-scoped RLS on `table` (needs an `organization_id uuid NULL` column).

    NULL organization_id == official/shared data: readable by everyone, writable only by the parser.
    """
    q = f'"{table}"'
    return [
        f"ALTER TABLE {q} ENABLE ROW LEVEL SECURITY",
        # FORCE: the table owner is subject to policies too, so an app that accidentally
        # connects as the owner does not silently bypass isolation.
        f"ALTER TABLE {q} FORCE ROW LEVEL SECURITY",
        f'CREATE POLICY "{table}_api_select" ON {q} FOR SELECT TO yurist_api '
        f"USING (organization_id IS NULL OR organization_id = (SELECT app_current_org()))",
        f'CREATE POLICY "{table}_api_insert" ON {q} FOR INSERT TO yurist_api '
        f"WITH CHECK (organization_id = (SELECT app_current_org()))",
        f'CREATE POLICY "{table}_api_update" ON {q} FOR UPDATE TO yurist_api '
        f"USING (organization_id = (SELECT app_current_org())) WITH CHECK (organization_id = (SELECT app_current_org()))",
        f'CREATE POLICY "{table}_api_delete" ON {q} FOR DELETE TO yurist_api '
        f"USING (organization_id = (SELECT app_current_org()))",
        f'CREATE POLICY "{table}_parser_all" ON {q} FOR ALL TO yurist_parser '
        f"USING (organization_id IS NULL) WITH CHECK (organization_id IS NULL)",
    ]


def org_rls_reverse_sql(table: str) -> list[str]:
    q = f'"{table}"'
    stmts = [
        f'DROP POLICY IF EXISTS "{table}_{suffix}" ON {q}'
        for suffix in ("api_select", "api_insert", "api_update", "api_delete", "parser_all")
    ]
    return [*stmts, f"ALTER TABLE {q} NO FORCE ROW LEVEL SECURITY", f"ALTER TABLE {q} DISABLE ROW LEVEL SECURITY"]


# --------------------------------------------------------------------------- grants

R = ("SELECT",)
W_NO_DELETE = ("SELECT", "INSERT", "UPDATE")
W_ALL = ("SELECT", "INSERT", "UPDATE", "DELETE")

# (app_label, model_name or None for every model in the app) -> privileges
API_GRANTS = [
    ("organizations", None, R),
    ("legal_sources", None, R),
    ("legal_documents", None, R),
    ("legal_monitoring", None, R),
    ("statistics", None, R),
    ("parsers", None, R),
    ("parsers", "parserjob", ("SELECT", "INSERT")),  # manual-run endpoint creates the job row
    ("search", "documentchunk", W_ALL),
    ("embeddings", "embeddingprofile", R),
    ("embeddings", "chunkembedding", W_ALL),
]
PARSER_GRANTS = [
    ("legal_sources", None, W_NO_DELETE),
    ("legal_documents", None, W_NO_DELETE),
    ("legal_monitoring", None, W_NO_DELETE),
    ("statistics", None, W_NO_DELETE),
    ("parsers", None, W_NO_DELETE),
    ("search", "documentchunk", W_ALL),  # DELETE: re-chunking replaces chunks
    ("embeddings", "embeddingprofile", R),
    ("embeddings", "chunkembedding", W_ALL),
]
# Owner of the SECURITY DEFINER official-search functions (BYPASSRLS): read-only on exactly what they join.
SEARCH_GRANTS = [
    ("legal_documents", "legaldocument", R),
    ("legal_documents", "legaldocumentversion", R),
    ("search", "documentchunk", R),
]
_MANAGED_APPS = sorted({spec[0] for spec in API_GRANTS + PARSER_GRANTS + SEARCH_GRANTS})


def _tables(app_label, model_name):
    config = apps.get_app_config(app_label)
    for model in config.get_models():
        if model_name is None or model._meta.model_name == model_name:
            yield model._meta.db_table


def apply_role_grants(using="default"):
    """Idempotently set table privileges for the yurist_* group roles."""
    connection = connections[using]
    if connection.vendor != "postgresql":
        return
    # One transaction: concurrent sessions keep seeing the old privileges until COMMIT, so a deploy never
    # exposes a moment where REVOKE ALL has run and GRANT has not.
    with transaction.atomic(using=using), connection.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('yurist_api', 'yurist_parser', 'yurist_search')")
        existing = {row[0] for row in cur.fetchall()}
        # post_migrate also fires after a rollback / partial migrate: only touch tables that exist right now
        present = set(connection.introspection.table_names(cur))
        managed_tables = sorted({t for label in _MANAGED_APPS for t in _tables(label, None)} & present)
        for role, spec in (("yurist_api", API_GRANTS), ("yurist_parser", PARSER_GRANTS), ("yurist_search", SEARCH_GRANTS)):
            if role not in existing:
                log.warning("Role %s does not exist; skipping grants (run deploy/db/init/00-roles.sql)", role)
                continue
            for table in managed_tables:
                cur.execute(f'REVOKE ALL ON TABLE "{table}" FROM {role}')
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            for app_label, model_name, privileges in spec:
                for table in (t for t in _tables(app_label, model_name) if t in present):
                    cur.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role}')
