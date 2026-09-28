import json
from unittest import TestCase, skipUnless

from apps.legal_documents.constants import SectionType as T
from apps.legal_documents.parsing.structure import build_sections, classify_block
from apps.search.chunking import HeuristicTokenCounter, chunk_sections, split_text

from .factories import DATA_DIR, block, blocks_for

COUNTER = HeuristicTokenCounter()


def sections_of(*pairs):
    return build_sections([block(i, css, text) for i, (css, text) in enumerate(pairs)])[0]


class ClassifyTests(TestCase):
    def test_numbering_levels(self):
        self.assertEqual(classify_block(["ACT_TEXT"], "1. text")[:2], (T.CLAUSE, "1"))
        self.assertEqual(classify_block(["ACT_TEXT"], "1.2. text")[:2], (T.SUBCLAUSE, "1.2"))
        self.assertEqual(classify_block(["ACT_TEXT"], "3.4.5. text")[0], T.SUBCLAUSE)
        self.assertEqual(classify_block(["ACT_TEXT"], "(a) text")[:2], (T.ITEM, "a"))
        self.assertEqual(classify_block(["ACT_TEXT"], "б) matn")[:2], (T.ITEM, "б"))

    def test_a_date_is_not_a_clause(self):
        self.assertEqual(classify_block(["ACT_TEXT"], "17.09.2026 yil qabul qilindi")[0], T.PARAGRAPH)

    def test_keyword_headings(self):
        self.assertEqual(classify_block(["ACT_TEXT"], "25-modda. Mehnat shartnomasi")[:3], (T.ARTICLE, "25", "Mehnat shartnomasi"))
        self.assertEqual(classify_block(["ACT_TEXT"], "1-bob. Umumiy qoidalar")[0], T.CHAPTER)
        self.assertEqual(classify_block(["ACT_TEXT"], "12-модда. Иш ҳақи")[0], T.ARTICLE)
        self.assertEqual(classify_block(["ACT_TEXT"], "Статья 5. Права")[:2], (T.ARTICLE, "5"))
        self.assertEqual(classify_block(["ACT_TEXT"], "Глава 2. Общие")[0], T.CHAPTER)

    def test_long_paragraph_is_not_mistaken_for_a_heading(self):
        text = "1-bob " + "so'z " * 100
        self.assertEqual(classify_block(["ACT_TEXT"], text)[0], T.PARAGRAPH)

    def test_roman_heading_only_with_header_class(self):
        self.assertEqual(classify_block(["TEXT_HEADER_DEFAULT"], "II. Majburiyat")[:2], (T.CHAPTER, "II"))
        self.assertEqual(classify_block(["ACT_TEXT"], "II. Majburiyat")[0], T.PARAGRAPH)

    def test_appendix_number(self):
        self.assertEqual(classify_block(["APPL_BANNER_LANDSCAPE_TITLE"], "qaroriga 2-ILOVA")[:2], (T.APPENDIX, "2"))
        self.assertEqual(classify_block(["APPL_BANNER_LANDSCAPE_TITLE"], "Приложение № 3")[:2], (T.APPENDIX, "3"))


class TreeTests(TestCase):
    def test_hierarchy_and_paths(self):
        secs = sections_of(
            ("TEXT_HEADER_DEFAULT", "I. Umumiy"), ("ACT_TEXT", "1. Birinchi"), ("ACT_TEXT", "1.1. Kichik"),
            ("ACT_TEXT", "(a) band"), ("ACT_TEXT", "2. Ikkinchi"), ("TEXT_HEADER_DEFAULT", "II. Maxsus"), ("ACT_TEXT", "3. Uchinchi"),
        )
        by = {s.text: s for s in secs}
        self.assertEqual(by["1. Birinchi"].parent, by["I. Umumiy"])
        self.assertEqual(by["1.1. Kichik"].parent, by["1. Birinchi"])
        self.assertEqual(by["(a) band"].parent, by["1.1. Kichik"])
        self.assertEqual(by["2. Ikkinchi"].parent, by["I. Umumiy"])
        self.assertEqual(by["3. Uchinchi"].parent, by["II. Maxsus"])
        self.assertEqual(by["(a) band"].path, "ch_i.cl_1.sc_1_1.it_a")

    def test_every_block_becomes_exactly_one_section_in_order(self):
        blocks = blocks_for(["1. a", "text", "(a) x", "", "2. b"])
        secs, ann = build_sections(blocks)
        self.assertEqual(len(secs), 5)
        self.assertEqual([s.order_index for s in secs], list(range(5)))
        self.assertEqual(ann, [])

    def test_duplicate_numbers_get_unique_paths(self):
        secs = sections_of(("ACT_TEXT", "1. a"), ("ACT_TEXT", "1. b"), ("ACT_TEXT", "1. c"))
        self.assertEqual(len({s.path for s in secs}), 3)

    def test_appendix_resets_numbering_scope(self):
        secs = sections_of(("ACT_TEXT", "1. main"), ("APPL_BANNER_LANDSCAPE_TITLE", "1-ILOVA"), ("ACT_TITLE_APPL", "Nizom"), ("ACT_TEXT", "1. in appendix"))
        appendix = next(s for s in secs if s.section_type == T.APPENDIX)
        self.assertIsNone(appendix.parent)
        self.assertEqual(secs[-1].parent, appendix)
        self.assertEqual(secs[2].parent, appendix)  # appendix title stays inside it

    def test_signature_leaves_the_running_text(self):
        secs = sections_of(("TEXT_HEADER_DEFAULT", "I. A"), ("ACT_TEXT", "1. x"), ("SIGNATURE", "Prezident"))
        self.assertIsNone(secs[-1].parent)

    def test_annotations_are_separated(self):
        blocks = [block(0, "ACT_TEXT", "1. x"), {**block(1, "INDEXES_ON_REF", "[ OKOZ: 1. 01.00.00.00 X ]"), "is_content": False}]
        secs, ann = build_sections(blocks)
        self.assertEqual((len(secs), len(ann)), (1, 1))

    def test_table_block(self):
        html = '<div class="TABLE_STD2 lx_elem"><table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table></div>'
        secs, _ = build_sections([block(0, "TABLE_STD2", "", html=html)])
        self.assertEqual(secs[0].section_type, T.TABLE)
        self.assertEqual(secs[0].text, "A | B\n1 | 2")
        self.assertEqual(secs[0].metadata["tables"][0][1][0]["t"], "1")


