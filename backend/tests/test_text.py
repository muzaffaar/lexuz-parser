from datetime import date
from unittest import TestCase

from apps.legal_documents.parsing.annotations import parse_annotations, parse_classification
from apps.legal_documents.parsing.cards import map_status, parse_card1, parse_passport
from apps.legal_documents.parsing.titles import external_id_and_edition, number_key, parse_date, parse_document_link, parse_title
from apps.legal_documents.text import detect
from apps.legal_documents.text.normalize import html_to_text, normalize_search, query_variants
from apps.legal_documents.text.translit import cyrillic_to_latin


class HtmlToTextTests(TestCase):
    def test_inline_tags_do_not_inject_spaces(self):
        # the crawler's own text produced "501 -son" and "3-бандда :"
        self.assertEqual(html_to_text('<div>501<span>-son</span></div>'), "501-son")
        self.assertEqual(html_to_text("<div>3-бандда<b>:</b></div>"), "3-бандда:")

    def test_real_spaces_between_words_are_kept(self):
        self.assertEqual(html_to_text("<div>a <b>b</b> c</div>"), "a b c")

    def test_br_and_blocks_break_lines(self):
        self.assertEqual(html_to_text("<div>one<br>two</div>"), "one\ntwo")
        self.assertEqual(html_to_text("<div><p>one</p><p>two</p></div>"), "one\ntwo")

    def test_table_rows_become_pipe_lines(self):
        html = "<div><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></div>"
        self.assertEqual(html_to_text(html), "A | B\n1 | 2")

    def test_nul_and_control_characters_removed(self):
        self.assertEqual(html_to_text("<div>ab\x00c\x07d</div>"), "abcd")

    def test_nbsp_and_whitespace_collapsed(self):
        self.assertEqual(html_to_text("<div>a   \n  b</div>"), "a b")

    def test_script_and_style_ignored(self):
        self.assertEqual(html_to_text("<div>x<script>evil()</script><style>p{}</style>y</div>"), "xy")


class ParserChoiceTests(TestCase):
    """html.parser replaced lxml on the hot path: it must extract exactly the same text, including for the
    awkward markup the site produces (nested tables, unclosed tags, entities, stray br)."""

    CASES = [
        "<div class='ACT_TEXT lx_elem'>1. Matn <b>qalin</b> va <i>ogʻma</i>, 12<sup>2</sup> m<sup>2</sup>.</div>",
        "<div><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2 <br> 3</td></tr></table></div>",
        "<div><table><tr><td><table><tr><td>ichki</td></tr></table></td><td>tashqi</td></tr></table></div>",
        "<div>bir<br>ikki<br/>uch<p>toʻrt<p>besh</div>",
        "<div>&lt;script&gt; &amp; &nbsp;bo&#39;sh&nbsp;joy &#x2014; tire</div>",
        "<div><p>ochiq<span>yopilmagan <b>qalin</div>",
        "<div>  ko'p \n\n  bo'shliq \t bilan  </div>",
        "<div class='lx_elem'>Ўзбекистон <a href='/docs/1#5'>ҳужжат</a>га</div>",
    ]

    def test_html_parser_and_lxml_extract_identical_text(self):
        from bs4 import BeautifulSoup
        from unittest import mock

        from apps.legal_documents.text import normalize as N

        real = N.BeautifulSoup
        for html in self.CASES:
            with mock.patch.object(N, "BeautifulSoup", lambda markup, _features: real(markup, "lxml")):
                lxml_text = N.html_to_text(html)
            self.assertEqual(N.html_to_text(html), lxml_text, html)

    def test_hot_path_does_not_use_lxml(self):
        from apps.legal_documents.text.normalize import HTML_PARSER

        self.assertEqual(HTML_PARSER, "html.parser")


class SearchNormalizationTests(TestCase):
    def test_uzbek_cyrillic_and_latin_fold_to_the_same_key(self):
        latin = normalize_search("Oʻzbekiston Respublikasi Vazirlar Mahkamasi", language="uz", script="latn")
        cyril = normalize_search("Ўзбекистон Республикаси Вазирлар Маҳкамаси", language="uz", script="cyrl")
        self.assertEqual(latin, cyril)

    def test_apostrophe_variants_are_equivalent(self):
        variants = ["o'zbek", "oʻzbek", "o’zbek", "o‘zbek", "oʼzbek", "o`zbek"]
        self.assertEqual({normalize_search(v) for v in variants}, {"ozbek"})

    def test_russian_stays_cyrillic(self):
        self.assertEqual(normalize_search("Кабинет Министров", language="ru", script="cyrl"), "кабинет министров")

    def test_query_variants_for_ambiguous_cyrillic(self):
        v = query_variants("Ўзбекистон қонун")
        self.assertEqual(len(v), 2)
        self.assertIn("ozbekiston qonun", v)
        self.assertEqual(query_variants("plain latin"), ["plain latin"])

    def test_yo_at_word_start_and_after_vowel(self):
        self.assertEqual(cyrillic_to_latin("етказиш"), "yetkazish")
        self.assertEqual(cyrillic_to_latin("келиш"), "kelish")


