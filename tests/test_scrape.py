"""The scrape loop and the command line, with a fake source and with recorded Reuters responses."""

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from scraper import SOURCES, common, reuters  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def story(story_id: str) -> dict[str, Any]:
    row = common.empty_row()
    row.update(id=story_id, headline=f"Story {story_id}", url=f"https://example.com/{story_id}")
    row["categories"] = {"listing": ["label"]}
    return row


class FakeSource:
    """A source whose listing pages are given per section. It records every request."""

    def __init__(self, pages: dict[str, list[list[str]]], with_details: bool = True, name: str = "fake") -> None:
        self.NAME = name
        self.SECTIONS = {section: section for section in pages}
        self.pages = pages
        self.requests: list[tuple[str, int]] = []
        self.page_fetches: list[str] = []
        if with_details:
            self.article_details = self._article_details

    def list_page(self, section: str, page: int, size: int) -> list[dict[str, Any]]:
        self.requests.append((section, page))
        section_pages = self.pages[section]
        return [story(story_id) for story_id in section_pages[page]] if page < len(section_pages) else []

    def _article_details(self, url: str) -> tuple[str, dict[str, Any]]:
        self.page_fetches.append(url)
        return f"Body of {url}", {"page": ["tag"]}


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_scrape(jobs: list[tuple[Any, str]], **options: Any) -> tuple[int, list[dict[str, Any]]]:
    """Every row saved, read back from each section's file in job order, and the total saved."""
    settings = {"size": 8, "delay": 0, "max_pages": 0, "include_text": True, **options}
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            totals, errors = common.scrape(Path(directory), jobs, **settings)
        if errors:
            raise AssertionError(f"The scrape reported errors: {errors}")
        rows = []
        for source, section in dict.fromkeys(jobs):
            rows += read_rows(Path(directory) / source.NAME / f"{section}.jsonl")
    return sum(totals.values()), rows


class WaitingSource(FakeSource):
    """A fake outlet whose first listing page waits for the other outlets on the same barrier."""

    def __init__(self, name: str, barrier: threading.Barrier) -> None:
        super().__init__({"africa": [[f"{name}-1"]]}, name=name)
        self.barrier = barrier
        self.list_threads: set[threading.Thread] = set()
        self.close_thread: threading.Thread | None = None

    def list_page(self, section: str, page: int, size: int) -> list[dict[str, Any]]:
        self.list_threads.add(threading.current_thread())
        if page == 0:
            self.barrier.wait()
        return super().list_page(section, page, size)

    def close(self) -> None:
        self.close_thread = threading.current_thread()


class FailingSource(FakeSource):
    def list_page(self, section: str, page: int, size: int) -> list[dict[str, Any]]:
        raise RuntimeError("Blocked by the outlet.")


class OutletThreadTests(unittest.TestCase):
    def scrape_to(self, directory: str, jobs: list[tuple[Any, str]]) -> tuple[dict[str, int], dict[str, str]]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return common.scrape(Path(directory), jobs, size=8, delay=0, max_pages=0, include_text=True)

    def test_outlets_run_at_the_same_time_into_their_own_folders(self) -> None:
        # Each outlet's first page waits for the other's. One outlet after the other breaks the barrier.
        barrier = threading.Barrier(2, timeout=5)
        one, two = WaitingSource("one", barrier), WaitingSource("two", barrier)
        with tempfile.TemporaryDirectory() as directory:
            totals, errors = self.scrape_to(directory, [(one, "africa"), (two, "africa")])
            self.assertEqual((totals, errors), ({"one": 1, "two": 1}, {}))
            for name in ("one", "two"):
                rows = read_rows(Path(directory) / name / "africa.jsonl")
                self.assertEqual([(row["outlet"], row["id"]) for row in rows], [(name, f"{name}-1")])

    def test_close_runs_in_the_thread_that_scraped_the_outlet(self) -> None:
        source = WaitingSource("one", threading.Barrier(1))
        with tempfile.TemporaryDirectory() as directory:
            self.scrape_to(directory, [(source, "africa")])
        self.assertEqual(source.list_threads, {source.close_thread})
        self.assertIsNot(source.close_thread, threading.main_thread())

    def test_one_outlet_failing_does_not_stop_the_others(self) -> None:
        broken = FailingSource({"africa": []}, name="broken")
        working = FakeSource({"africa": [["a1"], ["a2"]]}, name="working")
        with tempfile.TemporaryDirectory() as directory:
            totals, errors = self.scrape_to(directory, [(broken, "africa"), (working, "africa")])
            self.assertEqual(len(read_rows(Path(directory) / "working" / "africa.jsonl")), 2)
        self.assertEqual(errors, {"broken": "Blocked by the outlet."})
        self.assertEqual(totals, {"broken": 0, "working": 2})


