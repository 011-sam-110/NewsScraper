"""Rail tests. The push to Provenance's live news rail, section 9.0.

The tests that matter are `CrossImplementationTests`. Everything else here checks my own code
against my own expectations, which is worth something but cannot catch the one failure that would
actually stop the rail working: my HMAC agreeing with itself and disagreeing with the TypeScript
that checks it.

So those vectors were not written by hand. They were produced by running the REAL functions out of
Provenance's `lib/news/ingest.ts` at `origin/main` under node, on 2026-09-18, and pasting the
output. If either side changes the signing string, one of these goes red and names the reason,
instead of every push answering 401 with no explanation on either machine.
"""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from newsfeed import rail
from newsfeed.rail import (
    MAX_ITEMS,
    MAX_WIRE_BYTES,
    RailRefused,
    batches,
    body_digest_hex,
    build_event,
    build_item,
    encode,
    headers,
    item_hash,
    rows_since,
    sign,
    signing_string,
)
from newsfeed.store import Store

# Produced by node from lib/news/ingest.ts at origin/main, 2026-09-18. Not a real secret.
VECTOR_SECRET = "rail-test-secret-do-not-use-in-production-0123456789"
VECTORS = [
    {
        "timestamp": 1758182400000,
        "body": '{"version":1,"generatedAt":"2026-09-18T08:00:00Z","items":[]}',
        "digest": "b109d758201b1e9632173920cc96a65ff23863b67d4a3c1a43d81aba3c68b8da",
        "signing_string": (
            "provenance-news-ingest-v1:1758182400000:"
            "b109d758201b1e9632173920cc96a65ff23863b67d4a3c1a43d81aba3c68b8da"
        ),
        "signature": "sha256=0426831f89854a54dca60bf8ddfa227a3b0132b8b93d6e47040f131fd86d4644",
    },
    {
        "timestamp": 1,
        "body": "",
        "digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "signing_string": (
            "provenance-news-ingest-v1:1:"
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        ),
        "signature": "sha256=8a84ed9d2c134d519f9cfb85e14b16f0237ef8e99fd462db841177b6e5ae6a8f",
    },
    {
        # Non-ASCII on purpose. Both sides must hash the UTF-8 bytes, and a side that hashed
        # UTF-16 or a latin-1 mangling would pass every ASCII vector and fail here.
        "timestamp": 1758182400123,
        "body": '{"a":"éü 中文"}',
        "digest": "7cce2b037e82c0ec204b9b274d9696ff110e57907201cdf468f5961081f93614",
        "signing_string": (
            "provenance-news-ingest-v1:1758182400123:"
            "7cce2b037e82c0ec204b9b274d9696ff110e57907201cdf468f5961081f93614"
        ),
        "signature": "sha256=24502446a1cf507cf02b2ae808f4cc789955c6460fd2fbf0c2fb9c1b19c81496",
    },
]


class CrossImplementationTests(unittest.TestCase):
    def test_the_digest_matches_the_typescript(self) -> None:
        for vector in VECTORS:
            with self.subTest(timestamp=vector["timestamp"]):
                self.assertEqual(body_digest_hex(vector["body"].encode("utf-8")), vector["digest"])

    def test_the_signing_string_matches_the_typescript(self) -> None:
        for vector in VECTORS:
            with self.subTest(timestamp=vector["timestamp"]):
                self.assertEqual(
                    signing_string(vector["timestamp"], vector["digest"]), vector["signing_string"]
                )

    def test_the_signature_matches_the_typescript(self) -> None:
        for vector in VECTORS:
            with self.subTest(timestamp=vector["timestamp"]):
                self.assertEqual(
                    sign(VECTOR_SECRET, vector["timestamp"], vector["body"].encode("utf-8")),
                    vector["signature"],
                )

    def test_the_prefix_is_the_rail_one_not_the_snapshot_one(self) -> None:
        """Two contracts live side by side. Section 8 signs `v1\\n<METHOD>\\n/api/ingest/newsfeed`
        and the rail signs `provenance-news-ingest-v1:<ms>:<digest>`. Crossing them is a 401 with
        nothing in either log to say why."""
        self.assertEqual(rail.SIGNATURE_PREFIX, "provenance-news-ingest-v1")
        self.assertIn(rail.SIGNATURE_PREFIX, signing_string(1, "abc"))


