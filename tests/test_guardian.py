"""The Guardian extraction tests against responses recorded on 2026-09-14."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import guardian  # noqa: E402
from scraper.common import FetchError  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "guardian"
SITE = "https://www.theguardian.com"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture_bytes(name))


class FeedTests(unittest.TestCase):
    """Page 0 is the section's RSS feed."""

    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(guardian, "fetch", return_value=(None, fixture_bytes("rss_africa.xml"))) as fetch:
            cls.rows = guardian.list_page("africa", 0, 20)
        cls.requested = fetch.call_args.args[0]

    def test_requests_the_section_feed(self) -> None:
        self.assertEqual(self.requested, f"{SITE}/world/africa/rss")

    def test_every_feed_item_is_a_row(self) -> None:
        self.assertEqual(len(self.rows), 20)

    def test_row_mapping(self) -> None:
        row = self.rows[1]
        self.assertEqual(row["id"], "uk-news/2026/sep/14/register-british-slave-traders-africa-research")
        self.assertEqual(row["headline"], "What is the Register of British Slave Traders and whom does it identify?")
        self.assertEqual(row["url"], f"{SITE}/uk-news/2026/sep/14/register-british-slave-traders-africa-research")
        self.assertEqual(row["published"], "2026-09-14T10:20:10+00:00")
        self.assertEqual(row["author"], "David Conn Investigations correspondent")
        self.assertEqual(row["authors"], ["David Conn Investigations correspondent"])
        self.assertTrue(row["description"].startswith("Three years of research has uncovered 13,000 people"))
        self.assertTrue(row["thumbnail"].startswith("https://i.guim.co.uk/"))
        self.assertIsNone(row["text"])
        self.assertEqual(row["listing"], "rss")

    def test_feed_categories_keep_the_guardian_tag_ids(self) -> None:
        tags = self.rows[1]["categories"]["feed_tags"]
        self.assertIn({"id": "uk/uk", "name": "UK news"}, tags)
        self.assertIn({"id": "world/slavery", "name": "Slavery"}, tags)
        for tag in tags:
            self.assertNotIn("theguardian.com", tag["id"])

    def test_every_row_has_the_feed_fields(self) -> None:
        for row in self.rows:
            for field in ("id", "headline", "url", "published", "categories"):
                self.assertTrue(row[field], f"{field} is empty for {row['url']}")

    def test_content_type_comes_from_the_url(self) -> None:
        self.assertEqual(self.rows[0]["content_type"], "video")
        self.assertEqual(self.rows[1]["content_type"], "article")


class ListingPageTests(unittest.TestCase):
    """Pages from 1 are the section's paginated listing, which reaches back years."""

    @classmethod
    def setUpClass(cls) -> None:
        body = fixture_bytes("listing_africa_page2.json")
        final_url = f"{SITE}/world/africa.json?page=2"
        with mock.patch.object(guardian, "fetch", return_value=(final_url, body)) as fetch:
            cls.rows = guardian.list_page("africa", 1, 20)
        cls.requested = fetch.call_args.args[0]

    def test_page_1_requests_listing_page_2(self) -> None:
        # Page 0 (the feed) holds the same stories as listing page 1.
        self.assertEqual(self.requested, f"{SITE}/world/africa.json?page=2")

    def test_every_card_is_a_row(self) -> None:
        self.assertEqual(len(self.rows), 20)

    def test_first_card_fields(self) -> None:
        row = self.rows[0]
        self.assertEqual(row["id"], "world/2026/sep/04/us-deportation-equatorial-guinea-fourth-country-trump")
        self.assertEqual(row["url"], f"{SITE}/world/2026/sep/04/us-deportation-equatorial-guinea-fourth-country-trump")
        self.assertTrue(row["headline"].startswith("Six people refused to get off ICE deportation plane"))
        self.assertEqual(row["published"], "2026-09-04T10:00:04+00:00")
        self.assertEqual(row["short_url"], f"{SITE}/p/x5q8xe")
        self.assertTrue(row["thumbnail"].startswith("https://i.guim.co.uk/img/media/6f37c8bb"))
        self.assertEqual(row["listing"], "page")
        self.assertEqual(row["categories"], {"card_type": "feature", "card_pillar": "news"})

    def test_every_row_has_the_card_fields(self) -> None:
        for row in self.rows:
            for field in ("id", "headline", "url", "published"):
                self.assertTrue(row[field], f"{field} is empty for {row['url']}")

    def test_past_the_last_page_the_redirect_gives_no_rows(self) -> None:
        # Past the end the Guardian redirects to the unpaginated section page.
        body = fixture_bytes("listing_africa_page2.json")
        with mock.patch.object(guardian, "fetch", return_value=(f"{SITE}/world/africa", body)):
            self.assertEqual(guardian.list_page("africa", 5000, 20), [])

    def test_a_missing_page_gives_no_rows(self) -> None:
        with mock.patch.object(guardian, "fetch", side_effect=FetchError("HTTP 404", 404)):
            self.assertEqual(guardian.list_page("africa", 3, 20), [])

    def test_other_fetch_errors_are_raised(self) -> None:
        with mock.patch.object(guardian, "fetch", side_effect=FetchError("HTTP 503", 503)):
            with self.assertRaises(RuntimeError):
                guardian.list_page("africa", 3, 20)


class ArticleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        url = f"{SITE}/world/2026/sep/12/burial-king-oyo-uganda"
        payload = fixture_json("article_king_oyo.json")
        with mock.patch.object(guardian, "fetch_json", return_value=payload) as fetch:
            cls.result = guardian.article_details(url)
        cls.requested = fetch.call_args.args[0]

    def test_requests_the_page_model(self) -> None:
        self.assertEqual(self.requested, f"{SITE}/world/2026/sep/12/burial-king-oyo-uganda.json?dcr=true")

    def test_page_fields(self) -> None:
        _, _, fields = self.result
        self.assertEqual(fields["word_count"], 571)
        self.assertEqual(fields["published"], "2026-09-12T15:09:05.000Z")
        self.assertEqual(fields["updated"], "2026-09-12T16:59:01.000Z")
        self.assertEqual(fields["author"], "Reuters in Kampala")
        self.assertEqual(fields["authors"], ["Reuters in Kampala"])
        self.assertEqual(
            fields["description"],
            "Royal guards carry casket through streets of Fort Portal amid row over appointment of Edward Rukidi Kijanangoma",
        )
        self.assertTrue(fields["thumbnail"].startswith("https://i.guim.co.uk/img/media/96f464b8587057f7a6bf89c2eeb1330f76f923b3/"))
        self.assertEqual(fields["id"], "world/2026/sep/12/burial-king-oyo-uganda")
        self.assertEqual(fields["short_url"], f"{SITE}/p/x6x7cb")
        self.assertEqual(fields["page_content_type"], "Article")
        self.assertEqual(fields["production_office"], "Uk")
        self.assertIs(fields["is_commentable"], False)
        self.assertNotIn("contributor_ids", fields)  # The page has no contributor tags.
        self.assertNotIn("video_duration_seconds", fields)

    def test_page_fields_fill_only_empty_card_fields(self) -> None:
        card = {"id": "world/2026/sep/12/burial-king-oyo-uganda", "headline": "Card headline",
                "url": f"{SITE}/world/2026/sep/12/burial-king-oyo-uganda"}
        row = {**guardian.common.empty_row(), **card, "published": "2026-09-12T15:09:05+00:00"}
        with mock.patch.object(guardian, "list_page", side_effect=[[row], []]), \
                mock.patch.object(guardian, "article_details", return_value=self.result), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            with tempfile.TemporaryDirectory() as folder:
                guardian.common.scrape(Path(folder), [(guardian, "africa")], size=20, delay=0, max_pages=0, include_text=True)
                output = Path(folder) / "guardian" / "africa.jsonl"
                saved = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["headline"], "Card headline")
        self.assertEqual(saved["published"], "2026-09-12T15:09:05+00:00")
        self.assertEqual(saved["word_count"], 571)
        self.assertEqual(saved["author"], "Reuters in Kampala")
        self.assertEqual(saved["updated"], "2026-09-12T16:59:01.000Z")
        self.assertTrue(saved["text"].startswith("Uganda’s King Oyo"))

    def test_text(self) -> None:
        text, _, _ = self.result
        paragraphs = text.split("\n\n")
        self.assertEqual(len(paragraphs), 19)
        self.assertTrue(paragraphs[0].startswith("Uganda’s King Oyo Nyimba Kabamba Iguru Rukidi IV"))
        self.assertTrue(paragraphs[-1].startswith("Besides the throne, the new king will inherit"))
        self.assertNotIn("<", text)
        self.assertNotIn("⁠", text)

    def test_categories_are_the_page_tags(self) -> None:
        _, categories, _ = self.result
        self.assertEqual(categories["keywords"], ["Uganda", "UK news", "World news", "Africa"])
        self.assertEqual(categories["keyword_ids"], ["world/uganda", "uk/uk", "world/world", "world/africa"])
        self.assertEqual(categories["tones"], ["News"])
        self.assertEqual(categories["page_type"], ["Article"])
        self.assertEqual(categories["tracking"], ["UK", "Global", "UK Foreign"])
        self.assertEqual(categories["commissioning_desks"], ["uk-foreign"])
        self.assertEqual(categories["series"], [])
        self.assertEqual(categories["contributors"], [])
        self.assertEqual(categories["page_section"], {"id": "world", "label": "Uganda", "url": "world/uganda"})
        self.assertEqual(categories["pillar"], "news")
        self.assertEqual(categories["design"], "ArticleDesign")
        self.assertEqual(len(categories["page_tags"]), 9)
        self.assertIn({"id": "tone/news", "type": "Tone", "title": "News"}, categories["page_tags"])

    def test_listing_and_page_category_keys_do_not_collide(self) -> None:
        _, categories, _ = self.result
        self.assertFalse({"feed_tags", "card_type", "card_pillar"} & set(categories))


