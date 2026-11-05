"""Provenance's RSS feeds as outlets, replayed from saved real feeds. No network.

Fixtures in tests/fixtures/feeds/ are real responses from 2026-11-05, cut to three items, with
full-text elements (content:encoded) removed: this repo is public and holds no article text.
"""

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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
        rows, recorded = replay("npr")
        self.assertEqual(len(rows), 3)
        self.assertEqual(recorded.urls, ["https://feeds.npr.org/1004/rss.xml"])
        for row in rows:
            self.assertTrue(row["headline"])
            self.assertTrue(row["url"].startswith("https://"))
            self.assertRegex(row["published"], r"^\d{4}-\d\d-\d\dT")
            self.assertIsNone(row["text"])

    def test_rss1_rdf_feeds_are_read(self) -> None:
        # DW and AllAfrica publish RSS 1.0, whose items live in the RSS 1.0 namespace.
        for slug in ("dw", "allafrica"):
            with self.subTest(slug=slug):
                rows, _ = replay(slug)
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(row["headline"] and row["url"] for row in rows))
                self.assertTrue(all(row["published"] for row in rows), "dc:date is the date in RSS 1.0")

    def test_a_date_inside_cdata_is_read(self) -> None:
        # The Diplomat wraps pubDate in CDATA: <pubDate><![CDATA[Thu, 05 Nov 2026 ...]]></pubDate>.
        rows, _ = replay("thediplomat")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["published"] and row["published"].startswith("2026-") for row in rows))

    def test_a_feed_has_one_page(self) -> None:
        rows, recorded = replay("npr", page=1)
        self.assertEqual(rows, [])
        self.assertEqual(recorded.urls, [], "a later page must not make a request")

    def test_a_feed_fetches_no_article_page(self) -> None:
        self.assertFalse(hasattr(SOURCES["npr"], "article_details"))

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
            for host in ("theguardian.com", "nytimes.com"):
                self.assertNotIn(host, url, f"{slug} reads {host}, which Sam dropped")

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

        recorded = RecordedFeed((FIXTURES / "dw.xml").read_bytes())
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(common, "http_get", recorded):
            with Store(Path(folder) / "news.sqlite3") as store:
                sink = StoreSink(store)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    _, errors = common.scrape(Path(folder), [(SOURCES["dw"], "feed")], 8, 0.0, 0, True, sink=sink)
                self.assertEqual(errors, {})
                self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories WHERE outlet = 'dw' AND status = 'feed'"), 3)

    def test_media_credit_nested_in_media_content_is_kept(self) -> None:
        document = (FIXTURES / "npr.xml").read_bytes()
        self.assertEqual(len(feeds.nested_media_text(document)), 3)


if __name__ == "__main__":
    unittest.main()