class ScrapeLoopTests(unittest.TestCase):
    def test_each_row_records_its_outlet_and_section(self) -> None:
        source = FakeSource({"africa": [["a1", "a2"]], "europe": [["e1"]]})
        total, rows = run_scrape([(source, "africa"), (source, "europe")])
        self.assertEqual(total, 3)
        self.assertEqual([(row["outlet"], row["section"]) for row in rows], [("fake", "africa")] * 2 + [("fake", "europe")])

    def test_page_text_and_categories_are_merged_into_the_row(self) -> None:
        source = FakeSource({"africa": [["a1"]]})
        _, rows = run_scrape([(source, "africa")])
        self.assertEqual(rows[0]["text"], "Body of https://example.com/a1")
        self.assertEqual(rows[0]["categories"], {"listing": ["label"], "page": ["tag"]})

    def test_a_story_in_two_sections_gets_a_row_in_each_and_one_page_fetch(self) -> None:
        source = FakeSource({"africa": [["s1", "s2"]], "europe": [["s1", "s2"]]})
        total, rows = run_scrape([(source, "africa"), (source, "europe")])
        self.assertEqual(total, 4)
        self.assertEqual(len(source.page_fetches), 2)
        self.assertEqual(rows[2]["text"], rows[0]["text"])

    def test_a_page_of_stories_from_an_earlier_section_does_not_end_the_next_section(self) -> None:
        source = FakeSource({"africa": [["s1", "s2"]], "europe": [["s1", "s2"], ["e1"]]})
        total, _ = run_scrape([(source, "africa"), (source, "europe")])
        self.assertEqual(total, 5)
        self.assertIn(("europe", 1), source.requests)

    def test_a_section_ends_when_its_page_repeats_or_is_empty(self) -> None:
        source = FakeSource({"africa": [["s1"], ["s1"], ["s2"]], "europe": [["e1"]]})
        total, _ = run_scrape([(source, "africa"), (source, "europe")])
        self.assertEqual(total, 2)
        self.assertEqual(source.requests, [("africa", 0), ("africa", 1), ("europe", 0), ("europe", 1)])

    def test_max_pages_applies_to_each_section(self) -> None:
        source = FakeSource({"africa": [["a1"], ["a2"]], "europe": [["e1"], ["e2"]]})
        run_scrape([(source, "africa"), (source, "europe")], max_pages=1)
        self.assertEqual(source.requests, [("africa", 0), ("europe", 0)])

    def test_page_fields_fill_only_what_the_listing_left_empty(self) -> None:
        source = FakeSource({"africa": [["a1"]]})
        source.article_details = lambda url: (
            "Body",
            {},
            {"headline": "Page headline", "published": "2026-09-14", "authors": ["A Writer"], "post_id": 7},
        )
        _, rows = run_scrape([(source, "africa")])
        row = rows[0]
        self.assertEqual(row["headline"], "Story a1")  # The listing had one; the page does not overwrite it.
        self.assertEqual((row["published"], row["authors"], row["post_id"]), ("2026-09-14", ["A Writer"], 7))

    def test_no_page_fetch_without_include_text_or_without_details(self) -> None:
        source = FakeSource({"africa": [["a1"]]})
        _, rows = run_scrape([(source, "africa")], include_text=False)
        self.assertEqual(source.page_fetches, [])
        self.assertIsNone(rows[0]["text"])
        feed_only = FakeSource({"africa": [["a1"]]}, with_details=False)
        total, _ = run_scrape([(feed_only, "africa")])
        self.assertEqual(total, 1)