class NonStandardItemTests(unittest.TestCase):
    def test_video_has_no_body_but_keeps_its_tags(self) -> None:
        url = f"{SITE}/uk-news/video/2026/sep/14/how-britains-kings-and-queens-powered-the-slave-trade-video"
        with mock.patch.object(guardian, "fetch_json", return_value=fixture_json("video_slave_trade.json")):
            text, categories, fields = guardian.article_details(url)
        self.assertIsNone(text)
        self.assertEqual(
            fields["authors"],
            ["Nikhita Chulani", "Albert Villa Alsina", "Stephanie Windeler", "Morgan Ofori"],
        )
        self.assertEqual(fields["author"], "Morgan Ofori, Stephanie Windeler, Albert Villa Alsina and Nikhita Chulani")
        self.assertEqual(fields["contributor_ids"][0], "profile/nikhita-chulani")
        self.assertEqual(fields["page_content_type"], "Video")
        self.assertEqual(fields["short_url"], f"{SITE}/p/x6xhc4")
        self.assertEqual(categories["page_type"], ["Video"])
        self.assertEqual(categories["series"], ["Enslaving nation"])
        self.assertEqual(
            categories["contributors"],
            ["Nikhita Chulani", "Albert Villa Alsina", "Stephanie Windeler", "Morgan Ofori"],
        )
        self.assertEqual(categories["design"], "VideoDesign")
        self.assertEqual(categories["commissioning_desks"], ["social"])

    def test_live_blog_posts_start_with_their_titles(self) -> None:
        # Trimmed recording: two posts of a live blog, with their tags.
        url = f"{SITE}/world/live/2026/sep/14/sweden-election-magdalena-andersson-social-democrats-centre-left-latest-news-updates"
        with mock.patch.object(guardian, "fetch_json", return_value=fixture_json("live_blog_sweden_trimmed.json")):
            text, categories, fields = guardian.article_details(url)
        self.assertEqual(fields["published"], "2026-09-14T12:23:43.000Z")
        self.assertEqual(fields["page_content_type"], "LiveBlog")
        paragraphs = text.split("\n\n")
        self.assertEqual(len(paragraphs), 12)
        self.assertEqual(paragraphs[0], "PM Kristersson wants to 'take it easy' and wait until final results")
        self.assertTrue(paragraphs[1].startswith("Perhaps we should all just channel the Swedish PM"))
        self.assertEqual(paragraphs[5], "Social Democrat leadership meets for talks after election win")
        self.assertEqual(categories["tones"], ["Minute by minute", "News"])
        self.assertEqual(categories["keywords"], ["World news", "Sweden", "Europe"])
        self.assertEqual(categories["design"], "LiveBlogDesign")

    def test_legacy_page_shape_still_gives_text_and_tags(self) -> None:
        url = f"{SITE}/world/2026/sep/12/burial-king-oyo-uganda"
        with mock.patch.object(guardian, "fetch_json", return_value=fixture_json("article_king_oyo_legacy.json")):
            text, categories, fields = guardian.article_details(url)
        self.assertEqual(fields["published"], "2026-09-12T15:09:05+00:00")
        self.assertEqual(fields["word_count"], 571)
        self.assertEqual(fields["author"], "Reuters in Kampala")
        self.assertTrue(fields["thumbnail"].startswith("https://i.guim.co.uk/img/media/96f464b8"))
        self.assertTrue(text.startswith("Uganda’s King Oyo Nyimba Kabamba Iguru Rukidi IV"))
        self.assertEqual(categories["keywords"], ["Uganda", "UK news", "World news", "Africa"])
        self.assertEqual(categories["keyword_ids"], ["world/uganda", "uk/uk", "world/world", "world/africa"])
        self.assertEqual(categories["tones"], ["News"])
        self.assertEqual(categories["commissioning_desks"], ["uk-foreign"])

    def test_unknown_payload_gives_none(self) -> None:
        with mock.patch.object(guardian, "fetch_json", return_value={"refreshStatus": True}):
            self.assertIsNone(guardian.article_details(f"{SITE}/world/2026/sep/12/x"))

    def test_fetch_error_gives_none(self) -> None:
        with mock.patch.object(guardian, "fetch_json", side_effect=FetchError("HTTP 404", 404)):
            self.assertIsNone(guardian.article_details(f"{SITE}/world/2026/sep/12/x"))


class SectionTests(unittest.TestCase):
    def test_interface(self) -> None:
        self.assertEqual(guardian.NAME, "guardian")
        self.assertIn("africa", guardian.SECTIONS)
        self.assertIn("europe", guardian.SECTIONS)
        for path in guardian.SECTIONS.values():
            self.assertFalse(path.startswith("/") or path.endswith("/"), path)


if __name__ == "__main__":
    unittest.main()
