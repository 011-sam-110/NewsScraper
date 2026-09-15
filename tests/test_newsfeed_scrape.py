"""The store sink: what it stores, what it refetches, and where a section stops.

The sink is driven through the real scraper.common.scrape loop with a fake source, so these tests
cover the change to scrape_source as well as the sink itself.
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import newsfeed.scrape as scrape_stage  # noqa: E402
from newsfeed.scrape import StoreSink  # noqa: E402
from newsfeed.settings import load  # noqa: E402
from newsfeed.store import STATUS_EXTRACTED, STATUS_SCRAPED, Store  # noqa: E402
from scraper import common  # noqa: E402


class FakeSource:
    """Listing pages given per section, and an article page per URL. Records every request."""

    def __init__(self, name: str, pages: dict[str, list[list[dict[str, Any]]]],
                 texts: dict[str, str] | None = None) -> None:
        self.NAME = name
        self.SECTIONS = {section: section for section in pages}
        self.pages = pages
        self.texts = texts or {}
        self.listing_requests: list[tuple[str, int]] = []
        self.page_fetches: list[str] = []
        self.closed = 0

    def list_page(self, section: str, page: int, size: int) -> list[dict[str, Any]]:
        self.listing_requests.append((section, page))
        pages = self.pages[section]
        return [dict(row) for row in pages[page]] if page < len(pages) else []

    def article_details(self, url: str) -> tuple[str | None, dict[str, Any]]:
        self.page_fetches.append(url)
        return self.texts.get(url), {"page": ["label"]}

    def close(self) -> None:
        self.closed += 1


def row(story_id: str, url: str, headline: str | None = None, **extra: Any) -> dict[str, Any]:
    made = common.empty_row()
    made.update(id=story_id, url=url, headline=headline or f"Story {story_id}",
                published="2026-09-14T10:00:00Z")
    made.update(extra)
    return made


class SinkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "news.sqlite3"
        self.output = Path(tempfile.mkdtemp())

    def run_scrape(self, source: FakeSource, sections: list[str] | None = None,
                   max_pages: int = 0, include_text: bool = True) -> StoreSink:
        """One scrape through the real loop, with a fresh sink over the same store file."""
        with Store(self.path) as store:
            sink = StoreSink(store)
            jobs = [(source, section) for section in (sections or list(source.SECTIONS))]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                totals, errors = common.scrape(self.output, jobs, 8, 0.0, max_pages, include_text, sink=sink)
            self.assertEqual(errors, {})
            self.sink_totals = totals
            return sink

    def store(self) -> Store:
        return Store(self.path)


class StoringTests(SinkTestCase):
    def test_a_first_run_stores_stories_sections_aliases_and_text(self) -> None:
        source = FakeSource(
            "bbc",
            {"world": [[row("urn:bbc:asset:1", "https://www.bbc.co.uk/news/articles/a1")]]},
            texts={"https://www.bbc.co.uk/news/articles/a1": "A man was hurt near the bridge."},
        )
        sink = self.run_scrape(source)
        self.assertEqual(sink.new_stories["bbc"], 1)
        with self.store() as store:
            story = store.one("SELECT * FROM stories")
            self.assertEqual(story["outlet"], "bbc")
            self.assertEqual(story["primary_alias"], "urn:bbc:asset:1")
            self.assertEqual(story["url"], "https://www.bbc.co.uk/news/articles/a1")
            self.assertEqual(story["published"], "2026-09-14T10:00:00Z")
            self.assertEqual(story["status"], STATUS_SCRAPED)
            self.assertIsNotNone(story["text_hash"])
            self.assertIsNotNone(story["text_fetched_at"])
            self.assertEqual(
                sorted(r["alias"] for r in store.query("SELECT alias FROM story_aliases")),
                ["https://www.bbc.co.uk/news/articles/a1", "urn:bbc:asset:1"],
            )
            self.assertEqual(store.query("SELECT section FROM story_sections")[0]["section"], "world")
            self.assertEqual(
                store.one("SELECT text FROM story_texts")["text"], "A man was hurt near the bridge."
            )

    def test_a_second_run_stores_no_new_story(self) -> None:
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", "https://www.bbc.co.uk/news/articles/a1")]]})
        self.run_scrape(source)
        second = self.run_scrape(source)
        self.assertEqual(second.new_stories["bbc"], 0)
        with self.store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 1)

    def test_one_story_in_two_sections_is_one_story_with_two_section_labels(self) -> None:
        shared = row("urn:bbc:asset:1", "https://www.bbc.co.uk/news/articles/a1")
        source = FakeSource("bbc", {"world": [[shared]], "africa": [[shared]]})
        sink = self.run_scrape(source)
        self.assertEqual(sink.new_stories["bbc"], 1)
        self.assertEqual(sink.new_sections["bbc"], 1)
        with self.store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 1)
            self.assertEqual(
                sorted(r["section"] for r in store.query("SELECT section FROM story_sections")),
                ["africa", "world"],
            )

    def test_a_row_with_no_id_and_no_url_is_counted_but_not_stored(self) -> None:
        nameless = common.empty_row()
        nameless.update(headline="No id, no link")
        source = FakeSource("bbc", {"world": [[nameless]]})
        sink = self.run_scrape(source)
        self.assertEqual(sink.rows_seen["bbc"], 1)
        self.assertEqual(sink.new_stories["bbc"], 0)
        with self.store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 0)

    def test_format_flags_and_categories_are_stored(self) -> None:
        opinion = row("g1", "https://www.theguardian.com/commentisfree/2026/sep/14/x")
        opinion["categories"] = {"tones": ["Comment"]}
        source = FakeSource("guardian", {"world": [[opinion]]})
        self.run_scrape(source)
        with self.store() as store:
            story = store.one("SELECT * FROM stories")
            self.assertEqual(json.loads(story["format_flags"]), ["opinion"])
            self.assertIn("tones", json.loads(story["categories"]))

    def test_no_json_lines_tree_is_written_when_a_sink_stores_the_rows(self) -> None:
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", "https://www.bbc.co.uk/news/a1")]]})
        self.run_scrape(source)
        self.assertEqual(list(self.output.rglob("*.jsonl")), [])


class SectionStopTests(SinkTestCase):
    def test_a_section_stops_at_the_first_page_holding_no_new_story(self) -> None:
        pages = [
            [row("1", "https://example.com/1")],
            [row("2", "https://example.com/2")],
            [row("3", "https://example.com/3")],
        ]
        source = FakeSource("bbc", {"world": pages})
        self.run_scrape(source)
        self.assertEqual(source.listing_requests, [("world", 0), ("world", 1), ("world", 2), ("world", 3)])

        source.listing_requests.clear()
        second = self.run_scrape(source)
        # Page 0 holds only stories the store already has, so the section stops there.
        self.assertEqual(source.listing_requests, [("world", 0)])
        self.assertEqual(second.new_stories["bbc"], 0)

    def test_a_new_story_on_page_0_still_stops_after_a_known_page_1(self) -> None:
        pages = [[row("1", "https://example.com/1")], [row("2", "https://example.com/2")]]
        source = FakeSource("bbc", {"world": pages})
        self.run_scrape(source)

        pages[0] = [row("9", "https://example.com/9"), row("1", "https://example.com/1")]
        source.listing_requests.clear()
        sink = self.run_scrape(source)
        self.assertEqual(sink.new_stories["bbc"], 1)
        self.assertEqual(source.listing_requests, [("world", 0), ("world", 1)])

    def test_without_a_sink_the_section_still_walks_to_the_end(self) -> None:
        pages = [[row("1", "https://example.com/1")], [row("2", "https://example.com/2")]]
        source = FakeSource("bbc", {"world": pages})
        totals = {"bbc": 0}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            common.scrape_source(source, ["world"], self.output / "bbc", 8, 0.0, 0, True, totals,
                                 threading.Event(), None)
            common.scrape_source(source, ["world"], self.output / "bbc", 8, 0.0, 0, True, totals,
                                 threading.Event(), None)
        # Four listing requests each time: two pages and the empty page past the end.
        self.assertEqual(len(source.listing_requests), 6)
        self.assertTrue((self.output / "bbc" / "world.jsonl").exists())


class ArticlePageTests(SinkTestCase):
    def test_an_article_page_is_fetched_once_and_not_again(self) -> None:
        url = "https://www.bbc.co.uk/news/articles/a1"
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", url)]]}, texts={url: "Body text."})
        self.run_scrape(source)
        self.assertEqual(source.page_fetches, [url])
        source.page_fetches.clear()
        self.run_scrape(source)
        self.assertEqual(source.page_fetches, [])

    def test_a_page_that_gives_nothing_is_not_fetched_every_run(self) -> None:
        url = "https://www.bbc.co.uk/news/articles/a1"
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", url)]]}, texts={})
        self.run_scrape(source)
        self.assertEqual(source.page_fetches, [url])
        source.page_fetches.clear()
        self.run_scrape(source)
        self.assertEqual(source.page_fetches, [])
        with self.store() as store:
            story = store.one("SELECT * FROM stories")
            self.assertIsNone(story["text_hash"])
            self.assertIsNotNone(story["text_fetched_at"])

    def test_a_changed_listing_update_time_refetches_the_page(self) -> None:
        url = "https://www.bbc.co.uk/news/articles/a1"
        first = row("urn:bbc:asset:1", url, updated="2026-09-14T10:00:00Z")
        source = FakeSource("bbc", {"world": [[first]]}, texts={url: "First body."})
        self.run_scrape(source)
        source.page_fetches.clear()

        source.pages["world"] = [[row("urn:bbc:asset:1", url, updated="2026-09-14T12:00:00Z")]]
        source.texts[url] = "Second body, corrected."
        self.run_scrape(source)
        self.assertEqual(source.page_fetches, [url])
        with self.store() as store:
            self.assertEqual(store.one("SELECT text FROM story_texts")["text"], "Second body, corrected.")
            self.assertEqual(store.one("SELECT updated FROM stories")["updated"], "2026-09-14T12:00:00Z")

    def test_no_include_text_never_asks_for_a_page(self) -> None:
        url = "https://www.bbc.co.uk/news/articles/a1"
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", url)]]}, texts={url: "Body."})
        self.run_scrape(source, include_text=False)
        self.assertEqual(source.page_fetches, [])


class ChangeTests(SinkTestCase):
    def test_a_new_headline_sends_an_extracted_story_back_to_scraped(self) -> None:
        url = "https://www.bbc.co.uk/news/articles/a1"
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", url, headline="First wording")]]})
        self.run_scrape(source)
        with self.store() as store:
            store.release_stories([store.scalar("SELECT story_id FROM stories")], status=STATUS_EXTRACTED)

        source.pages["world"] = [[row("urn:bbc:asset:1", url, headline="Corrected wording")]]
        self.run_scrape(source)
        with self.store() as store:
            story = store.one("SELECT * FROM stories")
            self.assertEqual(story["headline"], "Corrected wording")
            self.assertEqual(story["status"], STATUS_SCRAPED)

    def test_an_unchanged_story_stays_where_the_pipeline_left_it(self) -> None:
        url = "https://www.bbc.co.uk/news/articles/a1"
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", url)]]})
        self.run_scrape(source)
        with self.store() as store:
            store.release_stories([store.scalar("SELECT story_id FROM stories")], status=STATUS_EXTRACTED)
        self.run_scrape(source)
        with self.store() as store:
            self.assertEqual(store.one("SELECT status FROM stories")["status"], STATUS_EXTRACTED)

    def test_a_listing_row_does_not_wipe_a_field_the_article_page_supplied(self) -> None:
        url = "https://www.theguardian.com/world/2026/sep/14/x"
        full = row("g1", url, description="A full description")
        source = FakeSource("guardian", {"world": [[full]]})
        self.run_scrape(source)

        source.pages["world"] = [[row("g1", url, description=None)]]
        self.run_scrape(source)
        with self.store() as store:
            self.assertEqual(store.one("SELECT description FROM stories")["description"], "A full description")

    def test_a_short_url_seen_later_joins_the_same_story(self) -> None:
        url = "https://www.theguardian.com/world/2026/sep/14/x"
        source = FakeSource("guardian", {"world": [[row("g1", url)]]})
        self.run_scrape(source)

        source.pages["world"] = [[row("g1", url, short_url="https://gu.com/p/abc")]]
        second = self.run_scrape(source)
        self.assertEqual(second.new_stories["guardian"], 0)
        with self.store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 1)
            self.assertEqual(store.find_story("guardian", ["https://gu.com/p/abc"]),
                             store.scalar("SELECT story_id FROM stories"))
            # The primary alias never changes, so an id minted from it stays stable.
            self.assertEqual(store.one("SELECT primary_alias FROM stories")["primary_alias"], "g1")


class ParallelOutletTests(SinkTestCase):
    def test_five_outlets_write_at_once(self) -> None:
        sources = [
            FakeSource(name, {"world": [[row(f"{name}-1", f"https://{name}.example.com/1")]]})
            for name in ("reuters", "bbc", "guardian", "pbs", "nyt")
        ]
        with Store(self.path) as store:
            sink = StoreSink(store)
            jobs = [(source, "world") for source in sources]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                _, errors = common.scrape(self.output, jobs, 8, 0.0, 0, True, sink=sink)
        self.assertEqual(errors, {})
        self.assertEqual(sum(sink.new_stories.values()), 5)
        with self.store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 5)
        self.assertTrue(all(source.closed == 1 for source in sources))


class JsonLinesRunTests(SinkTestCase):
    """--jsonl-dir is a debugging run: it must behave like main.py and not touch the store."""

    def test_it_writes_json_lines_and_leaves_the_store_alone(self) -> None:
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", "https://www.bbc.co.uk/news/a1")]]})
        settings = load({"NEWSFEED_DATA_DIR": str(self.path.parent)})
        args = argparse.Namespace(
            sources=["bbc"], sections=None, size=8, delay=0.0, max_pages=0,
            include_text=True, jsonl_dir=self.output,
        )
        with mock.patch.dict(scrape_stage.SOURCES, {"bbc": source}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scrape_stage.run(args, settings), 0)
        self.assertEqual([p.name for p in self.output.rglob("*.jsonl")], ["world.jsonl"])
        self.assertFalse(settings.news_db.exists())

    def test_without_it_the_store_is_written_and_no_json_lines_appear(self) -> None:
        source = FakeSource("bbc", {"world": [[row("urn:bbc:asset:1", "https://www.bbc.co.uk/news/a1")]]})
        settings = load({"NEWSFEED_DATA_DIR": str(self.path.parent)})
        args = argparse.Namespace(
            sources=["bbc"], sections=None, size=8, delay=0.0, max_pages=0,
            include_text=True, jsonl_dir=None,
        )
        with mock.patch.dict(scrape_stage.SOURCES, {"bbc": source}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scrape_stage.run(args, settings), 0)
        self.assertTrue(settings.news_db.exists())
        with Store(settings.news_db) as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 1)
            self.assertIsNotNone(store.data_as_of())
            self.assertEqual(
                store.one("SELECT consecutive_failures FROM outlet_health WHERE outlet='bbc'")[0], 0
            )
        self.assertEqual(list(self.output.rglob("*.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
