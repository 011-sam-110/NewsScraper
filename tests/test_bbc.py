"""BBC News extraction tests against responses recorded on 2026-09-14."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import bbc  # noqa: E402
from scraper.common import FetchError  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "bbc"


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(bbc.common, "get_json", return_value=fixture_json("listing_india_page1_size5.json")) as get_json:
            cls.rows = bbc.list_page("india", 1, 5)
        cls.requested_url = get_json.call_args.args[0]

    def test_requests_the_india_collection_page(self) -> None:
        parts = urlsplit(self.requested_url)
        self.assertTrue(parts.path.endswith("/1a3cd4db-fe3d-46f2-9c9a-927a01b00c91"))
        self.assertEqual(parse_qs(parts.query), {"page": ["1"], "size": ["5"]})

    def test_keeps_every_item_including_sport_and_video(self) -> None:
        self.assertEqual(len(self.rows), 5)
        self.assertEqual([row["type"] for row in self.rows], ["article", "article", "video", "article", "article"])

    def test_article_field_mapping(self) -> None:
        row = self.rows[1]
        self.assertEqual(row["id"], "urn:bbc:optimo:asset:c07lv53l7jjo")
        self.assertEqual(row["asset_id"], "c07lv53l7jjo")
        self.assertEqual(row["headline"], "Trump shadow looms large over Brics as Modi hosts Putin and Xi in Delhi")
        self.assertTrue(row["description"].startswith("As Brics leaders meet in Delhi"))
        self.assertEqual(row["url"], "https://www.bbc.com/news/articles/c07lv53l7jjo")
        self.assertEqual(row["published"], "2026-09-11T09:38:23.506Z")
        self.assertEqual(row["updated"], "2026-09-11T09:38:23.506Z")
        self.assertEqual(
            row["thumbnail"],
            "https://ichef.bbci.co.uk/news/480/cpsprodpb/e0f3/live/80be8820-adcc-11f1-82c1-5ff19bf7a2f2.jpg",
        )
        self.assertEqual((row["thumbnail_width"], row["thumbnail_height"]), (1024, 576))
        self.assertTrue(row["thumbnail_alt"].startswith("Indian Prime Minister Narendra Modi"))
        self.assertEqual((row["subtype"], row["state"]), ("news", "published"))
        self.assertEqual(row["collection_id"], bbc.SECTIONS["india"])

    def test_every_row_has_the_common_fields(self) -> None:
        for row in self.rows:
            for field in ("id", "headline", "description", "published", "updated", "url", "thumbnail",
                          "author", "authors", "text", "word_count", "categories"):
                self.assertIn(field, row)
            for field in ("id", "headline", "url", "published", "thumbnail"):
                self.assertTrue(row[field], f"{field} is empty for {row['url']}")

    def test_listing_categories_are_the_topics_bbc_sends(self) -> None:
        self.assertEqual(self.rows[0]["categories"], {"listing_topics": ["Cricket"]})
        self.assertEqual(self.rows[0]["url"], "https://www.bbc.com/sport/cricket/articles/cx2kz4v4e6xo")
        self.assertEqual(self.rows[2]["categories"], {"listing_topics": []})

    def test_size_is_capped_at_the_api_maximum(self) -> None:
        with mock.patch.object(bbc.common, "get_json", return_value={"data": []}) as get_json:
            bbc.list_page("africa", 0, 500)
        self.assertEqual(parse_qs(urlsplit(get_json.call_args.args[0]).query)["size"], ["100"])

    def test_past_the_last_page_returns_nothing(self) -> None:
        with mock.patch.object(bbc.common, "get_json", return_value=fixture_json("listing_india_page20_size5.json")):
            self.assertEqual(bbc.list_page("india", 20, 5), [])

    def test_every_section_is_a_collection_id(self) -> None:
        self.assertEqual(
            set(bbc.SECTIONS),
            {"world", "africa", "asia", "china", "india", "australia", "europe", "latin_america",
             "middle_east", "us_and_canada", "uk"},
        )
        for collection_id in bbc.SECTIONS.values():
            self.assertRegex(collection_id, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ArticleWithAfricaPromoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text, cls.categories, _ = bbc.parse_article_page(fixture_text("article_zambia_anthrax.html"))

    def test_text_is_the_article_body(self) -> None:
        self.assertTrue(self.text.startswith("Authorities in Zambia have warned people not to eat meat"))
        self.assertTrue(self.text.endswith("The public has also been advised against handling wild animals."))
        self.assertEqual(len(self.text.split("\n\n")), 13)

    def test_text_has_no_promo_or_related_links(self) -> None:
        for furniture in ("BBCAfrica.com", "Follow us on Twitter", "You may also be interested", "Focus on Africa",
                          "Anthrax Island"):
            self.assertNotIn(furniture, self.text)

    def test_text_has_no_markup_or_invisible_characters(self) -> None:
        self.assertNotIn("<", self.text)
        for character in ("​", "‌", "‍", "⁠", "﻿"):
            self.assertNotIn(character, self.text)

    def test_page_categories(self) -> None:
        self.assertEqual(
            self.categories["topics"],
            [
                {"title": "Africa", "id": "cwgdj57820vt", "url": "/news/world/africa", "is_event": False},
                {"title": "Wildlife", "id": "ce2gz91g2jmt", "url": "/news/topics/ce2gz91g2jmt", "is_event": False},
                {"title": "Zambia", "id": "cdl8n2edezlt", "url": "/news/topics/cdl8n2edezlt", "is_event": False},
            ],
        )
        self.assertEqual(self.categories["page_section"], [{"title": "Africa", "url": "/news/world/africa"}])
        self.assertEqual(self.categories["pillar"], ["News"])

    def test_page_categories_do_not_reuse_the_listing_key(self) -> None:
        self.assertNotIn("listing_topics", self.categories)


class ArticleWithLocalRadioPromoTests(unittest.TestCase):
    """The promo here is not after a links block, but BBC marks it suitableForAbridgement: false."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text, cls.categories, _ = bbc.parse_article_page(fixture_text("article_nepal_flood_cumbria.html"))

    def test_text_ends_at_the_last_body_paragraph(self) -> None:
        self.assertTrue(self.text.startswith("An MP has accused the government of failing to help"))
        self.assertTrue(self.text.endswith("I am disappointed that this hasn't led to the outcome that my honourable friend seeks.\""))
        self.assertNotIn("Follow BBC Cumbria", self.text)
        self.assertNotIn("Get in touch", self.text)

    def test_topics_in_page_order(self) -> None:
        self.assertEqual(
            [topic["title"] for topic in self.categories["topics"]],
            ["Newbiggin-by-the-Sea", "Nepal", "Ian Lavery", "Foreign & Commonwealth Office", "Ashington"],
        )


class ArticleWithSubheadingsAndListsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text, _, _ = bbc.parse_article_page(fixture_text("article_ireland_trump_list.html"))

    def test_marked_paragraphs_inside_the_body_are_kept(self) -> None:
        self.assertIn("Meanwhile, more than 10,000 people marched through Dublin", self.text)
        self.assertIn("Irish authorities have been planning for Trump's visit for months", self.text)

    def test_subheadings_that_head_body_text_are_kept(self) -> None:
        self.assertIn("\n\nProtesters turn out against Trump\n\n", self.text)

    def test_a_list_of_links_is_not_body_text(self) -> None:
        self.assertNotIn("As it happened", self.text)

    def test_text_ends_with_the_last_paragraph(self) -> None:
        self.assertTrue(self.text.endswith('"I think our overzealous approach to this welcome is fundamentally crazy."'))


class NonArticlePageTests(unittest.TestCase):
    def test_video_page_gives_topics_and_no_link_only_text(self) -> None:
        text, categories, _ = bbc.parse_article_page(fixture_text("video_cx2z0512erro.html"))
        self.assertIsNone(text)
        self.assertEqual([topic["title"] for topic in categories["topics"]], ["India", "Schools"])

    def test_sport_page_without_next_data_gives_nothing(self) -> None:
        self.assertIsNone(bbc.parse_article_page(fixture_text("sport_cx2kz4v4e6xo.html")))

    def test_article_details_returns_none_when_the_fetch_fails(self) -> None:
        with mock.patch.object(bbc.common, "get_html", side_effect=FetchError("HTTP 404", 404)):
            self.assertIsNone(bbc.article_details("https://www.bbc.com/news/articles/missing"))

    def test_article_details_parses_the_fetched_page(self) -> None:
        with mock.patch.object(bbc.common, "get_html", return_value=fixture_text("article_zambia_anthrax.html")) as get_html:
            text, categories, fields = bbc.article_details("https://www.bbc.com/news/articles/ce8en6v3601o")
        self.assertEqual(get_html.call_args.args[0], "https://www.bbc.com/news/articles/ce8en6v3601o")
        self.assertIn("Munyamadzi", text)
        self.assertEqual(len(categories["topics"]), 3)
        self.assertEqual(fields["author"], "Basillioh Rukanga")


