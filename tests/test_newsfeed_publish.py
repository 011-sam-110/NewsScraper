"""Publish tests (M10). Section 8 of docs/ARCHITECTURE.md.

These cover the snapshot BUILDER only. Sending it is not written, because `/api/ingest/newsfeed`
does not exist on the box yet.

The test that matters most is `NothingAModelWroteTests`. Section 3 says no model-written text is
ever shown or sent, and the only way to hold that line as this stage grows is to assert it over the
whole built body rather than field by field, so a field added later is covered before anyone
remembers to write a test for it.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsfeed import geonames, publish
from newsfeed.geonames import Gazetteer
from newsfeed.publish import (
    MAX_EVIDENCE,
    MAX_ITEMS,
    MAX_REPORTS,
    MAX_TITLE,
    PIN_WINDOW,
    SCHEMA,
    WORLD_WINDOW,
    build_snapshot,
    snapshot_id,
    trim_evidence,
)
from newsfeed.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
CLUSTER_HASH = "cluster0000000000"
EXTRACT_HASH = "extract0000000000"
RESOLVE_HASH = "resolve0000000000"

LONDON = 2643743
WESTMINSTER_LONDON_PPL = 2634341


def lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def build_gazetteer() -> Gazetteer:
    connection = sqlite3.connect(":memory:")
    geonames.build_database(
        connection,
        lines("allCountries.sample.txt"),
        lines("alternateNamesV2.sample.txt"),
        lines("countryInfo.sample.txt"),
        lines("admin1Codes.sample.txt"),
        lines("admin2Codes.sample.txt"),
    )
    return Gazetteer(connection)


def at(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class StoreBuilder:
    """A store holding whole clusters, written the way the real stages write them."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def add(
        self,
        cluster_id: str,
        *,
        outlets: tuple[str, ...] = ("bbc",),
        headline: str = "Example headline",
        published_hours: tuple[float, ...] = (-2.0,),
        is_pin: bool = True,
        geonames_id: int | None = WESTMINSTER_LONDON_PPL,
        latitude: float | None = 51.49700,
        longitude: float | None = -0.13700,
        precision: str | None = "district",
        category: str = "attack_or_violent_crime",
        event_date: str | None = "2026-09-18",
        quote: str | None = "a man was stabbed in Westminster last night",
        place_name: str | None = "Westminster",
        url: str = "https://example.com/story",
    ) -> None:
        with self.store.write() as connection:
            founder = f"{cluster_id}-0"
            for index, outlet in enumerate(outlets):
                story_id = f"{cluster_id}-{index}"
                connection.execute(
                    "INSERT INTO stories (story_id, outlet, primary_alias, url, headline,"
                    " published, first_seen_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        story_id, outlet, f"alias-{story_id}", f"{url}-{index}",
                        headline if index == 0 else f"{headline} ({outlet})",
                        at(published_hours[index] if index < len(published_hours) else -1.0),
                        at(-3), at(-1),
                    ),
                )
                connection.execute(
                    "INSERT INTO cluster_members (story_id, config_hash, cluster_id, verdict,"
                    " joined_at) VALUES (?, ?, ?, ?, ?)",
                    (story_id, CLUSTER_HASH, cluster_id, "same", at(-1)),
                )
            connection.execute(
                "INSERT INTO extractions (story_id, config_hash, category, event_date,"
                " place_name, place_quote, is_physical_event, accepted, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
                (founder, EXTRACT_HASH, category, event_date, place_name, quote, at(-1)),
            )
            connection.execute(
                "INSERT INTO resolutions (story_id, config_hash, extract_hash, resolved,"
                " geonames_id, latitude, longitude, place_precision, pinnable, reason, created_at)"
                " VALUES (?, ?, ?, 1, ?, ?, ?, ?, 1, 'matched', ?)",
                (
                    founder, RESOLVE_HASH, EXTRACT_HASH, geonames_id,
                    latitude, longitude, precision, at(-1),
                ),
            )
            connection.execute(
                "INSERT INTO clusters (cluster_id, config_hash, founder_story, country,"
                " founder_published, latitude, longitude, place_precision, is_pin, created_at)"
                " VALUES (?, ?, ?, 'GB', ?, ?, ?, ?, ?, ?)",
                (
                    cluster_id, CLUSTER_HASH, founder, at(-2),
                    latitude, longitude, precision, 1 if is_pin else 0, at(-1),
                ),
            )


class SnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "news.sqlite3")
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.gazetteer = build_gazetteer()
        self.addCleanup(self.gazetteer.close)
        self.builder = StoreBuilder(self.store)

    def build(self, **kwargs) -> dict:
        options = dict(
            cluster_hash=CLUSTER_HASH,
            extract_hash=EXTRACT_HASH,
            resolve_hash=RESOLVE_HASH,
            now=NOW,
            pins_withheld=False,
            world_news_withheld=False,
        )
        options.update(kwargs)
        return build_snapshot(self.store, self.gazetteer, **options)


class BodyShapeTests(SnapshotTestCase):
    def test_an_empty_store_still_builds_a_valid_body(self) -> None:
        body = self.build()
        self.assertEqual(body["schema"], SCHEMA)
        self.assertEqual(body["items"], [])
        self.assertTrue(body["snapshotId"].startswith("snap_"))
        self.assertEqual(len(body["snapshotId"]), len("snap_") + 16)
        self.assertIsNone(body["dataAsOf"])

    def test_every_outlet_appears_even_with_nothing_to_say(self) -> None:
        """A silent outlet reports null. Dropping it would read as "nothing to report"."""
        body = self.build()
        self.assertEqual([row["outlet"] for row in body["outlets"]], list(publish.OUTLETS))
        self.assertTrue(all(row["lastNewStoryAt"] is None for row in body["outlets"]))

    def test_a_pin_carries_the_fields_the_contract_names(self) -> None:
        self.builder.add("nf_000000000001")
        item = self.build()["items"][0]
        self.assertEqual(
            sorted(item),
            ["category", "eventDate", "firstReportedAt", "id", "lastReportedAt", "location",
             "reports", "title"],
        )
        self.assertEqual(
            sorted(item["location"]),
            ["evidence", "evidenceOutlet", "geonamesId", "lat", "lon", "place", "precision"],
        )

    def test_the_display_name_comes_from_the_gazetteer(self) -> None:
        """The whole point of the name tables: a pin says where it is in words."""
        self.builder.add("nf_000000000001")
        location = self.build()["items"][0]["location"]
        self.assertEqual(location["place"], "City of Westminster, Greater London, United Kingdom")

    def test_coordinates_are_rounded_to_five_places(self) -> None:
        self.builder.add("nf_000000000001", latitude=51.4970012345, longitude=-0.1370098765)
        location = self.build()["items"][0]["location"]
        self.assertEqual(location["lat"], 51.497)
        self.assertEqual(location["lon"], -0.13701)

    def test_a_world_news_item_has_no_location(self) -> None:
        self.builder.add("nf_000000000002", is_pin=False)
        item = self.build()["items"][0]
        self.assertIsNone(item["location"])


class PinRulesTests(SnapshotTestCase):
    def test_a_precision_the_contract_forbids_is_not_a_pin(self) -> None:
        """Region and country are never pinned, section 6. The item stays, as World news."""
        for precision in ("region", "country", None, "made-up"):
            with self.subTest(precision=precision):
                self.setUp()
                self.builder.add("nf_000000000003", precision=precision)
                item = self.build()["items"][0]
                self.assertIsNone(item["location"])

    def test_a_pin_whose_geonames_row_has_gone_falls_back_to_world_news(self) -> None:
        """A monthly rebuild can retire an id. That must not drop the story or raise."""
        self.builder.add("nf_000000000004", geonames_id=999_999_999)
        item = self.build()["items"][0]
        self.assertIsNone(item["location"])

    def test_a_pin_with_no_coordinate_is_not_a_pin(self) -> None:
        self.builder.add("nf_000000000005", latitude=None, longitude=None)
        self.assertIsNone(self.build()["items"][0]["location"])


class WindowTests(SnapshotTestCase):
    def test_a_pin_older_than_seven_days_is_dropped(self) -> None:
        hours = -(PIN_WINDOW.total_seconds() / 3600) - 1
        self.builder.add("nf_000000000006", published_hours=(hours,))
        self.assertEqual(self.build()["items"], [])

    def test_a_pin_inside_seven_days_is_kept(self) -> None:
        hours = -(PIN_WINDOW.total_seconds() / 3600) + 1
        self.builder.add("nf_000000000007", published_hours=(hours,))
        self.assertEqual(len(self.build()["items"]), 1)

    def test_a_world_news_item_gets_72_hours_not_seven_days(self) -> None:
        """The two windows differ, so a shared one would be wrong for one of them."""
        hours = -(WORLD_WINDOW.total_seconds() / 3600) - 1
        self.builder.add("nf_000000000008", is_pin=False, published_hours=(hours,))
        self.assertEqual(self.build()["items"], [])

        self.setUp()
        # The same age as a PIN survives, which is what makes the two windows distinguishable.
        self.builder.add("nf_000000000009", is_pin=True, published_hours=(hours,))
        self.assertEqual(len(self.build()["items"]), 1)