class HeaderTests(unittest.TestCase):
    def test_every_header_the_route_reads_is_sent(self) -> None:
        """The exact set, pinned. A header added without a reason is a header nobody decided on.

        `user-agent` is not read by the route. It is read by Cloudflare in front of it, which
        answers the urllib default with 403 before Next ever sees the request. See UserAgentTests.
        """
        sent = headers(VECTOR_SECRET, b"{}", timestamp_ms=1)
        self.assertEqual(
            sorted(sent),
            ["content-type", "user-agent", "x-provenance-content-sha256",
             "x-provenance-signature", "x-provenance-timestamp"],
        )

    def test_the_declared_digest_agrees_with_the_signed_one(self) -> None:
        """They must agree or the route answers 422. Sending it is what makes a mangled body say
        WHICH side mangled it, which a bad signature alone cannot."""
        body = b'{"version":1,"items":[]}'
        sent = headers(VECTOR_SECRET, body, timestamp_ms=1)
        self.assertEqual(sent["x-provenance-content-sha256"], body_digest_hex(body))

    def test_the_timestamp_is_milliseconds_not_seconds(self) -> None:
        """The route parses it and rejects a five minute skew. Seconds would read as 1970."""
        sent = headers(VECTOR_SECRET, b"{}")
        self.assertGreater(int(sent["x-provenance-timestamp"]), 1_700_000_000_000)


class BodyTests(unittest.TestCase):
    def test_generated_at_is_a_string_not_a_number(self) -> None:
        """The far end types `generatedAt` as a number, but reads it with `epochMs`, which takes a
        string and returns 0 for anything else. Sending the number the interface names would be
        silently discarded."""
        decoded = json.loads(encode([], "2026-09-18T08:00:00Z"))
        self.assertIsInstance(decoded["generatedAt"], str)
        self.assertEqual(decoded["version"], 1)
        self.assertEqual(decoded["items"], [])

    def test_the_body_is_utf8_without_escapes(self) -> None:
        """`ensure_ascii=False`, so the digest is taken over the same bytes the far end decodes."""
        body = encode([{"title": "éü"}], "2026-09-18T08:00:00Z")
        self.assertIn("éü".encode("utf-8"), body)


class BatchTests(unittest.TestCase):
    def test_a_batch_never_exceeds_the_item_cap(self) -> None:
        items = [{"id": f"st_{n:016x}", "title": "x"} for n in range(MAX_ITEMS * 2 + 7)]
        produced = list(batches(items))
        self.assertTrue(all(len(batch) <= MAX_ITEMS for batch in produced))
        self.assertEqual(sum(len(batch) for batch in produced), len(items))

    def test_a_batch_is_split_by_size_before_it_reaches_the_item_cap(self) -> None:
        items = [{"id": f"st_{n:016x}", "text": "x" * 200_000} for n in range(30)]
        produced = list(batches(items))
        self.assertGreater(len(produced), 1)
        self.assertTrue(all(len(batch) < MAX_ITEMS for batch in produced))

    def test_one_oversized_story_is_sent_alone_rather_than_dropped(self) -> None:
        """Dropping it would quietly lose exactly the live blogs and long reads the rail wants."""
        items = [{"id": "st_small", "text": "x"}, {"id": "st_huge", "text": "x" * 4_000_000}]
        produced = list(batches(items))
        self.assertEqual([len(batch) for batch in produced], [1, 1])
        self.assertEqual(produced[1][0]["id"], "st_huge")

    def test_no_item_is_lost_or_duplicated(self) -> None:
        items = [{"id": f"st_{n}", "text": "x" * (n * 1000)} for n in range(120)]
        produced = [item["id"] for batch in batches(items) for item in batch]
        self.assertEqual(produced, [item["id"] for item in items])


