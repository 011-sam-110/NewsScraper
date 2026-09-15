"""PBS NewsHour extraction tests against pages recorded on 2026-09-14."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import pbs  # noqa: E402
from scraper.common import FetchError  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "pbs"
SITE = "https://www.pbs.org/newshour"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class SectionListingTests(unittest.TestCase):
    """The first World page: 1 extra-large card, 4 large cards, then 5 horizontal cards."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = pbs.parse_listing(fixture("section_world.html"))

    def test_finds_every_story_card_and_skips_episode_promos(self) -> None:
        self.assertEqual(len(self.rows), 10)
        self.assertEqual([row["card_type"] for row in self.rows], ["card-xl"] + ["card-lg"] * 4 + ["card-horiz"] * 5)
        for row in self.rows:
            self.assertNotIn("full-episode", row["url"])

    def test_extra_large_card_fields(self) -> None:
        row = self.rows[0]
        url = f"{SITE}/politics/trump-says-hes-lifting-10-tariff-on-irish-whiskey-because-everybodys-been-bugging-me"
        self.assertEqual(row["url"], url)
        self.assertEqual(row["id"], "/newshour/politics/trump-says-hes-lifting-10-tariff-on-irish-whiskey-because-everybodys-been-bugging-me")
        self.assertEqual(row["headline"], "Trump says he's lifting 10% tariff on Irish whiskey because 'everybody's been bugging me'")
        self.assertEqual(row["listing_date"], "Sep 14")
        self.assertEqual(row["author"], "Darlene Superville, Jill Lawless, Associated Press")
        self.assertEqual(row["authors"], ["Darlene Superville", "Jill Lawless", "Associated Press"])
        self.assertTrue(row["thumbnail"].endswith("RTRMADP_3_USA-TRUMP-IRELAND-1024x683.jpg"))
        self.assertEqual(row["url_section"], "politics")
        self.assertEqual(row["categories"], {"card_section": {"name": "Politics", "url": f"{SITE}/politics"}})

    def test_large_card_has_excerpt_and_lazy_image(self) -> None:
        row = self.rows[1]
        self.assertTrue(row["description"].startswith("President Donald Trump said Sunday"))
        self.assertIn("Saudi Arabia’s alleged role", row["description"])
        self.assertTrue(row["thumbnail"].endswith("RTRMADP_3_USA-TRUMP-IRELAND-1024x683.jpg"))
        self.assertEqual(row["author"], "Associated Press")

    def test_horizontal_card_fields(self) -> None:
        row = self.rows[5]
        self.assertEqual(row["url"], f"{SITE}/world/iran-backed-houthis-in-yemen-claim-new-attacks-on-saudi-arabia")
        self.assertEqual(row["headline"], "Fighting in Yemen intensifies, and Houthis launch more attacks on Saudi Arabia")
        self.assertEqual(row["listing_date"], "Sep 13")
        self.assertTrue(row["description"].startswith("The Yemeni government vowed"))
        self.assertTrue(row["description"].endswith("shipping alternative…"))
        self.assertTrue(row["thumbnail"].endswith("IRAN-CRISIS-YEMEN-HOUTHIS-2-768x512.jpg"))
        self.assertFalse(row["is_video"])
        self.assertIsNone(row["video_duration"])
        # A horizontal card shows no section link.
        self.assertEqual(row["categories"], {})

    def test_listing_does_not_invent_dates_or_text(self) -> None:
        for row in self.rows:
            self.assertIsNone(row["published"])
            self.assertIsNone(row["text"])


class TagListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = pbs.parse_listing(fixture("tag_africa_page2.html"))

    def test_finds_ten_cards(self) -> None:
        self.assertEqual(len(self.rows), 10)
        for row in self.rows:
            for field in ("id", "headline", "url", "description", "listing_date", "thumbnail", "author"):
                self.assertTrue(row[field], f"{field} is empty for {row['url']}")

    def test_video_card(self) -> None:
        row = self.rows[0]
        self.assertEqual(row["url"], f"{SITE}/show/how-the-loss-of-usaid-has-weakened-the-fight-against-ebola")
        self.assertTrue(row["is_video"])
        self.assertEqual(row["video_duration"], "7:02")
        self.assertEqual(row["url_section"], "show")
        self.assertEqual(row["authors"], ["William Brangham", "Azhar Merchant"])

    def test_video_flags_match_the_page(self) -> None:
        self.assertEqual([row["is_video"] for row in self.rows], [True, False, True, True, False, False, False, True, True, False])