class GateTests(SnapshotTestCase):
    def test_withheld_pins_are_left_out_and_the_flag_is_set(self) -> None:
        self.builder.add("nf_00000000000a")
        self.builder.add("nf_00000000000b", is_pin=False)
        body = self.build(pins_withheld=True)
        self.assertTrue(body["pinsWithheld"])
        self.assertEqual([item["id"] for item in body["items"]], ["nf_00000000000b"])

    def test_withheld_world_news_is_left_out_and_the_flag_is_set(self) -> None:
        self.builder.add("nf_00000000000c")
        self.builder.add("nf_00000000000d", is_pin=False)
        body = self.build(world_news_withheld=True)
        self.assertTrue(body["worldNewsWithheld"])
        self.assertEqual([item["id"] for item in body["items"]], ["nf_00000000000c"])

    def test_both_withheld_sends_an_empty_body_rather_than_no_body(self) -> None:
        """The box must still hear from home, section 7.10. Silence is a different signal."""
        self.builder.add("nf_00000000000e")
        body = self.build(pins_withheld=True, world_news_withheld=True)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["schema"], SCHEMA)


class ReportTests(SnapshotTestCase):
    def test_the_lead_report_is_the_earliest_published_one(self) -> None:
        """Section 8.1: the lead is the founder, the earliest PUBLISHED story."""
        self.builder.add(
            "nf_00000000000f",
            outlets=("bbc", "reuters", "guardian"),
            published_hours=(-2.0, -5.0, -3.0),
        )
        reports = self.build()["items"][0]["reports"]
        self.assertEqual([report["outlet"] for report in reports], ["reuters", "guardian", "bbc"])

    def test_at_most_eight_reports(self) -> None:
        many = tuple(f"bbc" for _ in range(12))
        self.builder.add(
            "nf_000000000010", outlets=many, published_hours=tuple(-float(i) for i in range(12))
        )
        self.assertEqual(len(self.build()["items"][0]["reports"]), MAX_REPORTS)

    def test_a_report_with_no_usable_url_is_left_out(self) -> None:
        self.builder.add("nf_000000000011", url="javascript:alert(1)")
        self.assertEqual(self.build()["items"], [])

    def test_a_long_headline_is_cut_to_the_contract_length(self) -> None:
        self.builder.add("nf_000000000012", headline="x" * 500)
        item = self.build()["items"][0]
        self.assertEqual(len(item["title"]), MAX_TITLE)
        self.assertEqual(len(item["reports"][0]["headline"]), MAX_TITLE)


class EvidenceTests(unittest.TestCase):
    def test_a_short_quote_is_untouched(self) -> None:
        self.assertEqual(trim_evidence("stabbed in Westminster", "Westminster"),
                         "stabbed in Westminster")

    def test_a_long_quote_is_cut_to_the_cap(self) -> None:
        quote = "word " * 200
        trimmed = trim_evidence(quote, None)
        assert trimmed is not None
        self.assertLessEqual(len(trimmed), MAX_EVIDENCE)

    def test_the_place_name_survives_the_trim(self) -> None:
        """The quote exists to show the place name. A trim that loses it loses the point."""
        quote = ("filler " * 60) + "the fire began in Westminster shortly after midnight " + ("filler " * 60)
        trimmed = trim_evidence(quote, "Westminster")
        assert trimmed is not None
        self.assertLessEqual(len(trimmed), MAX_EVIDENCE)
        self.assertIn("Westminster", trimmed)

    def test_nothing_is_added_to_a_quote(self) -> None:
        """No ellipsis and no marker: every character has to be one the outlet wrote."""
        quote = ("alpha " * 100) + "Westminster " + ("beta " * 100)
        trimmed = trim_evidence(quote, "Westminster")
        assert trimmed is not None
        self.assertNotIn("...", trimmed)
        self.assertNotIn("…", trimmed)
        self.assertIn(trimmed, " ".join(quote.split()))

    def test_a_missing_quote_is_none_rather_than_an_empty_string(self) -> None:
        self.assertIsNone(trim_evidence(None, "Westminster"))
        self.assertIsNone(trim_evidence("", "Westminster"))