class DetectTests(TestCase):
    def test_uzbek_cyrillic(self):
        g = detect.detect("Ўзбекистон Республикаси Президентининг қарори тўғрисида ҳужжат")
        self.assertEqual((g.language, g.script), ("uz", "cyrl"))

    def test_russian(self):
        g = detect.detect("Постановление Кабинета Министров Республики Узбекистан об утверждении Положения о порядке")
        self.assertEqual((g.language, g.script), ("ru", "cyrl"))

    def test_english(self):
        g = detect.detect("On measures to improve the efficiency of the system and the approval of the regulation")
        self.assertEqual((g.language, g.script), ("en", "latn"))

    def test_uzbek_latin(self):
        g = detect.detect("Oʻzbekiston Respublikasi Prezidentining qarori va toʻgʻrisida")
        self.assertEqual((g.language, g.script), ("uz", "latn"))

    def test_too_short_cyrillic_is_not_guessed_as_russian(self):
        g = detect.detect("Тошкент ш.,")
        self.assertFalse(g.confident)
        self.assertEqual(g.language, "")

    def test_card_value_wins(self):
        self.assertEqual(detect.from_card_value("Узбекский (к)").script, "cyrl")
        self.assertEqual(detect.from_card_value("Узбекский (л)").script, "latn")
        self.assertEqual(detect.from_card_value("Русский").language, "ru")
        self.assertIsNone(detect.from_card_value(""))


class TitleTests(TestCase):
    def test_variants(self):
        t = parse_title("PQ-330-сон 17.09.2026. “Qoraqalpogʻiston”")
        self.assertEqual((t.number, t.date, t.title), ("PQ-330", date(2026, 9, 17), "“Qoraqalpogʻiston”"))
        self.assertEqual(parse_title("3481-4-сон 17.09.2026. Ички").number, "3481-4")
        self.assertEqual(parse_title("КСҚ-8-сон 22.09.2026. Ўзбекистон").number, "КСҚ-8")
        self.assertEqual(parse_title("501-сон 17.09.2026. X").number, "501")

    def test_numberless_dated_titles_treaties_and_laws(self):
        for raw, expected_date, expected_title in (
            ("04.11.1997. Xalqaro avtomobil aloqasi toʻgʻrisida", date(1997, 11, 4), "Xalqaro avtomobil aloqasi toʻgʻrisida"),
            ("14.09.2022. Об упрощении визовых процессов", date(2022, 9, 14), "Об упрощении визовых процессов"),
            ("18.09.2023. On Cooperation and Consultations", date(2023, 9, 18), "On Cooperation and Consultations"),
            ("26.10.1961. Ижрочилар, фонограммалар тайёрловчилар", date(1961, 10, 26), "Ижрочилар, фонограммалар тайёрловчилар"),
        ):
            t = parse_title(raw)
            self.assertEqual((t.number, t.date, t.title), ("", expected_date, expected_title), raw)

    def test_a_numbered_title_still_wins_over_the_date_only_pattern(self):
        self.assertEqual(parse_title("PQ-330-сон 17.09.2026. X").number, "PQ-330")

    def test_impossible_leading_date_is_not_taken_as_a_date(self):
        t = parse_title("31.02.2026. Nonsense")
        self.assertEqual((t.date, t.title), (None, "31.02.2026. Nonsense"))

    def test_unparseable_title_is_kept_verbatim(self):
        t = parse_title("Just a title")
        self.assertEqual((t.number, t.date, t.title), ("", None, "Just a title"))

    def test_number_key_folds_scripts(self):
        self.assertEqual(number_key("ПҚ-330"), number_key("PQ-330"))
        self.assertEqual(number_key("КСҚ-8"), "ksq-8")

    def test_invalid_date(self):
        self.assertIsNone(parse_date("31.02.2026"))
        self.assertEqual(parse_date("01.07.1997 00"), date(1997, 7, 1))

    def test_document_links(self):
        self.assertEqual(external_id_and_edition("https://lex.uz/uz/docs/-8489227"), ("-8489227", None))
        link = parse_document_link("/docs/545128?ONDATE=01.07.1997 00#-123")
        self.assertEqual((link.external_id, link.edition_on, link.anchor), ("545128", date(1997, 7, 1), "-123"))
        for skip in ("/docs/1?type=doc", "/docs/1?ONDATE2=01.01.2020", "/pdfs/1", "https://evil.example/docs/1", "/actinfo/card1/1"):
            self.assertIsNone(parse_document_link(skip), skip)