class ItemTests(unittest.TestCase):
    def row(self, **kwargs) -> dict:
        base = dict(
            story_id="st_ca5f18f0fce56944", outlet="bbc", headline="A headline",
            description="A description", url="https://example.com/a", published="2026-09-18T07:00:00Z",
            first_seen_at="2026-09-18T07:05:00Z", last_seen_at="2026-09-18T08:00:00Z",
            word_count=400, thumbnail=None, text_hash="abc", format_flags='["gallery"]',
            categories='["world"]', text="the body", sections=["world"],
            is_physical_event=None,
        )
        base.update(kwargs)
        return base

    def test_the_five_fields_the_route_requires_are_always_present(self) -> None:
        """Without any one of id, outlet, title, url or lastSeenAt the row is dropped at the far
        end rather than stored half formed."""
        item = build_item(self.row())
        for field in ("id", "outlet", "title", "url", "lastSeenAt"):
            with self.subTest(field=field):
                self.assertTrue(item[field])

    def test_author_names_are_never_sent(self) -> None:
        """CLAUDE.md forbids them in a published body. Not sending them removes the conflict
        rather than trusting the far end to keep dropping them."""
        item = build_item(self.row())
        self.assertNotIn("authors", item)
        self.assertNotIn("authors", json.dumps(item))

    def test_place_hints_are_empty_because_they_are_veto_only(self) -> None:
        """An outlet tag can rule a place out and can never supply one. Both sides hold the rule,
        and a Reuters dateline says where the reporter filed, not where the event happened."""
        self.assertEqual(build_item(self.row())["placeHints"], [])

    def test_text_is_sent_by_default_and_can_be_left_out(self) -> None:
        self.assertEqual(build_item(self.row())["text"], "the body")
        self.assertNotIn("text", build_item(self.row(), send_text=False))

    def test_an_unextracted_story_carries_no_event(self) -> None:
        self.assertIsNone(build_item(self.row())["event"])

    def test_is_physical_is_a_real_boolean_not_the_sqlite_integer(self) -> None:
        """The far end accepts `true` or `1`, precisely because a sender that passes the column
        straight through ships `1`. Sending the boolean is correct even if it ever tightens."""
        event = build_event(self.row(is_physical_event=1))
        assert event is not None
        self.assertIs(event["isPhysical"], True)
        self.assertIs(json.loads(json.dumps(event))["isPhysical"], True)

    def test_a_false_extraction_is_an_event_not_a_missing_one(self) -> None:
        """0 and None mean different things: "the model said no" and "the model never ran"."""
        self.assertIsNotNone(build_event(self.row(is_physical_event=0)))
        self.assertIsNone(build_event(self.row(is_physical_event=None)))

    def test_a_malformed_json_column_is_an_empty_list_not_a_crash(self) -> None:
        item = build_item(self.row(format_flags="not json at all", categories=None))
        self.assertEqual(item["formatFlags"], [])
        self.assertEqual(item["keywords"], [])


class ItemHashTests(unittest.TestCase):
    def base(self, **kwargs) -> dict:
        row = dict(
            headline="A", description="B", url="u", published="p", thumbnail=None,
            word_count=1, text_hash=None, sections=["world"], format_flags="[]",
            event_fingerprint=None,
        )
        row.update(kwargs)
        return row

    def test_the_same_story_hashes_the_same(self) -> None:
        self.assertEqual(item_hash(self.base()), item_hash(self.base()))

    def test_a_changed_headline_changes_the_hash(self) -> None:
        self.assertNotEqual(item_hash(self.base()), item_hash(self.base(headline="different")))

    def test_it_still_changes_when_there_is_no_text_hash(self) -> None:
        """text_hash is NULL for every NYT row and every row whose article was never fetched. If
        the key were the text hash alone, those rows could never be updated at the far end."""
        first = self.base(text_hash=None, headline="A")
        second = self.base(text_hash=None, headline="A corrected headline")
        self.assertNotEqual(item_hash(first), item_hash(second))

    def test_a_new_extraction_changes_the_hash(self) -> None:
        """Otherwise a story extracted after it was first pushed would never reach the map."""
        self.assertNotEqual(
            item_hash(self.base(event_fingerprint=None)),
            item_hash(self.base(event_fingerprint='{"isPhysical":true}')),
        )

    def test_section_order_does_not_change_it(self) -> None:
        self.assertEqual(
            item_hash(self.base(sections=["world", "uk"])),
            item_hash(self.base(sections=["uk", "world"])),
        )


class CursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "news.sqlite3")
        self.store.migrate()
        self.addCleanup(self.store.close)
        with self.store.write() as connection:
            for index, seen in enumerate(
                ["2026-09-18T06:00:00Z", "2026-09-18T07:00:00Z", "2026-09-18T08:00:00Z"]
            ):
                connection.execute(
                    "INSERT INTO stories (story_id, outlet, primary_alias, url, headline,"
                    " published, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"st_{index}", "bbc", f"a{index}", f"https://example.com/{index}",
                     f"Headline {index}", seen, seen, seen),
                )

    def read(self, cursor: str) -> list[str]:
        return [row["story_id"] for row in rows_since(self.store, cursor, extract_hash="x", limit=50)]

    def test_an_empty_cursor_reads_everything(self) -> None:
        self.assertEqual(self.read(""), ["st_0", "st_1", "st_2"])

    def test_the_cursor_is_exclusive(self) -> None:
        """The far end returns the high-water mark of what it ACCEPTED, so resending that row
        would be pure duplication."""
        self.assertEqual(self.read("2026-09-18T07:00:00Z"), ["st_2"])

    def test_rows_come_back_oldest_first(self) -> None:
        """A newest-first read would make the cursor skip everything behind the first batch."""
        self.assertEqual(self.read(""), sorted(self.read("")))

    def test_a_cursor_past_the_end_reads_nothing(self) -> None:
        self.assertEqual(self.read("2030-01-01T00:00:00Z"), [])


class RefusalTests(unittest.TestCase):
    def test_the_status_is_kept_so_the_caller_can_tell_them_apart(self) -> None:
        """404 means the box has no secret, 401 means ours is wrong, 503 means the maintenance
        curtain is up and the push should simply be retried."""
        for status in (401, 404, 413, 422, 503):
            with self.subTest(status=status):
                self.assertEqual(RailRefused(status, "body").status, status)

    def test_a_body_over_the_wire_cap_is_refused_here_not_there(self) -> None:
        """Sending it would cost the far end a 413 and us the whole batch."""
        with self.assertRaises(ValueError):
            rail.post("https://example.invalid", VECTOR_SECRET, b"x" * (MAX_WIRE_BYTES + 1),
                      compress=False)


class CompressionTests(unittest.TestCase):
    def test_compression_happens_after_signing(self) -> None:
        """The signature covers the CONTENT, not the wire bytes, because Cloudflare may recompress
        or re-chunk. Signing the gzip would fail for a reason no log on either side could show."""
        body = encode([{"id": "st_1", "text": "x" * 5000}], "2026-09-18T08:00:00Z")
        sent = headers(VECTOR_SECRET, body, timestamp_ms=1)
        self.assertEqual(sent["x-provenance-signature"], sign(VECTOR_SECRET, 1, body))
        self.assertNotEqual(sent["x-provenance-signature"], sign(VECTOR_SECRET, 1, gzip.compress(body)))

    def test_gzip_round_trips_to_the_signed_bytes(self) -> None:
        body = encode([{"id": "st_1", "text": "x" * 5000}], "2026-09-18T08:00:00Z")
        self.assertEqual(gzip.decompress(gzip.compress(body)), body)


class SecretHygieneTests(unittest.TestCase):
    def test_no_secret_is_committed_in_this_module(self) -> None:
        """The repo is public. The only literal here is a test vector secret that is named as one."""
        source = Path(rail.__file__).read_text(encoding="utf-8")
        self.assertNotIn("NEWS_INGEST_SECRET=", source)
        for line in source.splitlines():
            if "secret" in line.lower():
                self.assertNotRegex(line, r'secret\s*=\s*"[A-Za-z0-9+/]{16,}"')


class UserAgentTests(unittest.TestCase):
    """The client names itself, because the default one never reaches the route.

    Provenance sits behind Cloudflare. Its browser integrity check answers `Python-urllib/3.13`
    with 403 and error code 1010, before the request reaches Next at all, so the failure reads as
    the box refusing us when the box never saw it. Measured against production on 2026-09-18.
    """

    def test_every_request_carries_a_user_agent(self) -> None:
        sent = rail.headers("secret", b"{}")
        self.assertIn("user-agent", sent)
        self.assertEqual(sent["user-agent"], rail.USER_AGENT)

    def test_it_is_not_the_default_one_cloudflare_refuses(self) -> None:
        self.assertNotIn("urllib", rail.USER_AGENT.lower())
        self.assertNotIn("python", rail.USER_AGENT.lower())

    def test_it_names_the_sender_and_where_to_find_it(self) -> None:
        """What a server operator wants when they look a client up in their logs."""
        self.assertIn("NewsScraper", rail.USER_AGENT)
        self.assertIn("https://", rail.USER_AGENT)

    def test_it_does_not_claim_to_be_a_browser(self) -> None:
        for lie in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
            self.assertNotIn(lie, rail.USER_AGENT)

    def test_the_user_agent_is_not_signed_over(self) -> None:
        """The signature covers the body. A header the CDN may rewrite must not be in it."""
        first = rail.sign("secret", 1_700_000_000_000, b'{"a":1}')
        self.assertEqual(first, rail.sign("secret", 1_700_000_000_000, b'{"a":1}'))
        self.assertNotIn(rail.USER_AGENT, rail.signing_string(1_700_000_000_000, "deadbeef"))


