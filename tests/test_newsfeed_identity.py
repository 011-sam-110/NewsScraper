"""Story identity: canonical URLs, aliases, UTC times and format flags, against real row shapes."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import identity  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class CanonicalUrlTests(unittest.TestCase):
    def test_lower_cases_scheme_and_host_and_drops_query_and_fragment(self) -> None:
        self.assertEqual(
            identity.canonical_url("HTTPS://WWW.BBC.CO.UK/news/articles/abc?utm=1#top"),
            "https://www.bbc.co.uk/news/articles/abc",
        )

    def test_drops_a_trailing_slash_but_keeps_the_path(self) -> None:
        self.assertEqual(
            identity.canonical_url("https://www.reuters.com/world/africa/story-2026-09-14/"),
            "https://www.reuters.com/world/africa/story-2026-09-14",
        )

    def test_drops_a_default_port_and_keeps_another(self) -> None:
        self.assertEqual(identity.canonical_url("https://example.com:443/a"), "https://example.com/a")
        self.assertEqual(identity.canonical_url("http://example.com:8080/a"), "http://example.com:8080/a")

    def test_two_links_to_one_story_give_one_alias(self) -> None:
        self.assertEqual(
            identity.canonical_url("https://www.theguardian.com/world/2026/sep/14/x?page=with:block-1"),
            identity.canonical_url("HTTPS://www.theguardian.com/world/2026/sep/14/x/"),
        )

    def test_leaves_alone_what_it_cannot_parse(self) -> None:
        self.assertIsNone(identity.canonical_url(None))
        self.assertIsNone(identity.canonical_url("  "))
        self.assertEqual(identity.canonical_url("/newshour/world/x"), "/newshour/world/x")


class AliasTests(unittest.TestCase):
    def test_each_outlet_uses_the_ids_the_design_lists(self) -> None:
        cases = {
            "reuters": ({"id": "ABCDEF", "url": "https://www.reuters.com/world/x/"},
                        ["ABCDEF", "https://www.reuters.com/world/x"]),
            "bbc": ({"id": "urn:bbc:optimo:asset:cx2kz4v4e6xo", "url": "https://www.bbc.co.uk/news/articles/c1"},
                    ["urn:bbc:optimo:asset:cx2kz4v4e6xo", "https://www.bbc.co.uk/news/articles/c1"]),
            "guardian": ({"id": "world/2026/sep/14/x", "short_url": "https://gu.com/p/abc",
                          "url": "https://www.theguardian.com/world/2026/sep/14/x"},
                         ["https://gu.com/p/abc", "world/2026/sep/14/x",
                          "https://www.theguardian.com/world/2026/sep/14/x"]),
            "pbs": ({"id": "/newshour/world/x", "post_id": "554954",
                     "url": "https://www.pbs.org/newshour/world/x"},
                    ["/newshour/world/x", "554954", "https://www.pbs.org/newshour/world/x"]),
            "nyt": ({"guid": "nyt://article/abc", "url": "https://www.nytimes.com/2026/09/14/world/x.html"},
                    ["nyt://article/abc", "https://www.nytimes.com/2026/09/14/world/x.html"]),
        }
        for outlet, (row, expected) in cases.items():
            with self.subTest(outlet=outlet):
                self.assertEqual(identity.aliases(outlet, row), expected)

    def test_a_missing_id_still_leaves_the_url_to_join_on(self) -> None:
        self.assertEqual(
            identity.aliases("guardian", {"url": "https://www.theguardian.com/world/2026/sep/14/x"}),
            ["https://www.theguardian.com/world/2026/sep/14/x"],
        )

    def test_repeated_values_appear_once(self) -> None:
        row = {"id": "https://www.pbs.org/newshour/world/x", "url": "https://www.pbs.org/newshour/world/x"}
        self.assertEqual(identity.aliases("pbs", row), ["https://www.pbs.org/newshour/world/x"])

    def test_a_row_with_nothing_to_identify_it_has_no_aliases(self) -> None:
        self.assertEqual(identity.aliases("bbc", {"headline": "No id, no link"}), [])

    def test_story_id_is_stable_and_outlet_specific(self) -> None:
        first = identity.story_id("bbc", "urn:bbc:optimo:asset:cx2kz4v4e6xo")
        self.assertEqual(first, identity.story_id("bbc", "urn:bbc:optimo:asset:cx2kz4v4e6xo"))
        self.assertNotEqual(first, identity.story_id("reuters", "urn:bbc:optimo:asset:cx2kz4v4e6xo"))
        self.assertTrue(first.startswith("st_"))
        self.assertEqual(len(first), 19)


class UtcTimeTests(unittest.TestCase):
    def test_offsets_and_z_become_utc(self) -> None:
        self.assertEqual(identity.utc_iso("2026-09-14T18:20:00-04:00"), "2026-09-14T22:20:00Z")
        self.assertEqual(identity.utc_iso("2026-09-14T22:20:00Z"), "2026-09-14T22:20:00Z")
        self.assertEqual(identity.utc_iso("2026-09-14T22:20:00.123456+00:00"), "2026-09-14T22:20:00Z")

    def test_rfc_822_feed_dates(self) -> None:
        self.assertEqual(identity.utc_iso("Sun, 14 Sep 2026 18:20:00 GMT"), "2026-09-14T18:20:00Z")

    def test_a_naive_time_is_read_as_utc(self) -> None:
        self.assertEqual(identity.utc_iso("2026-09-14 18:20:00"), "2026-09-14T18:20:00Z")

    def test_epoch_seconds_and_milliseconds(self) -> None:
        self.assertEqual(identity.utc_iso(1789489200), "2026-09-15T16:20:00Z")
        self.assertEqual(identity.utc_iso(1789489200000), "2026-09-15T16:20:00Z")

    def test_a_value_that_will_not_parse_is_none(self) -> None:
        # scraper.common.rss_date passes unparsed values through, so these reach the store.
        for value in (None, "", "   ", "no date here", object()):
            with self.subTest(value=value):
                self.assertIsNone(identity.utc_iso(value))


class TextHashTests(unittest.TestCase):
    def test_reflowed_whitespace_does_not_change_the_hash(self) -> None:
        self.assertEqual(
            identity.text_hash("A man was hurt\n\n  near the bridge."),
            identity.text_hash("A man was hurt near the bridge."),
        )

    def test_different_text_changes_the_hash(self) -> None:
        self.assertNotEqual(identity.text_hash("Two hurt."), identity.text_hash("Three hurt."))

    def test_no_text_is_no_hash(self) -> None:
        self.assertIsNone(identity.text_hash(None))
        self.assertIsNone(identity.text_hash("   \n "))


class FormatFlagTests(unittest.TestCase):
    def test_a_live_url_is_flagged_for_any_outlet(self) -> None:
        for outlet in identity.OUTLETS:
            with self.subTest(outlet=outlet):
                row = {"url": "https://example.com/world/live/2026-sep-14", "categories": {}}
                self.assertIn("live", identity.format_flags(outlet, row))

    def test_reuters_n2_codes(self) -> None:
        for code, flag in identity.REUTERS_N2_FLAGS.items():
            with self.subTest(code=code):
                row = {"url": "https://www.reuters.com/world/x", "categories": {"subjects": [{"code": code}]}}
                self.assertIn(flag, identity.format_flags("reuters", row))

    def test_a_lead_photograph_is_not_a_gallery(self) -> None:
        # Reuters sets primary_media_type to "image" on nearly every ordinary report. Reading that
        # as a gallery flagged 420 of 570 stored Reuters stories and barred them all from pinning.
        row = {"url": "https://www.reuters.com/world/africa/x", "primary_media_type": "image",
               "categories": {}}
        self.assertEqual(identity.format_flags("reuters", row), [])

    def test_a_real_gallery_or_video_is_still_flagged(self) -> None:
        for media, flag in (("gallery", "gallery"), ("video", "video")):
            with self.subTest(media=media):
                row = {"url": "https://www.reuters.com/world/africa/x", "primary_media_type": media,
                       "categories": {}}
                self.assertEqual(identity.format_flags("reuters", row), [flag])

    def test_reuters_plain_report_has_no_flag(self) -> None:
        row = {"url": "https://www.reuters.com/world/africa/x", "categories": {"subjects": [{"code": "AFRICA"}]}}
        self.assertEqual(identity.format_flags("reuters", row), [])

    def test_bbc_non_article_types(self) -> None:
        row = {"url": "https://www.bbc.co.uk/news/videos/c1", "type": "video",
               "subtype": "vertical clip news", "categories": {}}
        self.assertEqual(identity.format_flags("bbc", row), ["video"])
        article = {"url": "https://www.bbc.co.uk/news/articles/c1", "type": "article",
                   "subtype": "news", "categories": {}}
        self.assertEqual(identity.format_flags("bbc", article), [])

    def test_guardian_tone_and_design(self) -> None:
        opinion = {"url": "https://www.theguardian.com/commentisfree/2026/sep/14/x",
                   "categories": {"tones": ["Comment"]}}
        self.assertIn("opinion", identity.format_flags("guardian", opinion))
        video = {"url": "https://www.theguardian.com/world/video/2026/sep/14/x",
                 "categories": {"design": "VideoDesign"}}
        self.assertIn("video", identity.format_flags("guardian", video))
        live = {"url": "https://www.theguardian.com/world/2026/sep/14/x",
                "categories": {"design": "LiveBlogDesign"}}
        self.assertIn("live", identity.format_flags("guardian", live))

    def test_nyt_url_paths(self) -> None:
        briefing = {"url": "https://www.nytimes.com/2026/09/14/briefing/x.html", "categories": {}}
        self.assertEqual(identity.format_flags("nyt", briefing), ["roundup"])
        opinion = {"url": "https://www.nytimes.com/2026/09/14/opinion/x.html", "categories": {}}
        self.assertEqual(identity.format_flags("nyt", opinion), ["opinion"])
        report = {"url": "https://www.nytimes.com/2026/09/14/world/africa/x.html", "categories": {}}
        self.assertEqual(identity.format_flags("nyt", report), [])

    def test_pbs_broadcast_page(self) -> None:
        show = {"url": "https://www.pbs.org/newshour/show/usaid-ebola", "post_type": "show", "categories": {}}
        self.assertIn("broadcast", identity.format_flags("pbs", show))
        post = {"url": "https://www.pbs.org/newshour/world/x", "post_type": "post", "categories": {}}
        self.assertEqual(identity.format_flags("pbs", post), [])

    def test_flags_are_sorted_and_unique(self) -> None:
        row = {"url": "https://www.reuters.com/world/live/x",
               "categories": {"subjects": [{"code": "ANLINS"}, {"code": "EXPLN"}]}}
        flags = identity.format_flags("reuters", row)
        self.assertEqual(flags, sorted(set(flags)))
        self.assertEqual(flags, ["analysis", "explainer", "live"])


class RowFieldTests(unittest.TestCase):
    def test_authors_from_reuters_dicts_and_feed_strings(self) -> None:
        self.assertEqual(
            identity.authors_of({"authors": [{"byline": "A Reporter"}, {"name": "B Reporter"}]}),
            ["A Reporter", "B Reporter"],
        )
        self.assertEqual(identity.authors_of({"authors": ["A Reporter", "A Reporter"]}), ["A Reporter"])
        self.assertEqual(identity.authors_of({"authors": [], "author": "Only One"}), ["Only One"])
        self.assertEqual(identity.authors_of({"authors": []}), [])

    def test_word_count_prefers_the_outlet_and_falls_back_to_the_text(self) -> None:
        self.assertEqual(identity.word_count_of({"word_count": 412, "text": "one two"}), 412)
        self.assertEqual(identity.word_count_of({"word_count": None, "text": "one two  three"}), 3)
        self.assertIsNone(identity.word_count_of({"word_count": None, "text": None}))


class RealRowTests(unittest.TestCase):
    """The rules run over rows built by the real parsers from saved responses."""

    def test_bbc_listing_rows(self) -> None:
        from scraper import bbc

        payload = json.loads((FIXTURES / "bbc" / "listing_india_page1_size5.json").read_text(encoding="utf-8"))
        rows = [bbc.normalise_item(item, "collection") for item in payload["data"]]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(url=row["url"]):
                aliases = identity.aliases("bbc", row)
                self.assertGreaterEqual(len(aliases), 1)
                self.assertTrue(aliases[0].startswith("urn:bbc:"))
                self.assertIsNotNone(identity.utc_iso(row["published"]))
                self.assertIsInstance(identity.format_flags("bbc", row), list)

    def test_nyt_feed_rows(self) -> None:
        from scraper import nyt

        document = (FIXTURES / "nyt" / "Americas.xml").read_bytes()
        from scraper import common

        items = common.parse_rss(document)
        rows = []
        for item in items:
            from scraper import feeds

            row = feeds.base_row(item, nyt.SECTIONS["americas"])
            row["categories"] = {**nyt.keyword_categories(item), **row["categories"]}
            rows.append(row)
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(url=row["url"]):
                self.assertTrue(identity.aliases("nyt", row))
                self.assertIsNotNone(identity.utc_iso(row["published"]))


if __name__ == "__main__":
    unittest.main()