class ListPageTests(unittest.TestCase):
    def test_page_urls(self) -> None:
        self.assertEqual(pbs.page_url("world", 0), f"{SITE}/world")
        self.assertEqual(pbs.page_url("world", 1), f"{SITE}/world/page/2")
        self.assertEqual(pbs.page_url("africa", 1), f"{SITE}/tag/africa/page/2")

    def test_list_page_parses_the_fetched_page_and_labels_it(self) -> None:
        requested = f"{SITE}/tag/africa/page/2"
        with mock.patch.object(pbs, "fetch_page", return_value=(requested, fixture("tag_africa_page2.html"))) as fetch:
            rows = pbs.list_page("africa", 1, 20)
        fetch.assert_called_once_with(requested)
        self.assertEqual(len(rows), 10)
        self.assertEqual({row["listing_url"] for row in rows}, {requested})

    def test_section_redirect_past_the_last_page_returns_nothing(self) -> None:
        # Seen live: /newshour/world/page/501 redirects to /newshour/.
        home = (f"{SITE}/", fixture("section_world.html"))
        with mock.patch.object(pbs, "fetch_page", return_value=home):
            self.assertEqual(pbs.list_page("world", 500, 10), [])

    def test_tag_404_past_the_last_page_returns_nothing(self) -> None:
        # Seen live: /newshour/tag/africa/page/65 returns HTTP 404.
        with mock.patch.object(pbs, "fetch_page", side_effect=FetchError("HTTP 404", 404)):
            self.assertEqual(pbs.list_page("africa", 64, 10), [])

    def test_other_fetch_errors_are_raised(self) -> None:
        with mock.patch.object(pbs, "fetch_page", side_effect=FetchError("HTTP 503", 503)):
            with self.assertRaises(RuntimeError):
                pbs.list_page("africa", 0, 10)

    def test_sections(self) -> None:
        self.assertEqual(
            set(pbs.SECTIONS),
            {"world", "politics", "nation", "economy", "science", "health", "arts", "education",
             "africa", "middle-east", "europe", "asia", "latin-america"},
        )
        self.assertEqual(pbs.SECTIONS["africa"], "/newshour/tag/africa")
        self.assertEqual(pbs.SECTIONS["world"], "/newshour/world")


class ArticlePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = pbs.parse_article_page(fixture("article_pope_leo_equatorial_guinea.html"))
        cls.paragraphs = (cls.page["text"] or "").split("\n\n")

    def test_metadata(self) -> None:
        page = self.page
        self.assertEqual(page["post_id"], "554954")
        self.assertEqual(page["post_type"], "post")
        self.assertEqual(page["headline"], "Pope Leo criticizes colonization of minerals, 'lust for power' in Equatorial Guinea")
        self.assertEqual(page["published"], "2026-04-21T14:08:02-04:00")
        self.assertEqual(page["updated"], "2026-04-21T14:08:02-04:00")
        self.assertEqual(page["authors"], ["Nicole Winfield, Associated Press"])
        self.assertEqual(page["author_urls"], [f"{SITE}/author/nicole-winfield-associated-press"])
        self.assertTrue(page["thumbnail"].endswith("POPE-AFRICA-EQUATORIAL-1024x683.jpg"))
        self.assertEqual(page["canonical_url"], f"{SITE}/world/pope-leo-criticizes-colonization-of-minerals-lust-for-power-in-equatorial-guinea")
        self.assertTrue(page["description"].startswith("Pope Leo XIV has denounced the “colonization”"))

    def test_text_is_the_article_body(self) -> None:
        self.assertTrue(self.paragraphs[0].startswith("MALABO, Equatorial Guinea (AP) — Pope Leo XIV arrived"))
        self.assertIn("Two models of cities", self.paragraphs)
        self.assertIn("A secular but very Catholic country", self.paragraphs)
        self.assertTrue(self.paragraphs[-1].startswith("Associated Press writers Monika Pronczuk"))

    def test_text_has_no_funding_box_or_promo_links(self) -> None:
        text = self.page["text"]
        for furniture in ("free press is a cornerstone", "Donate now", "Support trusted journalism", "READ MORE"):
            self.assertNotIn(furniture, text)
        self.assertNotIn("", self.paragraphs)

    def test_categories_are_the_page_tags_and_section(self) -> None:
        categories = self.page["categories"]
        self.assertEqual(
            categories["tags"],
            [
                {"name": "africa", "url": f"{SITE}/tag/africa"},
                {"name": "equatorial guinea", "url": f"{SITE}/tag/equatorial-guinea"},
                {"name": "pope", "url": f"{SITE}/tag/pope"},
                {"name": "pope leo", "url": f"{SITE}/tag/pope-leo"},
                {"name": "pope leo xiv", "url": f"{SITE}/tag/pope-leo-xiv"},
            ],
        )
        self.assertEqual(categories["article_section"], "World")
        self.assertEqual(categories["news_keywords"], ["africa", "equatorial guinea", "pope", "pope leo", "pope leo xiv", "world"])

    def test_article_details_returns_text_categories_and_page_fields(self) -> None:
        with mock.patch.object(pbs.common, "get_html", return_value=fixture("article_pope_leo_equatorial_guinea.html")):
            text, categories, fields = pbs.article_details(f"{SITE}/world/x")
        self.assertEqual(text, self.page["text"])
        self.assertEqual(categories, self.page["categories"])
        self.assertEqual(fields["published"], "2026-04-21T14:08:02-04:00")
        self.assertEqual(fields["updated"], "2026-04-21T14:08:02-04:00")
        self.assertEqual(fields["post_id"], "554954")
        self.assertEqual(fields["post_type"], "post")
        self.assertEqual(fields["authors"], ["Nicole Winfield, Associated Press"])
        self.assertEqual(fields["page_authors"], ["Nicole Winfield, Associated Press"])
        self.assertEqual(fields["author"], "Nicole Winfield, Associated Press")
        self.assertEqual(fields["word_count"], len(text.split()))
        self.assertTrue(fields["thumbnail"].endswith("POPE-AFRICA-EQUATORIAL-1024x683.jpg"))
        self.assertTrue(fields["description"].startswith("Pope Leo XIV has denounced"))
        self.assertNotIn("text", fields)
        self.assertNotIn("categories", fields)

    def test_scrape_fills_empty_row_fields_and_keeps_the_card_byline(self) -> None:
        import tempfile, json
        from scraper import common

        listing = pbs.parse_listing(fixture("tag_africa_page2.html"))[-1:]
        self.assertIn("pope-leo-criticizes", listing[0]["url"])
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(pbs, "list_page", side_effect=lambda s, p, n: listing if p == 0 else []), \
                mock.patch.object(pbs.common, "get_html", return_value=fixture("article_pope_leo_equatorial_guinea.html")), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            common.scrape(Path(folder), [(pbs, "africa")], size=10, delay=0, max_pages=0, include_text=True)
            output = Path(folder) / "pbs" / "africa.jsonl"
            row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["published"], "2026-04-21T14:08:02-04:00")
        self.assertEqual(row["post_id"], "554954")
        self.assertEqual(row["word_count"], len(row["text"].split()))
        self.assertEqual(row["author"], listing[0]["author"])
        self.assertEqual(row["page_authors"], ["Nicole Winfield, Associated Press"])
        self.assertEqual(row["listing_date"], "Apr 21")
        self.assertIn("tags", row["categories"])

    def test_video_page_fields(self) -> None:
        with mock.patch.object(pbs.common, "get_html", return_value=fixture("show_usaid_ebola.html")):
            _, _, fields = pbs.article_details(f"{SITE}/show/x")
        self.assertEqual(fields["post_type"], "broadcast")
        self.assertEqual(fields["updated"], "2026-06-10T21:12:22-04:00")
        self.assertEqual(fields["video_duration"], "PT7M2S")
        self.assertEqual(fields["author"], "William Brangham, Azhar Merchant")

    def test_article_details_is_none_when_the_page_fails(self) -> None:
        with mock.patch.object(pbs.common, "get_html", side_effect=FetchError("HTTP 404", 404)):
            self.assertIsNone(pbs.article_details(f"{SITE}/world/gone"))