class RailPinVetoTests(unittest.TestCase):
    """The rail has no gate of its own unless this side puts one there.

    The section 8 snapshot refuses a pin twice: a category that can never be about a place, and any
    precision other than point, district or city. The rail path goes straight from the extraction
    to Provenance's geocoder, so unless these run here, neither end runs them.
    """

    def row(self, **changes):
        base = {
            "is_physical_event": 1,
            "category": "attack_or_violent_crime",
            "event_date": "2026-09-18",
            "place_name": "Westminster",
            "place_within": "London",
            "place_country": "GB",
            "place_kind": "district",
            "place_quote": "a man was stabbed in Westminster last night",
            "other_places": None,
            "key_entities": None,
        }
        base.update(changes)
        return base

    def test_an_ordinary_event_is_sent_whole(self) -> None:
        event = rail.build_event(self.row())
        self.assertTrue(event["isPhysical"])
        self.assertEqual(event["placeName"], "Westminster")
        self.assertEqual(event["quote"], "a man was stabbed in Westminster last night")

    def test_a_category_that_can_never_pin_is_sent_unpinnable(self) -> None:
        """The Kuala Lumpur acquittal case: a courtroom is a place, and the story is not about it."""
        event = rail.build_event(self.row(category="courts_and_justice"))
        self.assertFalse(event["isPhysical"])
        for field in ("placeName", "placeWithin", "placeCountry", "placeKind", "quote"):
            self.assertIsNone(event[field], field)

    def test_an_opinion_piece_is_sent_unpinnable(self) -> None:
        event = rail.build_event(self.row(category="opinion_analysis"))
        self.assertFalse(event["isPhysical"])
        self.assertIsNone(event["placeName"])

    def test_region_and_country_precision_are_sent_unpinnable(self) -> None:
        """Section 6: region and country are never pinned."""
        for kind in ("region", "country", "Region", " COUNTRY "):
            event = rail.build_event(self.row(place_kind=kind))
            self.assertFalse(event["isPhysical"], kind)
            self.assertIsNone(event["placeName"], kind)

    def test_the_precisions_the_contract_allows_still_pin(self) -> None:
        for kind in ("city", "district", "venue", "street", "point"):
            event = rail.build_event(self.row(place_kind=kind))
            self.assertTrue(event["isPhysical"], kind)
            self.assertEqual(event["placeName"], "Westminster", kind)

    def test_the_category_and_the_story_still_travel(self) -> None:
        """A vetoed row is still news. It stops claiming a place; it does not vanish."""
        event = rail.build_event(self.row(category="courts_and_justice"))
        self.assertEqual(event["category"], "courts_and_justice")
        self.assertEqual(event["eventDate"], "2026-09-18")

    def test_a_row_the_extractor_already_called_unphysical_is_untouched(self) -> None:
        event = rail.build_event(self.row(is_physical_event=0, place_kind="city"))
        self.assertFalse(event["isPhysical"])

    def test_no_extraction_at_all_is_still_None(self) -> None:
        self.assertIsNone(rail.build_event(self.row(is_physical_event=None)))

    def test_an_unknown_category_or_kind_is_allowed(self) -> None:
        """The permissive direction, deliberately: a strict default stops pinning silently."""
        event = rail.build_event(self.row(category=None, place_kind=None))
        self.assertTrue(event["isPhysical"])
        event = rail.build_event(self.row(category="something_new", place_kind="hamlet"))
        self.assertTrue(event["isPhysical"])

    def test_the_reason_is_named_so_a_log_can_say_which_rule_fired(self) -> None:
        self.assertEqual(
            rail.pinnable_on_the_rail("courts_and_justice", "city"),
            (False, "category_never_pins"),
        )
        self.assertEqual(
            rail.pinnable_on_the_rail("disaster", "region"),
            (False, "precision_never_pins"),
        )
        self.assertEqual(rail.pinnable_on_the_rail("disaster", "city"), (True, ""))
