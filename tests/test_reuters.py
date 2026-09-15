"""Reuters extraction tests against responses recorded on 2026-09-14."""

import json
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import reuters  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "reuters"


class SectionPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads((FIXTURES / "section_africa.json").read_text(encoding="utf-8"))
        cls.articles = [reuters.normalise_article(raw) for raw in reuters.find_articles(payload)]

    def test_finds_every_article_on_the_page(self) -> None:
        self.assertEqual(len(self.articles), 8)

    def test_first_article_fields(self) -> None:
        article = self.articles[0]
        self.assertEqual(article["published"], "2026-09-14T07:02:27.242Z")
        self.assertEqual(article["short_bio"], "Grid & Infrastructure")
        self.assertTrue(article["thumbnail"].endswith("/C2UINQE5ZBM7FOTGI56GD3D554.jpg"))
        self.assertEqual(article["read_minutes"], 3)
        self.assertEqual(article["author"], "Isaac Anyaogu")
        self.assertEqual(article["word_count"], 432)

    def test_every_article_has_the_listing_fields(self) -> None:
        for article in self.articles:
            for field in ("headline", "url", "published", "thumbnail", "short_bio", "author", "slug"):
                self.assertTrue(article[field], f"{field} is empty for {article['url']}")

    def test_author_bio_is_set_only_when_reuters_sends_one(self) -> None:
        bios = [article["author_bio"] for article in self.articles if article["author_bio"]]
        self.assertEqual(len(bios), 1)
        self.assertTrue(bios[0].startswith("Marc Jones is"))

    def test_listing_categories(self) -> None:
        self.assertEqual(
            self.articles[0]["categories"],
            {
                "section": ["Business", "Energy"],
                "primary_topic": "Grid & Infrastructure",
                "ad_topics": ["com", "engy", "ivbk"],
            },
        )

    def test_every_article_has_a_section(self) -> None:
        for article in self.articles:
            self.assertTrue(article["categories"]["section"], f"no section for {article['url']}")


class ArticlePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = reuters.ArticlePageParser()
        parser.feed((FIXTURES / "article_dangote_ipo.html").read_text(encoding="utf-8"))
        cls.text = parser.get_article_body() or ""

    def test_text_is_the_article_body(self) -> None:
        self.assertIn("Nigerian billionaire Aliko Dangote on Monday launched", self.text[:200])
        self.assertGreaterEqual(len(self.text.split("\n\n")), 13)

    def test_text_length_matches_the_reported_word_count(self) -> None:
        # The article page reports word_count 345: the body plus the 4-word dateline.
        # The section listing reported 432 for the same article, so do not compare with that.
        self.assertEqual(len(self.text.split()), 345 - 4)

    def test_text_has_no_page_furniture(self) -> None:
        for furniture in ("Power Up newsletter", "Trust Principles", "All rights reserved", "device characteristics"):
            self.assertNotIn(furniture, self.text)

    def test_text_has_no_markup_or_invisible_characters(self) -> None:
        self.assertNotIn("<a ", self.text)
        for invisible in ("\u200b", "\u2060", "\ufeff"):
            self.assertNotIn(invisible, self.text)

    def test_falls_back_to_paragraph_divs_without_fusion_content(self) -> None:
        parser = reuters.ArticlePageParser()
        parser.feed(
            '<p>Cookie banner</p><div data-testid="paragraph-0">First <a href="/x">line</a>.</div>'
            '<div data-testid="paragraph-1">Second\u200b line.</div>'
        )
        self.assertEqual(parser.get_article_body(), "First line.\n\nSecond line.")

    def test_returns_none_when_no_article_body_exists(self) -> None:
        parser = reuters.ArticlePageParser()
        parser.feed("<p>Cookie banner</p><p>Footer</p>")
        self.assertIsNone(parser.get_article_body())


class ArticlePageCategoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser = reuters.ArticlePageParser()
        parser.feed((FIXTURES / "article_dangote_ipo.html").read_text(encoding="utf-8"))
        cls.categories = parser.get_categories()

    def test_topics_are_the_named_reuters_topics(self) -> None:
        self.assertEqual(
            self.categories["topics"],
            ["Grid & Infrastructure", "Petrochemicals", "Refining", "Transport Fuels"],
        )

    def test_subjects_carry_the_n2_code_and_name(self) -> None:
        subjects = self.categories["subjects"]
        self.assertEqual(len(subjects), 39)
        self.assertIn({"code": "IPO", "name": "Initial Public Offerings"}, subjects)
        self.assertIn({"code": "NG", "name": "Nigeria"}, subjects)
        # Reuters sends some codes without a name; keep the code.
        self.assertIn({"code": "BIZ", "name": None}, subjects)

    def test_distribution_codes_are_not_categories(self) -> None:
        values = json.dumps(self.categories)
        self.assertNotIn("DEST:", values)

    def test_sections_keywords_and_place(self) -> None:
        self.assertEqual(self.categories["sections"], ["Africa", "Business", "Energy", "Commodities"])
        self.assertEqual(self.categories["keywords"], ["NIGERIA", "DANGOTE/IPO (UPDATE 1, PIX, TV)"])
        self.assertEqual(self.categories["place"], "LAGOS")

    def test_dateline_without_a_city_is_not_a_place(self) -> None:
        # Seen live on 2026-09-14: a Washington story with no city in its dateline.
        result = {"taxonomy": {}, "additional_properties": {"article_properties": {"place": "Sept 14 (Reuters)"}}}
        self.assertIsNone(reuters.page_categories(result)["place"])

    def test_page_without_fusion_content_has_no_categories(self) -> None:
        parser = reuters.ArticlePageParser()
        parser.feed('<div data-testid="paragraph-0">Only text.</div>')
        self.assertEqual(parser.get_categories(), {})


class LiveBlogTests(unittest.TestCase):
    """A live blog recorded on 2026-09-14. Its Fusion body is only an Arena embed; the posts are in JSON-LD."""

    @classmethod
    def setUpClass(cls) -> None:
        parser = reuters.ArticlePageParser()
        parser.feed((FIXTURES / "live_blog_hormuz.html").read_text(encoding="utf-8"))
        cls.text = parser.get_article_body() or ""
        cls.categories = parser.get_categories()

    def test_text_holds_every_post_headline_and_body(self) -> None:
        self.assertTrue(self.text.startswith("What you need to know\n\nA planned meeting between Iran"))
        self.assertIn("US prevents Iran's nuclear chief from attending IAEA meeting", self.text)

    def test_text_has_no_embed_markup(self) -> None:
        for markup in ("arena", "<script", "<div"):
            self.assertNotIn(markup, self.text)

    def test_categories_keep_the_sections(self) -> None:
        self.assertIn("Middle East", self.categories["sections"])
        self.assertEqual(self.categories["subjects"], [])


class SectionQueryTests(unittest.TestCase):
    def test_the_api_query_names_the_section(self) -> None:
        url = reuters.build_url("/world/europe/", 16, 3, 8)
        query = json.loads(parse_qs(urlsplit(url).query)["query"][0])
        self.assertEqual((query["section_id"], query["uri"], query["offset"]), ("/world/europe/", "/world/europe/", 16))

    def test_list_page_asks_for_the_section_path_at_the_page_offset(self) -> None:
        payload = json.loads((FIXTURES / "section_africa.json").read_text(encoding="utf-8"))
        calls = []
        with mock.patch.object(reuters, "fetch_page", lambda *args: calls.append(args) or payload):
            rows = reuters.list_page("europe", 2, 8)
        self.assertEqual(calls, [("/world/europe/", 16, 3, 8)])
        self.assertEqual({row["section_path"] for row in rows}, {"/world/europe/"})

    def test_sections_are_the_world_menu(self) -> None:
        # The Reuters World menu on 2026-09-14 had these 15 entries.
        self.assertEqual(len(reuters.SECTIONS), 15)


if __name__ == "__main__":
    unittest.main()