class ChunkingTests(TestCase):
    def chunk(self, secs, max_tokens=60, min_tokens=20):
        return chunk_sections(secs, max_tokens=max_tokens, min_tokens=min_tokens, counter=COUNTER)

    def test_no_blind_character_cuts_and_full_coverage(self):
        secs = sections_of(*[("ACT_TEXT", f"{i}. " + "Bu band matni. " * 6) for i in range(1, 15)])
        chunks = self.chunk(secs)
        covered = {id(s) for c in chunks for s in c.sections}
        self.assertEqual(covered, {id(s) for s in secs})
        for c in chunks:
            self.assertLessEqual(c.token_count, 60 * 1.15)
            self.assertFalse(c.text.endswith("Bu ban"))  # never cut mid-word

    def test_chunks_do_not_cross_chapter_boundaries(self):
        secs = sections_of(
            ("TEXT_HEADER_DEFAULT", "I. A"), ("ACT_TEXT", "1. x"), ("TEXT_HEADER_DEFAULT", "II. B"), ("ACT_TEXT", "2. y"),
        )
        chunks = self.chunk(secs, max_tokens=500, min_tokens=100)
        for c in chunks:
            self.assertFalse("1. x" in c.text and "2. y" in c.text)

    def test_oversized_leaf_split_on_sentences(self):
        text = " ".join(f"Gap raqami {i} tugadi." for i in range(1, 60))
        parts, forced = split_text(text, 40, COUNTER)
        self.assertFalse(forced)
        self.assertGreater(len(parts), 3)
        self.assertEqual(" ".join(parts), text)
        self.assertTrue(all(p.endswith(".") for p in parts))

    def test_single_giant_sentence_is_flagged_forced(self):
        parts, forced = split_text("so'z " * 400, 40, COUNTER)
        self.assertTrue(forced)
        self.assertGreater(len(parts), 1)

    def test_big_table_splits_by_rows_and_repeats_header(self):
        rows = ["Nomi | Qiymat"] + [f"Qator {i} | {i * 11}" for i in range(80)]
        parts, _ = split_text("\n".join(rows), 60, COUNTER)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(p.startswith("Nomi | Qiymat") for p in parts))
        self.assertEqual(sum(p.count("Qator") for p in parts), 80)

    def test_one_huge_table_row_is_split_inside_the_row_keeping_its_key_cells(self):
        # real case: an act whose table rows contain whole paragraphs (one row far larger than a chunk)
        long_cell = " ".join(f"Bu {i}-jumla mahsulot turkumining tavsifi." for i in range(1, 90))
        text = "\n".join(["T/r | Toifa raqami | Tavsif", "1. | 01.1 | Qisqa qator", f"42. | 04.2.5.2 | {long_cell}", "43. | 04.2.6 | Yana qisqa"])
        parts, forced = split_text(text, 80, COUNTER)
        self.assertFalse(forced)
        for part in parts:
            self.assertLessEqual(COUNTER.count(part), 80 * 1.1, part[:60])
            self.assertTrue(part.startswith("T/r | Toifa raqami | Tavsif"))  # header repeated
        rows_42 = [ln for part in parts for ln in part.split("\n") if ln.startswith("42. | 04.2.5.2 | ")]
        self.assertGreater(len(rows_42), 1)  # the big row became several pieces, each carrying its row number and code
        rebuilt = " ".join(ln.split(" | ", 2)[2] for ln in rows_42)
        self.assertEqual(rebuilt, long_cell)  # nothing lost, nothing reordered
        self.assertTrue(any("Qisqa qator" in p for p in parts) and any("Yana qisqa" in p for p in parts))

    def test_heading_path_attached_to_children_of_oversized_container(self):
        secs = sections_of(
            ("TEXT_HEADER_DEFAULT", "I. Katta bob"),
            *[("ACT_TEXT", f"{i}. " + "matn " * 30) for i in range(1, 8)],
        )
        chunks = self.chunk(secs, max_tokens=80, min_tokens=20)
        self.assertTrue(any(c.heading_path and c.heading_path[0].startswith("I. Katta bob") for c in chunks))

    def test_signature_and_notes_excluded_header_included(self):
        secs = sections_of(("ACT_TITLE", "Sarlavha"), ("ACT_TEXT", "1. Matn " * 10), ("SIGNATURE", "Prezident Sh. M."))
        chunks = self.chunk(secs, max_tokens=500)
        joined = "\n".join(c.text for c in chunks)
        self.assertIn("Sarlavha", joined)
        self.assertNotIn("Prezident Sh. M.", joined)
        self.assertEqual(chunks[0].kind, "header")

    def test_tiny_label_is_glued_to_its_table(self):
        big_table = "\n".join(["A | B"] + [f"r{i} | {i}" for i in range(120)])
        html = "<div class='TABLE_STD2 lx_elem'><table>" + "".join(f"<tr><td>c{i}</td><td>{i}</td></tr>" for i in range(120)) + "</table></div>"
        secs, _ = build_sections([block(0, "ACT_TEXT", "40."), block(1, "TABLE_STD2", "", html=html)])
        chunks = self.chunk(secs, max_tokens=60, min_tokens=20)
        self.assertNotIn("40.", [c.text for c in chunks])
        self.assertTrue(chunks[0].text.startswith("40."))

    def test_budget_is_measured_on_the_joined_text_not_the_sum_of_parts(self):
        # The estimator's chars-per-token ratio depends on the script mix of the text it sees: a Cyrillic paragraph
        # (300) plus a digit table (150) sum to 450, but joined the text is Cyrillic-dominant and costs ~520.
        secs = sections_of(
            ("ACT_TEXT", "1-modda. Sarlavha"),
            ("ACT_TEXT", "абвгд " * 110),
            ("ACT_TEXT", "12345 " * 80),
        )
        self.assertEqual(COUNTER.count("абвгд " * 110) + COUNTER.count("12345 " * 80), 450)
        chunks = self.chunk(secs, max_tokens=450, min_tokens=20)
        self.assertTrue(all(c.token_count <= 450 for c in chunks), [c.token_count for c in chunks])
        self.assertIn("12345", "\n".join(c.text for c in chunks))

    def test_content_hash_covers_headings(self):
        a = self.chunk(sections_of(("TEXT_HEADER_DEFAULT", "I. A"), ("ACT_TEXT", "1. same")), 500)
        self.assertTrue(all(len(c.content_hash) == 64 for c in a))