class SplitBodyTests(unittest.TestCase):
    """Seen live on 2026-09-14: a newsletter box splits the body into two div.body-text blocks."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = pbs.parse_article_page(fixture("article_split_body_trump_ireland.html"))["text"] or ""

    def test_text_holds_both_body_blocks(self) -> None:
        self.assertTrue(self.text.startswith("DUBLIN (AP) — U.S. President Donald Trump said Saturday"))
        self.assertIn("\"I don't want to cause any problems but I'd love to see it unified,\"", self.text)
        # The first block alone is 69 words; the second block is about 1,088.
        self.assertGreater(len(self.text.split()), 1000)

    def test_text_has_no_newsletter_box(self) -> None:
        for furniture in ("Educate your inbox", "Subscribe", "check your inbox"):
            self.assertNotIn(furniture, self.text)


class VideoPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = pbs.parse_article_page(fixture("show_usaid_ebola.html"))
        cls.paragraphs = (cls.page["text"] or "").split("\n\n")

    def test_metadata(self) -> None:
        page = self.page
        self.assertEqual(page["post_id"], "560158")
        self.assertEqual(page["post_type"], "broadcast")
        self.assertEqual(page["headline"], "How the loss of USAID has weakened the fight against Ebola")
        self.assertEqual(page["published"], "2026-06-10T18:25:14-04:00")
        self.assertEqual(page["updated"], "2026-06-10T21:12:22-04:00")
        self.assertEqual(page["authors"], ["William Brangham", "Azhar Merchant"])
        self.assertEqual(page["video_duration"], "PT7M2S")
        self.assertEqual(page["video_embed_url"], "https://player.pbs.org/viralplayer/3111455670")
        self.assertTrue(page["summary"].startswith("The Ebola outbreak in the Democratic Republic of the Congo"))

    def test_text_is_the_transcript_with_speakers(self) -> None:
        self.assertEqual(self.paragraphs[0], "Geoff Bennett:")
        self.assertTrue(self.paragraphs[1].startswith("The Ebola outbreak in the Democratic Republic"))
        self.assertIn("Jeremy Konyndyk, President, Refugees International:", self.paragraphs)
        self.assertEqual(self.paragraphs[-1], "Jeremy Konyndyk of Refugees International, always great to see you. Thank you.")
        self.assertNotIn("Transcripts are machine and human generated", self.page["text"])
        self.assertNotIn("Listen to this Segment", self.page["text"])

    def test_categories(self) -> None:
        categories = self.page["categories"]
        self.assertEqual([tag["name"] for tag in categories["tags"]], ["africa", "ebola", "jeremy konyndyk", "usaid"])
        self.assertEqual(categories["article_section"], "World")
        self.assertEqual(categories["news_keywords"], ["africa", "ebola", "jeremy konyndyk", "usaid", "world"])


class MarkupFallbackTests(unittest.TestCase):
    def test_page_without_a_body_has_no_text(self) -> None:
        page = pbs.parse_article_page("<html><body><p>Footer</p></body></html>")
        self.assertIsNone(page["text"])
        self.assertEqual(page["categories"], {"tags": [], "article_section": None, "news_keywords": []})

    def test_transcript_falls_back_to_json_ld_article_body(self) -> None:
        page = pbs.parse_article_page(
            '<script type="application/ld+json">[{"@type":"NewsArticle","articleBody":"Line one. Line two."}]</script>'
        )
        self.assertEqual(page["text"], "Line one. Line two.")


if __name__ == "__main__":
    unittest.main()
