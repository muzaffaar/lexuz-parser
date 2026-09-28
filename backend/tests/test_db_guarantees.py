"""Guarantees enforced by PostgreSQL itself (constraints, triggers, RLS, grants). Application code
cannot bypass these, which is the point (TZ 35-36, 47, 135, 146)."""
import random
from contextlib import contextmanager
from datetime import date

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from apps.common.db import allow_hard_delete
from apps.embeddings.models import ChunkEmbedding, EmbeddingProfile
from apps.embeddings.services import ensure_profile_index, index_name, nearest
from apps.legal_documents.models import LegalDocument, LegalDocumentSection, LegalDocumentVersion
from apps.organizations.models import Organization
from apps.search.models import DocumentChunk, SourceType

from .factories import make_source


def make_doc(external_id="1", **kw):
    return LegalDocument.objects.create(
        source=make_source(), external_id=external_id, source_url="https://lex.uz/docs/1", title="t",
        representation="html", text_status="ok", **kw,
    )


def make_version(doc, n=1, valid_from=date(2020, 1, 1), valid_to=None, current=False, text="matn", edition_on=None):
    return LegalDocumentVersion.objects.create(
        document=doc, version_number=n, valid_from=valid_from, valid_to=valid_to, valid_from_source="adopted",
        is_current=current, content_hash=f"{n:064d}", normalized_text=text, text_source="html", parser_version=1,
        edition_on=edition_on,
    )


def make_chunk(version, doc, org=None, source_type=None, index=0, text="matn"):
    return DocumentChunk.objects.create(
        organization=org, source_type=source_type or (SourceType.ORGANIZATION_INTERNAL if org else SourceType.OFFICIAL_LAW),
        document=doc if not org else None, version=version, chunk_index=index, text=text, search_text=text,
        token_count=3, content_hash="0" * 64, chunker_version=1,
    )


@contextmanager
def as_role(role, org=None):
    with connection.cursor() as cur:
        cur.execute(f"SET LOCAL ROLE {role}")
        # always (re)set: the setting is transaction-local, so a previous block's org would leak into this one
        cur.execute("SELECT set_config('app.organization_id', %s, true)", [str(org.id) if org is not None else ""])
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute("RESET ROLE")


def denied():
    """Statement must be rejected by the database. The savepoint keeps the outer transaction usable."""
    class _Ctx:
        def __enter__(self_inner):
            self_inner.atomic = transaction.atomic()
            self_inner.atomic.__enter__()

        def __exit__(self_inner, exc_type, exc, tb):
            self_inner.atomic.__exit__(exc_type, exc, tb)
            if exc_type is None:
                raise AssertionError("statement was NOT rejected by the database")
            return issubclass(exc_type, DatabaseError)
    return _Ctx()


class VersionConstraintTests(TestCase):
    def setUp(self):
        self.doc = make_doc()

    def test_only_one_current_version(self):
        make_version(self.doc, 1, date(2020, 1, 1), date(2020, 12, 31), current=True)
        with denied():
            make_version(self.doc, 2, date(2021, 1, 1), current=True)

    def test_overlapping_windows_rejected(self):
        make_version(self.doc, 1, date(2020, 1, 1), date(2020, 12, 31))
        with denied():  # the constraint is DEFERRED; force the check, and roll the bad row back with it
            make_version(self.doc, 2, date(2020, 6, 1))
            with connection.cursor() as cur:
                cur.execute("SET CONSTRAINTS no_overlapping_versions IMMEDIATE")

    def test_adjacent_windows_are_fine(self):
        make_version(self.doc, 1, date(2020, 1, 1), date(2020, 12, 31))
        make_version(self.doc, 2, date(2021, 1, 1))
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS no_overlapping_versions IMMEDIATE")

    def test_end_before_start_rejected(self):
        with denied():
            make_version(self.doc, 1, date(2020, 5, 1), date(2020, 4, 1))

    def test_duplicate_version_number_rejected(self):
        make_version(self.doc, 1)
        with denied():
            make_version(self.doc, 1, date(2030, 1, 1))

    def test_as_of_lookup_uses_the_range(self):
        v1 = make_version(self.doc, 1, date(2020, 1, 1), date(2020, 12, 31))
        v2 = make_version(self.doc, 2, date(2021, 1, 1), current=True)
        self.assertEqual(self.doc.versions.get(valid_period__contains=date(2020, 6, 1)), v1)
        self.assertEqual(self.doc.versions.get(valid_period__contains=date(2099, 1, 1)), v2)
        self.assertFalse(self.doc.versions.filter(valid_period__contains=date(2019, 1, 1)).exists())