class SnapshotIdTests(SnapshotTestCase):
    def test_the_same_content_gives_the_same_id_at_a_different_time(self) -> None:
        """Section 7.10 sends only when the content changed, so the id cannot follow the clock."""
        self.builder.add("nf_000000000013")
        first = self.build(now=NOW)
        second = self.build(now=NOW + timedelta(minutes=5))
        self.assertEqual(first["snapshotId"], second["snapshotId"])
        self.assertNotEqual(first["generatedAt"], second["generatedAt"])

    def test_changed_content_gives_a_different_id(self) -> None:
        self.builder.add("nf_000000000014")
        before = self.build()
        self.builder.add("nf_000000000015", is_pin=False)
        self.assertNotEqual(before["snapshotId"], self.build()["snapshotId"])

    def test_the_id_does_not_depend_on_the_order_the_rows_were_inserted(self) -> None:
        """Two items reported in the SAME SECOND must not shuffle the id.

        Building twice from one store proves nothing: SQLite hands back the same order both times
        and Python's sort is stable, so a missing tiebreak stays invisible. Two stores holding the
        same two items in opposite insert order is what actually moves the rows, and it is the real
        case: the two events arrive in whichever order the scrape found them.
        """
        self.builder.add("nf_00000000001a", published_hours=(-2.0,))
        self.builder.add("nf_00000000001b", published_hours=(-2.0,))
        one = self.build()["snapshotId"]

        self.setUp()
        self.builder.add("nf_00000000001b", published_hours=(-2.0,))
        self.builder.add("nf_00000000001a", published_hours=(-2.0,))
        self.assertEqual(one, self.build()["snapshotId"])


class ConfigHashTests(SnapshotTestCase):
    def test_the_wrong_extract_hash_is_visible_rather_than_silent(self) -> None:
        """Reading extractions with the wrong hash matches nothing while every join succeeds.

        Nothing raises, so the only way to notice is that the fields sourced from the extraction
        come back empty. This test exists to make that visible on purpose, so the shape of the
        failure is written down rather than discovered in production.
        """
        self.builder.add("nf_000000000016")
        item = self.build(extract_hash="not-the-hash")["items"][0]
        self.assertIsNone(item["category"])
        self.assertIsNone(item["eventDate"])
        self.assertIsNone(item["location"]["evidence"])

    def test_the_wrong_cluster_hash_returns_nothing_at_all(self) -> None:
        self.builder.add("nf_000000000017")
        self.assertEqual(self.build(cluster_hash="not-the-hash")["items"], [])

    def test_a_second_resolve_config_does_not_duplicate_the_item(self) -> None:
        """resolutions is keyed by (story_id, resolve_hash). Joining without the hash would
        return one row per resolve configuration and build the same event twice."""
        self.builder.add("nf_000000000018")
        with self.store.write() as connection:
            connection.execute(
                "INSERT INTO resolutions (story_id, config_hash, extract_hash, resolved,"
                " geonames_id, latitude, longitude, place_precision, pinnable, reason, created_at)"
                " VALUES (?, 'an-older-resolve-config', ?, 1, ?, 1.0, 2.0, 'city', 1, 'matched', ?)",
                ("nf_000000000018-0", EXTRACT_HASH, LONDON, at(-1)),
            )
        body = self.build()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["location"]["geonamesId"], WESTMINSTER_LONDON_PPL)


class NothingAModelWroteTests(SnapshotTestCase):
    """Section 3: no model-written text is ever shown or sent.

    Asserted over the whole body rather than field by field, so a field added later is covered
    before anyone remembers to write a test for it.
    """

    def test_no_string_in_the_body_came_from_the_model(self) -> None:
        self.builder.add(
            "nf_000000000019",
            outlets=("bbc", "reuters"),
            published_hours=(-2.0, -3.0),
            quote="a man was stabbed in Westminster last night",
        )
        with self.store.write() as connection:
            connection.execute(
                "UPDATE extractions SET raw = ?, not_event_reason = ?, cluster_hint = ?"
                " WHERE story_id = ?",
                (
                    '{"reasoning": "MODEL PROSE, must never be sent"}',
                    "MODEL PROSE, must never be sent",
                    "MODEL PROSE, must never be sent",
                    "nf_000000000019-0",
                ),
            )
        body = json.dumps(self.build(), ensure_ascii=False)
        self.assertNotIn("MODEL PROSE", body)

    def test_the_only_quoted_text_is_the_evidence(self) -> None:
        """The article's own words appear once, in `evidence`, and nowhere else."""
        self.builder.add("nf_00000000001c", quote="a man was stabbed in Westminster last night")
        item = self.build()["items"][0]
        self.assertEqual(item["location"]["evidence"], "a man was stabbed in Westminster last night")
        without_evidence = dict(item)
        without_evidence["location"] = {
            key: value for key, value in item["location"].items() if key != "evidence"
        }
        self.assertNotIn("stabbed", json.dumps(without_evidence))


class CapTests(SnapshotTestCase):
    def test_no_more_than_three_thousand_items(self) -> None:
        self.assertEqual(MAX_ITEMS, 3000)
        body = self.build()
        self.assertLessEqual(len(body["items"]), MAX_ITEMS)