def _archive_docs():
    root = DATA_DIR / "documents"
    for folder in sorted(root.iterdir()):
        rec = json.loads((folder / "document.json").read_text(encoding="utf-8"))
        if rec["representation"] == "html":
            yield folder.name, rec


@skipUnless((DATA_DIR / "documents").exists(), "crawler archive not present")
class RealArchiveInvariantTests(TestCase):
    def test_structure_and_chunk_invariants_hold_for_every_real_document(self):
        """One pass over the WHOLE live archive (it keeps growing; new documents are exactly where surprises
        come from: e.g. 80k-token tables whose single rows exceed a chunk)."""
        checked = 0
        for name, rec in _archive_docs():
            secs, _ = build_sections(rec["blocks"])
            content = [b for b in rec["blocks"] if b["is_content"]]
            self.assertEqual(len(secs), len(content), name)  # every block -> exactly one section
            self.assertEqual(len({s.path for s in secs}), len(secs), f"duplicate paths in {name}")
            self.assertEqual([s.order_index for s in secs], list(range(len(secs))), name)

            chunks = chunk_sections(secs, max_tokens=450, min_tokens=60, counter=COUNTER)
            covered = {id(s) for c in chunks for s in c.sections}
            for s in secs:
                if s.text and s.section_type not in (T.SIGNATURE, T.NOTE):
                    self.assertIn(id(s), covered, f"{name}: {s.section_type} {s.text[:40]!r} not in any chunk")
            for c in chunks:
                self.assertLessEqual(c.token_count, 450 * 1.25, f"{name}: chunk {c.chunk_index}")
            checked += 1
        self.assertGreater(checked, 50)