class ImmutabilityTests(TestCase):
    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc)

    def test_version_content_is_frozen(self):
        for field, value in (("normalized_text", "changed"), ("content_hash", "f" * 64), ("edition_on", date(2000, 1, 1))):
            with denied():
                LegalDocumentVersion.objects.filter(pk=self.v.pk).update(**{field: value})

    def test_version_validity_window_may_move(self):
        LegalDocumentVersion.objects.filter(pk=self.v.pk).update(valid_to=date(2030, 1, 1), is_current=True)
        self.v.refresh_from_db()
        self.assertEqual(self.v.valid_to, date(2030, 1, 1))

    def test_sections_are_immutable(self):
        sec = LegalDocumentSection.objects.create(version=self.v, section_type="clause", text="a", order_index=0, path="cl_1", text_hash="0" * 64)
        with denied():
            LegalDocumentSection.objects.filter(pk=sec.pk).update(text="b")

    def test_official_law_cannot_be_hard_deleted(self):
        with denied():
            self.v.delete()
        with denied():
            self.doc.delete()

    def test_explicit_maintenance_escape_hatch(self):
        v2 = make_version(self.doc, 2, date(2025, 1, 1))
        with allow_hard_delete():
            v2.delete()
        self.assertFalse(LegalDocumentVersion.objects.filter(pk=v2.pk).exists())


class ChunkConstraintTests(TestCase):
    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc)
        self.org = Organization.objects.create(name="A", slug="a")

    def test_official_chunk_must_not_have_an_organization(self):
        with denied():
            make_chunk(self.v, self.doc, source_type=SourceType.OFFICIAL_LAW, org=self.org)

    def test_internal_chunk_requires_an_organization(self):
        with denied():
            DocumentChunk.objects.create(
                organization=None, source_type=SourceType.ORGANIZATION_INTERNAL, version=self.v, chunk_index=0,
                text="x", search_text="x", token_count=1, content_hash="0" * 64, chunker_version=1,
            )

    def test_chunk_index_unique_per_version(self):
        make_chunk(self.v, self.doc, index=0)
        with denied():
            make_chunk(self.v, self.doc, index=0)

    def test_tsvector_is_generated(self):
        c = make_chunk(self.v, self.doc, text="ozbekiston respublikasi")
        self.assertTrue(DocumentChunk.objects.filter(pk=c.pk).extra(where=["search_tsv @@ plainto_tsquery('simple', 'respublikasi')"]).exists())


class EmbeddingTests(TestCase):
    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc)
        self.profile = EmbeddingProfile.objects.create(name="p3", model_name="m", model_version="1", dimension=3, is_active=True)

    def test_dimension_must_match_profile(self):
        chunk = make_chunk(self.v, self.doc)
        with denied():
            ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[1, 2, 3, 4])
        ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[1, 2, 3])

    def test_one_embedding_per_chunk_and_profile(self):
        chunk = make_chunk(self.v, self.doc)
        ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[1, 2, 3])
        with denied():
            ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[3, 2, 1])

    def test_only_one_active_profile(self):
        with denied():
            EmbeddingProfile.objects.create(name="p4", model_name="m", model_version="2", dimension=4, is_active=True)
        EmbeddingProfile.objects.create(name="p5", model_name="m", model_version="3", dimension=5, is_active=False)

    def test_two_profiles_with_different_dimensions_coexist(self):
        other = EmbeddingProfile.objects.create(name="p5", model_name="m", model_version="3", dimension=5)
        chunk = make_chunk(self.v, self.doc)
        ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[1, 2, 3])
        ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=other, embedding=[1, 2, 3, 4, 5])

    def test_partial_hnsw_index_is_used_and_correct(self):
        rng = random.Random(7)
        chunks = [make_chunk(self.v, self.doc, index=i, text=f"c{i}") for i in range(60)]
        vectors = {c.id: [rng.random() for _ in range(3)] for c in chunks}
        ChunkEmbedding.objects.bulk_create(
            [ChunkEmbedding(chunk=c, embedding_profile=self.profile, embedding=vectors[c.id]) for c in chunks]
        )
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # CREATE INDEX refuses a table with pending deferred-FK events
        name = ensure_profile_index(self.profile)
        self.assertEqual(name, index_name(self.profile))
        ensure_profile_index(self.profile)  # idempotent
        query = vectors[chunks[5].id]
        with connection.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(
                "EXPLAIN SELECT chunk_id FROM embeddings_chunkembedding WHERE embedding_profile_id = %s "
                "ORDER BY embedding::vector(3) <=> %s::vector(3) LIMIT 3",
                [self.profile.id, "[" + ",".join(map(str, query)) + "]"],
            )
            plan = "\n".join(r[0] for r in cur.fetchall())
        self.assertIn(name, plan)
        top = nearest(self.profile, query, limit=1)
        self.assertEqual(top[0][0], chunks[5].id)
        self.assertAlmostEqual(top[0][1], 0.0, places=5)