class ReutersReplayTests(unittest.TestCase):
    """Recorded Reuters responses through the real source module and the shared loop."""

    def test_output_merges_listing_and_page_categories(self) -> None:
        section = json.loads((FIXTURES / "reuters" / "section_africa.json").read_text(encoding="utf-8"))
        page = reuters.ArticlePageParser()
        page.feed((FIXTURES / "reuters" / "article_dangote_ipo.html").read_text(encoding="utf-8"))
        fake_fetch_page = lambda path, offset, request_id, size: section if offset == 0 else {}  # noqa: E731
        with (
            mock.patch.object(reuters, "fetch_page", fake_fetch_page),
            mock.patch.object(reuters, "fetch_article_page", lambda url: page),
        ):
            total, rows = run_scrape([(reuters, "africa")])
        self.assertEqual(total, 8)
        first = rows[0]
        self.assertEqual((first["outlet"], first["section"], first["section_path"]), ("reuters", "africa", "/world/africa/"))
        self.assertEqual(first["categories"]["section"], ["Business", "Energy"])
        self.assertEqual(first["categories"]["place"], "LAGOS")
        self.assertIn("Refining", first["categories"]["topics"])
        self.assertIn("Nigerian billionaire Aliko Dangote", first["text"])

    def test_every_source_row_has_the_shared_fields(self) -> None:
        section = json.loads((FIXTURES / "reuters" / "section_africa.json").read_text(encoding="utf-8"))
        with mock.patch.object(reuters, "fetch_page", lambda *args: section):
            rows = reuters.list_page("africa", 0, 8)
        for row in rows:
            self.assertLessEqual(set(common.empty_row()), set(row))


class CommandLineTests(unittest.TestCase):
    def test_default_is_every_section_of_every_source(self) -> None:
        jobs = main.build_jobs(main.parse_args([]))
        expected = [(source, section) for source in SOURCES.values() for section in source.SECTIONS]
        self.assertEqual(jobs, expected)

    def test_sources_limit_the_run(self) -> None:
        jobs = main.build_jobs(main.parse_args(["--sources", "reuters"]))
        self.assertEqual({source.NAME for source, _ in jobs}, {"reuters"})

    def test_sections_are_chosen_as_source_and_section(self) -> None:
        jobs = main.build_jobs(main.parse_args(["--sections", "reuters:europe", "reuters:japan", "reuters:europe"]))
        self.assertEqual(jobs, [(reuters, "europe"), (reuters, "japan")])

    def test_unknown_section_is_rejected(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main.parse_args(["--sections", "reuters:antarctica"])


class RssParserTests(unittest.TestCase):
    def test_parses_items_with_taxonomy_creators_and_media(self) -> None:
        document = b"""<?xml version="1.0"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Headline</title><link>https://example.com/a</link><guid>g1</guid>
<description>&lt;p&gt;Summary &amp;amp; more&lt;/p&gt;</description><pubDate>Mon, 14 Sep 2026 09:16:15 +0000</pubDate>
<dc:creator>A Writer</dc:creator><dc:creator>B Writer</dc:creator>
<category domain="http://www.nytimes.com/namespaces/keywords/nyt_geo">Nigeria</category><category>Energy</category>
<media:content url="https://example.com/a.jpg" width="1024"/></item></channel></rss>"""
        (item,) = common.parse_rss(document)
        self.assertEqual(item["title"], "Headline")
        self.assertEqual(item["description"], "Summary & more")
        self.assertEqual(item["published"], "2026-09-14T09:16:15+00:00")
        self.assertEqual(item["creators"], ["A Writer", "B Writer"])
        self.assertEqual(
            item["categories"],
            [{"domain": "http://www.nytimes.com/namespaces/keywords/nyt_geo", "name": "Nigeria"}, {"domain": None, "name": "Energy"}],
        )
        self.assertEqual(item["media"][0]["url"], "https://example.com/a.jpg")


if __name__ == "__main__":
    unittest.main()
