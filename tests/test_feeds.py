"""Provenance's RSS feeds as outlets, replayed from saved real feeds. No network.

Fixtures in tests/fixtures/feeds/ are real responses from 2026-11-05, cut to three items, with
full-text elements (content:encoded) removed: this repo is public and holds no article text.
"""

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import DROPPED_OUTLETS, FEED_GROUP, FEED_OUTLETS, SOURCES, common, feeds, rss_outlets  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


class RecordedFeed:
    """Stands in for common.http_get: serves one saved document and records every URL asked for."""

    def __init__(self, document: bytes) -> None:
        self.document = document
        self.urls: list[str] = []

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> bytes:
        self.urls.append(url)
        return self.document


def replay(slug: str, page: int = 0) -> tuple[list[dict[str, Any]], RecordedFeed]:
    recorded = RecordedFeed((FIXTURES / f"{slug}.xml").read_bytes())
    with mock.patch.object(common, "http_get", recorded):
        rows = SOURCES[slug].list_page("feed", page, 8)
    return rows, recorded


class FeedOutletsTest(unittest.TestCase):
    def test_rss2_feed_gives_rows_with_headline_url_and_time(self) -> None:
        rows, recorded = replay("globalvoices")
        self.assertEqual(len(rows), 3)
        self.assertEqual(recorded.urls, ["https://globalvoices.org/feed/"])
        for row in rows:
            self.assertTrue(row["headline"])
            self.assertTrue(row["url"].startswith("https://"))
            self.assertRegex(row["published"], r"^\d{4}-\d\d-\d\dT")
            self.assertIsNone(row["text"])

    def test_an_rss1_rdf_feed_is_read(self) -> None:
        # A parser case on a saved document. DW's feed left the schedule on 2026-11-05 (its terms say
        # no), but its file is the one saved example of RSS 1.0: items in the RSS 1.0 namespace, dated
        # with dc:date.
        items = common.parse_rss((FIXTURES / "dw.xml").read_bytes())
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item["title"] and item["link"] for item in items))
        self.assertTrue(all(item["published"] for item in items), "dc:date is the date in RSS 1.0")

    def test_allafrica_is_rss2_whatever_its_file_name_says(self) -> None:
        # The address ends in .rdf, but the body is RSS 2.0 with a plain pubDate.
        rows, _ = replay("allafrica")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["headline"] and row["url"] and row["published"] for row in rows))

    def test_a_date_inside_cdata_is_read(self) -> None:
        # A parser case on a saved document (The Diplomat left the schedule on 2026-11-05). It wraps
        # pubDate in CDATA: <pubDate><![CDATA[Thu, 05 Nov 2026 ...]]></pubDate>.
        items = common.parse_rss((FIXTURES / "thediplomat.xml").read_bytes())
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item["published"] for item in items))

    def test_an_atom_feed_is_read(self) -> None:
        # GOV.UK publishes only Atom: <entry>, <link rel="alternate" href>, <updated>, <id>.
        rows, recorded = replay("govuk")
        self.assertEqual(recorded.urls, ["https://www.gov.uk/search/news-and-communications.atom"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            rows[0]["headline"],
            "International Development Minister speech at Education Cannot Wait Replenishment Conference",
        )
        self.assertEqual(
            rows[1]["url"], "https://www.gov.uk/government/news/law-officers-mark-25-years-of-pro-bono-week"
        )
        self.assertEqual(rows[0]["published"], "2026-11-05T10:34:44+00:00")
        self.assertTrue(rows[0]["id"].startswith("tag:www.gov.uk,2005:/government/speeches/"))
        self.assertTrue(all(row["description"] for row in rows))

    def test_kyiv_post_items_credited_to_afp_are_not_kept(self) -> None:
        # Kyiv Post's permission covers Kyiv Post material. Its feed also carries AFP items (8 of 100
        # on 2026-11-05); AFP sells licences only. The fixture's middle item is credited to AFP.
        rows, _ = replay("kyivpost")
        self.assertEqual(
            [row["url"] for row in rows],
            ["https://www.kyivpost.com/post/89212", "https://www.kyivpost.com/post/89206"],
        )
        self.assertTrue(all(row["published"].startswith("2026-11-05T") for row in rows))

    def test_a_third_party_credit_is_matched_whole_and_only_for_its_feed(self) -> None:
        for credit in ("AFP", "Interfax-Ukraine", "interfax", "Eurotopics", "TVP World", "ISW", "Reuters"):
            self.assertTrue(rss_outlets.third_party("kyivpost", {"creators": [credit]}), credit)
        for credit in ("Kyiv Post", "Stefan Korshak", "Afpina Smith", "Leo Chiu"):
            self.assertFalse(rss_outlets.third_party("kyivpost", {"creators": [credit]}), credit)
        self.assertFalse(rss_outlets.third_party("globalvoices", {"creators": ["AFP"]}))

    def test_the_feeds_added_later_on_2026_11_05_are_registered(self) -> None:
        feeds_by_slug = {slug: (name, url) for slug, name, url in rss_outlets.FEEDS}
        self.assertEqual(
            feeds_by_slug["ukrinform"], ("Ukrinform", "https://www.ukrinform.net/rss/block-lastnews")
        )
        self.assertEqual(feeds_by_slug["kyivpost"], ("Kyiv Post", "https://www.kyivpost.com/feed"))
        self.assertEqual(feeds_by_slug["globalvoices"], ("Global Voices", "https://globalvoices.org/feed/"))
        self.assertEqual(
            feeds_by_slug["govuk"], ("GOV.UK", "https://www.gov.uk/search/news-and-communications.atom")
        )
        rows, _ = replay("globalvoices")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["headline"], "Unmarried Chinese women are rewriting the marriage script")
        self.assertIsNone(rows[0]["text"])
        self.assertEqual(
            feeds_by_slug["agenciabrasil"],
            ("Agência Brasil", "https://agenciabrasil.ebc.com.br/en/rss/ultimasnoticias/feed.xml"),
        )
        # The fixture keeps each <description> element empty: in this feed it is the whole article.
        rows, _ = replay("agenciabrasil")
        self.assertEqual(
            [row["headline"] for row in rows],
            [
                "New lab helps fight illegal gold mining in the Amazon",
                "Brazil’s COP30 roadmaps are well received by environmentalists",
                "COP30 presidency releases roadmaps for energy, forestry transitions",
            ],
        )
        self.assertEqual(rows[0]["published"], "2026-11-04T13:52:00-03:00")
        self.assertTrue(rss_outlets.third_party("agenciabrasil", {"creators": ["Reuters"]}))
        self.assertFalse(rss_outlets.third_party("agenciabrasil", {"creators": ["Fabíola Sinimbú"]}))

    def test_the_mainstream_feeds_added_on_2026_11_05_read_from_their_saved_feeds(self) -> None:
        # Sam chose these under the "silent terms" rule (and Courthouse News under its own yes). Each
        # fixture is the real feed of that day cut to three items, with every description emptied.
        expected = {
            "courthousenews": ("Courthouse News Service", "https://www.courthousenews.com/feed/"),
            "semafor": ("Semafor", "https://www.semafor.com/rss.xml"),
            "nit": ("National Indigenous Times", "https://nit.com.au/feed/articles"),
            "pulsetasmania": ("Pulse Tasmania", "https://pulsetasmania.com.au/feed/"),
            "cityam": ("City AM", "https://www.cityam.com/feed/"),
            "nationcymru": ("Nation.Cymru", "https://nation.cymru/feed/"),
            "ukdefencejournal": ("UK Defence Journal", "https://ukdefencejournal.org.uk/feed/"),
        }
        feeds_by_slug = {slug: (name, url) for slug, name, url in rss_outlets.FEEDS}
        for slug, (name, url) in expected.items():
            with self.subTest(slug=slug):
                self.assertEqual(feeds_by_slug[slug], (name, url))
                rows, recorded = replay(slug)
                self.assertEqual(recorded.urls, [url])
                self.assertEqual(len(rows), 3)
                for row in rows:
                    self.assertTrue(row["headline"])
                    self.assertTrue(row["url"].startswith("https://"))
                    self.assertTrue(row["published"].startswith("2026-11-0"))
                    self.assertIsNone(row["text"])
        # The National Indigenous Times publishes Atom.
        rows, _ = replay("nit")
        self.assertEqual(rows[0]["headline"], "Young First Nations leader takes fight against invasive species to COP31")

    def test_wire_items_in_nation_cymru_and_courthouse_news_are_not_kept(self) -> None:
        for credit in ("PA", "PA Media", "PA Reporters", "Press Association", "pa political staff"):
            self.assertTrue(rss_outlets.third_party("nationcymru", {"creators": [credit]}), credit)
        for credit in ("Stephen Price", "Paul", "Pam Jones"):
            self.assertFalse(rss_outlets.third_party("nationcymru", {"creators": [credit]}), credit)
        for credit in ("Associated Press", "AP", "The Associated Press", "Reuters"):
            self.assertTrue(rss_outlets.third_party("courthousenews", {"creators": [credit]}), credit)
        self.assertFalse(rss_outlets.third_party("courthousenews", {"creators": ["Amanda Pampuro"]}))

    def test_the_asia_and_canada_feeds_drop_wire_and_partner_items(self) -> None:
        # Each fixture is the real feed of 2026-11-05 cut to three items; the middle one is not the
        # outlet's own work, marked the way that outlet marks it.
        self.assertEqual(
            [row["url"] for row in replay("asiatimes")[0]],
            [
                "https://asiatimes.com/2026/11/indias-22-billion-defense-push-cant-hide-its-missing-fighter-jets/",
                "https://asiatimes.com/2026/11/chinas-amphibious-surge-confronts-a-taiwan-invasions-hard-math/",
            ],
        )
        self.assertEqual(len(replay("businessworld")[0]), 2)
        self.assertEqual(
            [row["url"] for row in replay("phnompenhpost")[0]],
            ["https://phnompenhpost.com/national/cambodia-adopting-high-tech-surveillance-protection-for-mekong-dolphins/"],
        )
        self.assertEqual(len(replay("greaterkashmir")[0]), 2)
        self.assertEqual(len(replay("cheknews")[0]), 2)
        for slug in ("onlinekhabar", "newswirelk", "theisland", "cabinradio"):
            rows, recorded = replay(slug)
            self.assertEqual(len(rows), 3, slug)
            self.assertEqual(len(recorded.urls), 1, slug)
        republished = {"creators": [], "categories": [], "description": "Originally published by Pacific Forum, this article is republished with permission."}
        self.assertTrue(rss_outlets.third_party("asiatimes", republished))
        self.assertFalse(rss_outlets.third_party("asiatimes", {**republished, "description": "Sanae Takaichi has just completed her first year."}))
        self.assertTrue(rss_outlets.third_party("cheknews", {"creators": ["Brenna Owen, The Canadian Press"]}))
        self.assertFalse(rss_outlets.third_party("cheknews", {"creators": ["Mary Griffin"]}))

    def test_a_feed_has_one_page(self) -> None:
        rows, recorded = replay("globalvoices", page=1)
        self.assertEqual(rows, [])
        self.assertEqual(recorded.urls, [], "a later page must not make a request")

    def test_a_feed_fetches_no_article_page(self) -> None:
        self.assertFalse(hasattr(SOURCES["globalvoices"], "article_details"))

    def test_slugs_are_unique_and_registered(self) -> None:
        slugs = [slug for slug, _, _ in rss_outlets.FEEDS]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(FEED_OUTLETS, frozenset(slugs))
        for slug in slugs:
            self.assertIn(slug, SOURCES)
        self.assertNotIn(FEED_GROUP, SOURCES)

    def test_the_dropped_outlets_cannot_come_back(self) -> None:
        for dropped in DROPPED_OUTLETS:
            self.assertNotIn(dropped, SOURCES)
        for slug, name, url in rss_outlets.FEEDS:
            hostname = urlsplit(url).hostname or ""
            for host in (
                "theguardian.com", "nytimes.com", "npr.org", "independent.co.uk", "cbsnews.com",
                "abcnews.com", "go.com", "skynews.com", "sky.com", "aljazeera.com", "dw.com", "france24.com",
                "rfi.fr", "euronews.com", "africanews.com", "scmp.com", "indiatimes.com", "yna.co.kr",
                "focustaiwan.tw", "feedburner.com", "ansa.it", "rnz.co.nz", "cbc.ca", "abc.net.au", "un.org",
                "premiumtimesng.com", "sabcnews.com", "thediplomat.com", "bbc.co.uk", "bbci.co.uk", "pbs.org",
            ):
                # Whole host labels: sabcnews.com (South Africa's SABC) is not abcnews.com.
                self.assertFalse(
                    hostname == host or hostname.endswith("." + host), f"{slug} reads {host}, which Sam dropped"
                )

    def test_the_feeds_whose_terms_say_no_are_dropped_everywhere(self) -> None:
        # Sam dropped these on 2026-11-05: their own terms say no to a commercial site. Dropped means
        # the rail never sends a stored row of theirs either.
        said_no = (
            "npr", "independent", "cbsnews", "abcnews", "skynews", "aljazeera", "dw", "france24", "rfi",
            "euronews", "africanews", "scmp", "timesofindia", "yonhap", "focustaiwan", "ansa", "rnz", "cbc",
            "abcaustralia", "unnews", "premiumtimes", "sabcnews", "thediplomat",
        )
        self.assertEqual(len(said_no), 23)
        self.assertEqual(set(DROPPED_OUTLETS), {"guardian", "nyt", *said_no})
        for slug in said_no:
            self.assertIn(slug, DROPPED_OUTLETS)
            self.assertNotIn(slug, SOURCES)
            self.assertNotIn(slug, FEED_OUTLETS)
        # BBC and PBS say no as well, but their website scrapers still run until Sam decides on them.
        for slug in ("bbc", "pbs"):
            self.assertNotIn(slug, DROPPED_OUTLETS)
            self.assertIn(slug, SOURCES)
        self.assertEqual(len(rss_outlets.FEEDS), 26)

    def test_sources_the_paid_site_does_not_serve_are_not_fetched(self) -> None:
        urls = " ".join(url for _, _, url in rss_outlets.FEEDS)
        for host in ("aljazeera.com", "news.un.org", "t.me"):
            self.assertNotIn(host, urls)

    def test_a_real_feed_outlet_runs_through_the_scrape_loop_into_the_store(self) -> None:
        # The loop keys its work by source, so a source must be hashable. A fake source in the scrape
        # tests is; the first live run found that a SimpleNamespace was not.
        import contextlib
        import io
        import tempfile

        from newsfeed.scrape import StoreSink
        from newsfeed.store import Store

        recorded = RecordedFeed((FIXTURES / "globalvoices.xml").read_bytes())
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(common, "http_get", recorded):
            with Store(Path(folder) / "news.sqlite3") as store:
                sink = StoreSink(store)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    _, errors = common.scrape(
                        Path(folder), [(SOURCES["globalvoices"], "feed")], 8, 0.0, 0, True, sink=sink
                    )
                self.assertEqual(errors, {})
                self.assertEqual(
                    store.scalar("SELECT COUNT(*) FROM stories WHERE outlet = 'globalvoices' AND status = 'feed'"), 3
                )

    def test_media_credit_nested_in_media_content_is_kept(self) -> None:
        # A parser test on a saved document. NPR's feed left the schedule on 2026-11-05, but its file is
        # the one saved example of a media:credit nested inside media:content.
        document = (FIXTURES / "npr.xml").read_bytes()
        self.assertEqual(len(feeds.nested_media_text(document)), 3)


if __name__ == "__main__":
    unittest.main()