class RowLevelSecurityTests(TestCase):
    """Run as the real restricted roles: superusers bypass RLS, so tests must SET ROLE."""

    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc)
        self.org_a = Organization.objects.create(name="A", slug="a")
        self.org_b = Organization.objects.create(name="B", slug="b")
        self.official = make_chunk(self.v, self.doc, index=0, text="official")
        # org rows must be created by someone allowed to write them; superuser bypasses RLS in setUp
        self.chunk_a = make_chunk(self.v, self.doc, org=self.org_a, index=1, text="secret A")
        self.chunk_b = make_chunk(self.v, self.doc, org=self.org_b, index=2, text="secret B")
        self.profile = EmbeddingProfile.objects.create(name="p3", model_name="m", model_version="1", dimension=3)

    def visible(self):
        return set(DocumentChunk.objects.values_list("text", flat=True))

    def test_tenant_sees_official_plus_own_only(self):
        with as_role("yurist_api", self.org_a):
            self.assertEqual(self.visible(), {"official", "secret A"})
        with as_role("yurist_api", self.org_b):
            self.assertEqual(self.visible(), {"official", "secret B"})

    def test_knowing_another_orgs_uuid_reveals_nothing(self):  # TZ 36
        with as_role("yurist_api", self.org_a):
            self.assertFalse(DocumentChunk.objects.filter(pk=self.chunk_b.pk).exists())
            self.assertEqual(DocumentChunk.objects.filter(pk=self.chunk_b.pk).update(text="pwned"), 0)
            self.assertEqual(DocumentChunk.objects.filter(pk=self.chunk_b.pk).delete()[0], 0)

    def test_no_org_context_means_official_only(self):
        with as_role("yurist_api"):
            self.assertEqual(self.visible(), {"official"})

    def test_tenant_cannot_write_official_or_foreign_rows(self):
        with as_role("yurist_api", self.org_a):
            with denied():
                make_chunk(self.v, self.doc, index=10)                          # official (org NULL)
            with denied():
                make_chunk(self.v, self.doc, org=self.org_b, index=11)          # another tenant's
            make_chunk(self.v, self.doc, org=self.org_a, index=12)              # own: fine

    def test_parser_writes_official_but_never_sees_or_writes_org_rows(self):
        with as_role("yurist_parser"):
            self.assertEqual(self.visible(), {"official"})
            make_chunk(self.v, self.doc, index=20)
            with denied():
                make_chunk(self.v, self.doc, org=self.org_a, index=21)

    def test_embedding_must_carry_its_chunks_organization(self):
        with as_role("yurist_api", self.org_a):
            with denied():  # org mismatch: chunk is A's, embedding claims NULL (would leak to everyone)
                ChunkEmbedding.objects.create(chunk=self.chunk_a, embedding_profile=self.profile, embedding=[1, 2, 3], organization=None)
            with denied():  # claims A but the chunk is official
                ChunkEmbedding.objects.create(chunk=self.official, embedding_profile=self.profile, embedding=[1, 2, 3], organization=self.org_a)
            ChunkEmbedding.objects.create(chunk=self.chunk_a, embedding_profile=self.profile, embedding=[1, 2, 3], organization=self.org_a)

    def test_parser_cannot_launder_a_private_chunk_into_shared_embeddings(self):
        with as_role("yurist_parser"):
            with denied():
                ChunkEmbedding.objects.create(chunk=self.chunk_a, embedding_profile=self.profile, embedding=[1, 2, 3], organization=None)
            ChunkEmbedding.objects.create(chunk=self.official, embedding_profile=self.profile, embedding=[1, 2, 3], organization=None)

    def test_vector_search_is_tenant_scoped(self):
        for chunk, org in ((self.official, None), (self.chunk_a, self.org_a), (self.chunk_b, self.org_b)):
            ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[1, 0, 0], organization=org)
        with as_role("yurist_api", self.org_a):
            found = {cid for cid, _ in nearest(self.profile, [1, 0, 0], limit=10)}
        self.assertEqual(found, {self.official.id, self.chunk_a.id})


class RoleGrantTests(TestCase):
    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc)

    def test_api_role_is_read_only_on_legal_data(self):
        with as_role("yurist_api"):
            self.assertEqual(LegalDocument.objects.count(), 1)
            with denied():
                LegalDocument.objects.filter(pk=self.doc.pk).update(title="x")
            with denied():
                make_doc("2")
            with denied():
                self.v.delete()

    def test_parser_role_can_write_but_not_delete_legal_data(self):
        with as_role("yurist_parser"):
            make_doc("3")
            LegalDocument.objects.filter(pk=self.doc.pk).update(title="updated")
            with denied():
                LegalDocumentVersion.objects.filter(pk=self.v.pk).delete()

    def test_tenant_role_cannot_touch_parser_tables(self):
        from apps.parsers.models import ParserItem

        with as_role("yurist_api"):
            self.assertEqual(ParserItem.objects.count(), 0)
            with denied():
                ParserItem.objects.create(job_id=self.doc.id, url="x", status="new")