class CardTests(TestCase):
    CARD1 = """<table><tr><td class="lbl">Ҳужжат номи</td><td class="codL">X</td></tr>
    <tr><td class="lbl">Ҳужжат тури</td><td class="codL">Акты Президента</td><td class="lbl">Ҳужжат шакли</td><td class="codL">Указ</td></tr>
    <tr><td class="lbl">Ҳужжат тили</td><td class="codL">Узбекский (к)</td></tr>
    <tr><td class="lbl">Ҳужжат ҳолати</td><td class="codL">Действующий</td></tr>
    <tr><td class="lbl">Кучга кириш санаси</td><td class="codR">24.09.2026</td></tr>
    <tr><td class="lbl">Кучини йўқотган санаси</td><td class="codR"></td></tr></table>"""

    def test_card1_by_label(self):
        info = parse_card1(self.CARD1)
        self.assertEqual((info.document_type, info.document_form), ("Акты Президента", "Указ"))
        self.assertEqual(info.status, "active")
        self.assertEqual(info.effective_from, date(2026, 9, 24))
        self.assertIsNone(info.effective_to)
        self.assertEqual(info.language_value, "Узбекский (к)")

    def test_status_mapping(self):
        self.assertEqual(map_status("Действующий"), "active")
        self.assertEqual(map_status("Утратил силу"), "expired")
        self.assertEqual(map_status("Не вступил в силу"), "not_yet_effective")
        self.assertEqual(map_status("что-то новое"), "unknown")  # never guessed
        self.assertEqual(map_status(""), "unknown")

    def test_passport_requisites(self):
        html = '<div class="lx_lp_subtitle">Ўзбекистон Республикаси Вазирлар Маҳкамасининг қарори, 22.09.2026 йилдаги 508-сон</div>'
        info = parse_passport(html)
        self.assertEqual(info.authority, "Ўзбекистон Республикаси Вазирлар Маҳкамасининг")
        self.assertEqual(info.document_form, "қарори")
        self.assertEqual((info.adopted_at, info.document_number), (date(2026, 9, 22), "508"))

    def test_missing_passport_is_harmless(self):
        self.assertEqual(parse_passport("<div>nothing</div>").authority, "")


class AnnotationTests(TestCase):
    def test_classification_with_codes_and_multiple_entries(self):
        got = parse_classification("[ OKOZ: 1. 05.00.00.00 Mehnat / 05.05.00.00 Intizom / 05.05.01.00 Umumiy; 2. 12.00.00.00 Axborot ]")
        self.assertEqual([c.code for c in got], ["05.05.01.00", "12.00.00.00"])
        self.assertTrue(all(c.system == "okoz" for c in got))

    def test_system_aliases_and_placeholder(self):
        got = parse_classification("[ LQBL: 1. 08.00.00.00 Housing ] [ TDL: 1. Excuse, there is no description.... ]")
        self.assertEqual([c.system for c in got], ["okoz"])
        self.assertEqual(parse_classification("[ ТСЗ: 1. Ижтимоий / Меҳнат ]")[0].system, "tsz")

    def test_publication_origin_and_comments(self):
        blocks = [
            {"classes": ["PUBLICATION_ORIGIN"], "html": "<div>(Қонунчилик маълумотлари миллий базаси, 23.09.2026 й., 09/26/505/0960-сон)</div>"},
            {"classes": ["COMMENT"], "html": "<div>LexUZ sharhi Qarang: Yer kodeksi</div>", "order": 4},
            {"classes": ["COMMENT_FOR_WARNING"], "html": "<div>The text of the act is given in uzbek and russian</div>"},
        ]
        info = parse_annotations(blocks)
        self.assertEqual(info.published_at, date(2026, 9, 23))
        self.assertEqual(info.registry_number, "09/26/505/0960")
        self.assertEqual(len(info.comments), 1)
        self.assertEqual(len(info.warnings), 1)
