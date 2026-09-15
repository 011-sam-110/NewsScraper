"""Feed-only sources (NYT) replayed from recorded RSS feeds. No network."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import common, feeds, nyt  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FEED_FIXTURES = {
    nyt: ("americas", FIXTURES / "nyt" / "Americas.xml"),
}


class RecordedFeed:
    """Stands in for common.http_get: serves one recorded document and records every URL asked for."""

    def __init__(self, document: bytes) -> None:
        self.document = document
        self.urls: list[str] = []

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> bytes:
        self.urls.append(url)
        return self.document


def replay(source: Any, section: str | None = None, page: int = 0) -> tuple[list[dict[str, Any]], RecordedFeed]:
    default_section, path = FEED_FIXTURES[source]
    recorded = RecordedFeed(path.read_bytes())
    with mock.patch.object(common, "http_get", recorded):
        rows = source.list_page(section or default_section, page, 8)
    return rows, recorded


class SharedFeedSourceTests(unittest.TestCase):
    def test_every_row_has_the_shared_fields_and_no_text(self) -> None:
        for source in FEED_FIXTURES:
            with self.subTest(source=source.NAME):
                rows, recorded = replay(source)
                self.assertEqual(len(rows), 5)
                self.assertEqual(recorded.urls, [source.SECTIONS[FEED_FIXTURES[source][0]]])
                for row in rows:
                    self.assertLessEqual(set(common.empty_row()), set(row))
                    self.assertIsNone(row["text"])
                    self.assertTrue(row["id"] and row["url"] and row["headline"] and row["published"])

    def test_page_one_returns_nothing_and_requests_nothing(self) -> None:
        for source in FEED_FIXTURES:
            with self.subTest(source=source.NAME):
                rows, recorded = replay(source, page=1)
                self.assertEqual(rows, [])
                self.assertEqual(recorded.urls, [])

    def test_modules_do_not_fetch_article_pages(self) -> None:
        for source in FEED_FIXTURES:
            with self.subTest(source=source.NAME):
                self.assertFalse(hasattr(source, "article_details"))

    def test_scrape_with_text_requests_only_the_feed(self) -> None:
        for source, (section, path) in FEED_FIXTURES.items():
            with self.subTest(source=source.NAME):
                recorded = RecordedFeed(path.read_bytes())
                with tempfile.TemporaryDirectory() as directory, mock.patch.object(common, "http_get", recorded):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        totals, errors = common.scrape(
                            Path(directory), [(source, section)], size=8, delay=0, max_pages=0, include_text=True
                        )
                    output = Path(directory) / source.NAME / f"{section}.jsonl"
                    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
                self.assertEqual((totals, errors), ({source.NAME: 5}, {}))
                self.assertEqual(recorded.urls, [source.SECTIONS[section]])
                self.assertEqual({(row["outlet"], row["section"]) for row in rows}, {(source.NAME, section)})

    def test_feed_errors_name_the_outlet(self) -> None:
        def failing_get(url: str, *args: Any, **kwargs: Any) -> bytes:
            raise common.FetchError(f"HTTP 503 from {url}", 503)

        for source, (section, _) in FEED_FIXTURES.items():
            with self.subTest(source=source.NAME), mock.patch.object(common, "http_get", failing_get):
                with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                    source.list_page(section, 0, 8)


class HelperTests(unittest.TestCase):
    def test_url_section_is_the_path_before_the_date_or_slug(self) -> None:
        cases = {
            "https://www.nytimes.com/2026/09/10/arts/design/bayeux-tapestry-replicas.html": ["arts", "design"],
            "https://www.nytimes.com/video/world/asia/100000011129754/tracing-the-path.html": ["video", "world", "asia"],
            "https://example.com/world/middle-east/oil-supplies-0cd316ad?mod=rss": ["world", "middle-east"],
            "https://example.com/opinion/aluminum-tariffs-8f646d1e": ["opinion"],
            "https://example.com/middle-east-and-africa/2026/09/13/slug": ["middle-east-and-africa"],
            "https://example.com/interactive/2026-election-tracker": ["interactive"],
            "https://example.com/interactive/2026-war-tracker/": ["interactive"],
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(feeds.url_section(url), expected)

    def test_split_authors(self) -> None:
        self.assertEqual(feeds.split_authors(["Zane Irwin and Prior Beharry"]), ["Zane Irwin", "Prior Beharry"])
        self.assertEqual(
            feeds.split_authors(["Edward Wong, Genevieve Glatsky and Annie Correal"]),
            ["Edward Wong", "Genevieve Glatsky", "Annie Correal"],
        )
        self.assertEqual(feeds.split_authors(["Robert Siegel, E.J. Dionne Jr., Carlos Lozada and Vishakha Darbha"]),
                         ["Robert Siegel", "E.J. Dionne Jr.", "Carlos Lozada", "Vishakha Darbha"])
        self.assertEqual(feeds.split_authors(["Hannah Alberstadt", "and", "Miranda Marquit"]), ["Hannah Alberstadt", "Miranda Marquit"])
        self.assertEqual(feeds.split_authors([]), [])


class NytTests(unittest.TestCase):
    def test_field_mapping(self) -> None:
        rows, _ = replay(nyt)
        first = rows[0]
        url = "https://www.nytimes.com/2026/09/14/world/americas/trinidad-assassination-plot-arrests.html"
        self.assertEqual((first["id"], first["url"], first["guid"]), (url, url, url))
        self.assertEqual(first["headline"], "She Bashed the Government on a Private Call. Then She Was Detained for Months")
        self.assertTrue(first["description"])
        self.assertEqual(first["published"][:10], "2026-09-14")
        self.assertTrue(first["thumbnail"].startswith("https://static01.nyt.com/"))
        self.assertEqual(first["media"][0]["url"], first["thumbnail"])
        self.assertEqual(first["media_credit"], "Prior Beharry for The New York Times")
        self.assertEqual(first["url_section"], "world/americas")
        self.assertEqual(first["feed_url"], nyt.SECTIONS["americas"])

    def test_authors_are_split_from_the_one_creator_string(self) -> None:
        rows, _ = replay(nyt)
        self.assertEqual(rows[0]["authors"], ["Zane Irwin", "Prior Beharry"])
        self.assertEqual(rows[0]["author"], "Zane Irwin")
        self.assertEqual(rows[3]["authors"], ["Edward Wong", "Genevieve Glatsky", "Annie Correal"])
        self.assertEqual(rows[1]["authors"], ["Alan Yuhas"])

    def test_categories_are_grouped_by_nyt_keyword_domain(self) -> None:
        rows, _ = replay(nyt)
        chimu = rows[1]["categories"]
        self.assertEqual(chimu["subjects"], ["Archaeology and Anthropology", "Incas", "Tombs and Tombstones", "Arts and Antiquities Looting"])
        self.assertEqual(chimu["places"], ["Chan Chan (Peru)", "Peru"])
        self.assertEqual(chimu["other"], ["Chimu (culture)"])
        self.assertEqual(chimu["url_section"], ["world", "americas"])
        self.assertEqual(rows[2]["categories"]["organizations"], ["British Museum", "University of North Georgia"])
        self.assertEqual(rows[3]["categories"]["people"], ["Noboa, Daniel", "Rubio, Marco"])
        self.assertIn("The Tempest (Play)", rows[4]["categories"]["titles"])
        self.assertEqual(rows[4]["url_section"], "theater")

    def test_item_without_media_has_no_thumbnail(self) -> None:
        rows, _ = replay(nyt)
        self.assertIsNone(rows[2]["thumbnail"])
        self.assertEqual(rows[2]["media"], [])

    def test_sports_feed_is_not_pinned(self) -> None:
        self.assertNotIn("sports", nyt.SECTIONS)
        self.assertTrue(all(url.startswith("https://rss.nytimes.com/services/xml/rss/nyt/") for url in nyt.SECTIONS.values()))


if __name__ == "__main__":
    unittest.main()
