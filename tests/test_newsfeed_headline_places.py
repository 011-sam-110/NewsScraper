"""The Headline sources placement: feed rows into extract, the dateline rule, the relay snapshot.

Provenance's Headline sources layer draws a mark only for a headline whose story DeepSeek placed, by
URL, through the signed relay feed `headline-places`. These tests pin each step that decides what
crosses: which feed rows are read, what text they are read with, which quotes are evidence, and what
the snapshot carries.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from newsfeed import extract, headline_places, relay
from newsfeed.extract import check_evidence
from newsfeed.identity import now_utc
from newsfeed.settings import Settings
from newsfeed.store import STATUS_EXTRACTED, STATUS_FEED, Store

CURRENT = "cfg_current"


def payload(quote: str, name: str) -> dict:
    return {"event_place": {"name": name, "quote": quote}}


class DatelineTests(unittest.TestCase):
    """CLAUDE.md: a dateline is not the event place. It says where the report was filed."""

    def test_a_yonhap_dateline_alone_is_not_evidence(self) -> None:
        # Measured on 2026-11-05: three baseball stories were placed on Suwon by this alone.
        quote = "SUWON, South Korea, Nov. 4 (Yonhap) -- The Samsung Lions edged out the KT Wiz 3-"
        reason = check_evidence(payload(quote, "Suwon"))
        self.assertIsNotNone(reason)
        self.assertIn("dateline", reason)

    def test_a_wire_dateline_with_no_date_is_caught_too(self) -> None:
        self.assertIsNotNone(check_evidence(payload("LONDON (Reuters) - Shares fell on Tuesday.", "London")))
        self.assertIsNotNone(check_evidence(payload("KYIV, Ukraine (AP) — Officials met again.", "Kyiv")))

    def test_a_dateline_whose_story_names_the_place_again_passes(self) -> None:
        quote = "SEOUL, Nov. 5 (Yonhap) -- A fire broke out at a market in Seoul on Wednesday"
        self.assertIsNone(check_evidence(payload(quote, "Seoul")))

    def test_a_quote_that_is_not_a_dateline_is_untouched(self) -> None:
        self.assertIsNone(check_evidence(payload("A fire broke out in Suwon on Tuesday.", "Suwon")))
        self.assertIsNone(check_evidence(payload("Mother and daughter die in bear attack in Canada", "Canada")))


class FeedRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            data_dir=Path(tempfile.mkdtemp()),
            deepseek_api_key=None,
            ingest_url=None,
            ingest_secret=None,
            daily_budget_usd=1.0,
            telegram_bot_token=None,
            telegram_chat_id=None,
            deadman_url=None,
            proxy=None,
        )

    def seed(self, story_id: str, status: str, *, first_seen: str | None = None, extracted: dict | None = None,
             outlet: str = "bbc-world", url: str | None = None) -> None:
        now = now_utc()
        with Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as db:
                db.execute(
                    """INSERT INTO stories (story_id, outlet, primary_alias, url, source_url, headline, description,
                                            published, first_seen_at, last_seen_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (story_id, outlet, story_id, url or f"https://example.org/{story_id}",
                     url or f"https://example.org/{story_id}",
                     "Mother and daughter die after suspected grizzly bear attack in Canada",
                     "More than one grizzly may have been involved in the attack near Bella Coola.",
                     now, first_seen or now, now, status),
                )
                if extracted is not None:
                    db.execute(
                        """INSERT INTO extractions (story_id, config_hash, accepted, failure_reason, is_physical_event,
                                                    place_name, place_within, place_country, place_kind, place_quote,
                                                    created_at)
                           VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (story_id, CURRENT, extracted.get("failure"), 1 if extracted.get("event", True) else 0,
                         extracted.get("name"), extracted.get("within"), extracted.get("country", "CA"),
                         extracted.get("kind", "city"), extracted.get("quote"), now),
                    )

    def claim(self, since: str = "2000-01-01T00:00:00Z") -> list[str]:
        with Store(self.settings.news_db) as store:
            store.migrate()
            return store.claim_feed_extractions(CURRENT, 10, since)

    def test_only_extract_feeds_claims_a_feed_row(self) -> None:
        self.seed("st_feed", STATUS_FEED)
        with Store(self.settings.news_db) as store:
            store.migrate()
            self.assertEqual(store.claim_stories("scraped", 10), [], "a plain run never reads a feed row")
        self.assertEqual(self.claim(), ["st_feed"])
        self.assertEqual(self.claim(), [], "a leased row is not handed out twice")

    def test_a_feed_row_already_read_under_this_hash_or_too_old_is_not_claimed(self) -> None:
        self.seed("st_done", STATUS_FEED, extracted={"name": "Bella Coola", "quote": "near Bella Coola"})
        self.seed("st_old", STATUS_FEED, first_seen="2026-01-01T00:00:00Z")
        self.assertEqual(self.claim(since="2026-06-01T00:00:00Z"), [])

    def test_a_feed_row_is_read_with_its_headline_and_summary_and_keeps_its_status(self) -> None:
        self.seed("st_feed", STATUS_FEED)
        with Store(self.settings.news_db) as store:
            store.migrate()
            story = extract.load_story(store, "st_feed")
            self.assertEqual(
                story["text"],
                "Mother and daughter die after suspected grizzly bear attack in Canada. "
                "More than one grizzly may have been involved in the attack near Bella Coola.",
            )
            checked = extract.Checked(True, None, {"is_physical_event": True}, {})
            completion = SimpleNamespace(content="{}", model="deepseek-test")
            extract.store_extraction(store, story, CURRENT, checked, completion, [], False)
            status = store.scalar("SELECT status FROM stories WHERE story_id = 'st_feed'")
        self.assertEqual(status, STATUS_FEED, "resolve, cluster and the rail must never see a feed row")
        self.assertNotEqual(status, STATUS_EXTRACTED)

    def test_the_snapshot_holds_only_placed_rows_and_no_text_a_model_wrote(self) -> None:
        self.seed("st_placed", STATUS_FEED, url="https://www.bbc.co.uk/news/a",
                  extracted={"name": "Bella Coola", "quote": "near Bella Coola"})
        self.seed("st_failed", STATUS_FEED, url="https://www.bbc.co.uk/news/b",
                  extracted={"name": None, "quote": None, "failure": "the quote is a dateline"})
        self.seed("st_not_event", STATUS_FEED, url="https://www.bbc.co.uk/news/c",
                  extracted={"event": False, "name": None})
        self.seed("st_unresolved", STATUS_FEED, url="https://www.bbc.co.uk/news/d",
                  extracted={"name": "Nowhere", "quote": "in Nowhere"})
        self.seed("st_http", STATUS_FEED, url="http://example.org/e",
                  extracted={"name": "Bella Coola", "quote": "near Bella Coola"})
        self.seed("st_dropped", STATUS_FEED, outlet="guardian", url="https://www.theguardian.com/f",
                  extracted={"name": "Bella Coola", "quote": "near Bella Coola"})

        def place_of(name, within, country, kind):
            if name != "Bella Coola":
                return None  # not found, climbed or not pinnable: no place
            return {"id": 5897884, "name": "Bella Coola, Canada", "lat": 52.3732, "lon": -126.7537, "precision": "area"}

        with Store(self.settings.news_db) as store:
            store.migrate()
            rows = headline_places.placed_rows(store, CURRENT, "2000-01-01T00:00:00Z")
        items = headline_places.build_items(rows, place_of)
        self.assertEqual([item["url"] for item in items], ["https://www.bbc.co.uk/news/a"])
        self.assertEqual(set(items[0]), {"url", "place", "quote", "extractedAt"})
        self.assertEqual(items[0]["place"]["name"], "Bella Coola, Canada")
        self.assertEqual(items[0]["quote"], "near Bella Coola")

    def test_a_climbed_or_unpinnable_answer_is_no_place(self) -> None:
        from newsfeed.geonames import Place

        town = Place(706448, "Kharkiv", 49.98081, 36.25272, "P", "PPLA", "UA", "07", None, 1430885)
        region = Place(709716, "Donetsk Oblast", 48.0, 37.5, "A", "ADM1", "UA", "05", None, 4100000)
        places = [town, region]
        gazetteer = SimpleNamespace(
            candidates=lambda name, country: [p for p in places if p.name == name and p.country == country],
            display_name=lambda p: f"{p.name}, Ukraine",
        )
        place_of = headline_places.gazetteer_placer(gazetteer)
        self.assertEqual(place_of("Kharkiv", None, "UA", "city")["precision"], "area")
        self.assertIsNone(place_of("Komyshuvakha", "Kharkiv", "UA", "village"), "climbed to its town: no place")
        self.assertIsNone(place_of("Donetsk Oblast", None, "UA", "region"), "a region is never sent")


class RailTests(unittest.TestCase):
    def test_a_feed_row_crosses_the_news_rail_with_no_event(self) -> None:
        """Feed rows cross the rail as headline and link. Their place goes only to the relay."""
        from newsfeed import rail

        row = {
            "story_id": "st_feed", "outlet": "bbc-world", "headline": "H", "url": "https://example.org/a",
            "last_seen_at": "2026-11-05T09:00:00Z", "status": STATUS_FEED, "is_physical_event": 1,
            "category": "disaster", "place_name": "Bella Coola", "place_kind": "city", "place_quote": "q",
        }
        self.assertIsNone(rail.build_item(row)["event"])
        scraped = dict(row, status="extracted")
        self.assertIsNotNone(rail.build_item(scraped)["event"])


class RelayVectorTests(unittest.TestCase):
    """The same literals as Provenance's tests/unit/relay-protocol.test.ts."""

    def test_every_vector(self) -> None:
        for vector in relay.TEST_VECTORS:
            with self.subTest(vector["name"]):
                self.assertEqual(relay.body_digest_hex(vector["body"]), vector["digest"])
                self.assertEqual(
                    relay.sign(vector["secret"], vector["timestamp_ms"], vector["body"]),
                    "sha256=" + vector["signature"],
                )

    def test_a_short_secret_is_refused_here(self) -> None:
        with self.assertRaises(relay.SigningError):
            relay.sign("short", 1, b"{}")

    def test_the_envelope_names_the_feed_and_the_address_is_https(self) -> None:
        body = relay.body_bytes(relay.snapshot_envelope("headline-places", "2026-11-05T12:00:00Z", {"items": []}))
        self.assertEqual(json.loads(body)["feed"], "headline-places")
        self.assertEqual(relay.feed_url("https://provenance-online.com/", "headline-places"),
                         "https://provenance-online.com/api/relay/headline-places")
        with self.assertRaises(relay.SigningError):
            relay.feed_url("http://provenance-online.com", "headline-places")

    def test_a_relay_signature_is_not_a_rail_signature(self) -> None:
        from newsfeed import rail

        body = b'{"version":1,"feed":"headline-places","probe":true}'
        secret = "provenance-relay-test-secret-00000001"
        rail_sig = rail.headers(secret, body)
        stamp = int(rail_sig[next(k for k in rail_sig if "timestamp" in k.lower())])
        self.assertNotEqual(relay.sign(secret, stamp, body), rail_sig[next(k for k in rail_sig if "signature" in k.lower())])


if __name__ == "__main__":
    unittest.main()