class PageFieldTests(unittest.TestCase):
    def fields(self, name: str) -> tuple:
        text, _, fields = bbc.parse_article_page(fixture_text(name))
        return text, fields

    def test_article_page_fields(self) -> None:
        text, fields = self.fields("article_zambia_anthrax.html")
        self.assertEqual(fields["authors"], [{"name": "Basillioh Rukanga", "role": None, "location": None}])
        self.assertEqual(fields["author"], "Basillioh Rukanga")
        # firstPublished and lastUpdated, as epoch ms on the page. JSON-LD datePublished agrees.
        self.assertEqual(fields["published"], "2026-09-14T11:35:08.586Z")
        self.assertEqual(fields["updated"], "2026-09-14T11:35:12.000Z")
        self.assertEqual(fields["page_last_published"], "2026-09-14T11:35:08.586Z")
        self.assertEqual(fields["word_count"], len(text.split()))
        self.assertEqual(fields["bbc_word_count"], 434)
        self.assertEqual(fields["content_id"], "urn:bbc:optimo:asset:ce8en6v3601o")
        self.assertEqual(fields["language"], "en-gb")
        self.assertEqual(fields["seo_headline"], "Anthrax outbreak: Zambians warned not to eat dead wildlife")
        self.assertEqual(fields["image_copyright"], "Reuters")
        self.assertEqual(fields["page_type"], "article")

    def test_several_contributors_keep_bbc_names_and_locations(self) -> None:
        _, fields = self.fields("article_ireland_trump_list.html")
        self.assertEqual(
            fields["authors"],
            [
                {"name": "Gabija Gataveckaite, Mark Simpson and Jessica Lawrence", "role": None, "location": "in Dublin"},
                {"name": "Niall McCracken", "role": None, "location": "in Doonbeg, County Clare"},
            ],
        )
        self.assertEqual(fields["author"], "Gabija Gataveckaite, Mark Simpson and Jessica Lawrence, Niall McCracken")
        # lastPublished matches the JSON-LD dateModified on the same page.
        self.assertEqual(fields["page_last_published"], "2026-09-12T13:56:20.587Z")

    def test_contributor_role(self) -> None:
        _, fields = self.fields("article_nepal_flood_cumbria.html")
        self.assertEqual(fields["authors"][0]["role"], "Local Democracy Reporting Service")

    def test_video_page_fields(self) -> None:
        _, fields = self.fields("video_cx2z0512erro.html")
        self.assertEqual((fields["authors"], fields["author"], fields["word_count"]), ([], None, None))
        # The listing sent the same firstPublishedAt for this video.
        self.assertEqual(fields["published"], "2026-09-11T08:42:57.946Z")
        self.assertEqual(fields["updated"], "2026-09-11T08:42:57.946Z")
        self.assertEqual((fields["video_pid"], fields["video_duration_seconds"]), ("p0p900ss", 88))


class ScrapeMergeTests(unittest.TestCase):
    def test_page_fields_fill_only_what_the_listing_left_empty(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout

        from scraper import common

        listing = fixture_json("listing_india_page1_size5.json")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(bbc.common, "get_json", return_value=listing), \
                mock.patch.object(bbc.common, "get_html", return_value=fixture_text("article_zambia_anthrax.html")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            common.scrape(Path(directory), [(bbc, "india")], size=5, delay=0, max_pages=1, include_text=True)
            output = Path(directory) / "bbc" / "india.jsonl"
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        row = rows[1]
        self.assertEqual(row["published"], "2026-09-11T09:38:23.506Z")  # the listing's value is kept
        self.assertEqual(row["page_first_published"], "2026-09-14T11:35:08.586Z")
        self.assertEqual(row["author"], "Basillioh Rukanga")
        self.assertEqual(row["word_count"], len(row["text"].split()))


if __name__ == "__main__":
    unittest.main()
