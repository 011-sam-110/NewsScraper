"""Stage: rail. Push scraped stories to Provenance's news rail.

This is NOT the section 8 snapshot. Section 9.0 records the difference and it matters: Provenance
already merged and deployed `POST /api/news/ingest`, which takes raw scraped STORIES under the
signature prefix `provenance-news-ingest-v1`. The section 8 contract carries located PINS to
`/api/ingest/newsfeed`, which does not exist on the box yet and is milestone M9.

The practical consequence is the reason this module exists: the rail is reachable WITHOUT the
gate. A pin must wait for the M8 quality gate, seven days after the prompt freeze, because a wrong
pin asserts something about the world. A story on the rail is a headline and a link, already
published by the outlet, so it carries no such claim and nothing has to be withheld.

Rights, from `CLAUDE.md`: `text` is sent because section 9.0 sanctions it and Provenance's own
`NewsItem` has no text field by design, so it is input to clustering and never served. Author names
are not sent at all: the receiving parser drops them on purpose, and not sending them removes the
conflict rather than relying on the far end to keep removing it.

The secret is read from settings and never logged, never committed, and never printed. This repo is
public.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Iterator, Sequence

from .store import Store

if TYPE_CHECKING:  # pragma: no cover
    import argparse

    from .settings import Settings

# lib/news/ingest.ts. Changing any of these is a breaking protocol change on both sides.
SIGNATURE_PREFIX = "provenance-news-ingest-v1"
MAX_ITEMS = 500
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_WIRE_BYTES = 4 * 1024 * 1024
MAX_SKEW_MS = 5 * 60 * 1000

# Under the 8 MiB ceiling with room for one oversized story: a Reuters live blog assembles every
# post into a single body, and the far end's comment says the headroom exists for exactly that.
TARGET_BODY_BYTES = 3 * 1024 * 1024

TIMESTAMP_HEADER = "x-provenance-timestamp"
SIGNATURE_HEADER = "x-provenance-signature"
DIGEST_HEADER = "x-provenance-content-sha256"

PATH = "/api/news/ingest"


def body_digest_hex(body: bytes) -> str:
    """SHA-256 of the body, hex. The signature covers this, not the object."""
    return hashlib.sha256(body).hexdigest()


def signing_string(timestamp_ms: int, digest_hex: str) -> str:
    """Exactly what both sides sign.

    The far end builds this same string in `signingString` in `lib/news/ingest.ts`. It is signed
    over the UNCOMPRESSED JSON, never the bytes on the wire, because Cloudflare sits in front of
    Provenance and a proxy that recompresses or re-chunks a body changes the wire bytes without
    changing a character of the content. A signature over the wire would then fail for a reason no
    log on either side could explain.
    """
    return f"{SIGNATURE_PREFIX}:{timestamp_ms}:{digest_hex}"


def sign(secret: str, timestamp_ms: int, body: bytes) -> str:
    """The `x-provenance-signature` value, `sha256=` and the hex HMAC."""
    message = signing_string(timestamp_ms, body_digest_hex(body)).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# Provenance sits behind Cloudflare, whose browser integrity check answers the default
# `Python-urllib/3.13` with 403 and Cloudflare error 1010. The request never reaches the route, so
# the failure looks like the box refusing us when the box never saw it. Measured against production
# on 2026-09-18, same body, same second:
#
#   User-Agent: Python-urllib/3.13    403, Cloudflare 1010
#   User-Agent: the string below      401, which is the route itself refusing an unsigned body
#
# This names what we are and links to the code, which is what a server operator wants to find when
# they look up a client in their logs. It is NOT the rule in CLAUDE.md about not changing the user
# agent: that one is about the Reuters scraper and DataDome, where the agent is part of a
# fingerprint that was proven to work. This is our own client talking to our own server.
USER_AGENT = "NewsScraper-rail/1 (+https://github.com/011-sam-110/NewsScraper)"


def headers(secret: str, body: bytes, timestamp_ms: int | None = None) -> dict[str, str]:
    """Every header the route reads, including the optional declared digest.

    The digest header proves nothing the signature does not already prove. It is sent because when
    the two disagree the far end can say WHICH side mangled the body, and the signature alone
    cannot: a bad signature means "one of five things", a digest mismatch means "the content
    changed in transit".
    """
    stamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        TIMESTAMP_HEADER: str(stamp),
        SIGNATURE_HEADER: sign(secret, stamp, body),
        DIGEST_HEADER: body_digest_hex(body),
    }


ITEM_SQL = """
SELECT s.story_id, s.outlet, s.headline, s.description, s.url, s.published,
       s.first_seen_at, s.last_seen_at, s.word_count, s.thumbnail, s.text_hash,
       s.format_flags, s.categories,
       t.text,
       e.is_physical_event, e.category, e.event_date, e.place_name, e.place_within,
       e.place_country, e.place_kind, e.place_quote, e.other_places, e.key_entities
FROM stories s
LEFT JOIN story_texts t ON t.story_id = s.story_id
LEFT JOIN extractions e ON e.story_id = s.story_id AND e.config_hash = ?
WHERE s.last_seen_at > ?
ORDER BY s.last_seen_at, s.story_id
LIMIT ?
"""


def _json_list(value: str | None) -> list[str]:
    """A stored JSON array of strings, or an empty list. A malformed one is never fatal."""
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (ValueError, TypeError):
        return []
    if isinstance(loaded, dict):
        loaded = list(loaded.keys())
    if not isinstance(loaded, list):
        return []
    return [str(entry) for entry in loaded if isinstance(entry, (str, int, float)) and str(entry).strip()]


def item_hash(row: dict[str, Any]) -> str:
    """A digest over everything in the item that can change. The far end's idempotency key.

    `text_hash` alone would not do: it is NULL for every NYT row and every row whose article was
    never fetched, which would make those rows permanently unchangeable at the far end. The far
    end's own comment says so, so this covers the fields a re-scrape can move instead.
    """
    material = json.dumps(
        [
            row.get("headline"), row.get("description"), row.get("url"), row.get("published"),
            row.get("thumbnail"), row.get("word_count"), row.get("text_hash"),
            sorted(row.get("sections") or []), sorted(_json_list(row.get("format_flags"))),
            row.get("event_fingerprint"),
        ],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """The extraction, in the far end's `NewsEvent` shape, or None when extract has not run.

    `isPhysical` is sent as a real boolean. SQLite holds it as an INTEGER, and the far end accepts
    `true` OR `1` precisely because a sender that passes the column straight through ships `1`.
    Sending the boolean rather than leaning on that tolerance means this side is correct even if
    the far end ever tightens it.
    """
    if row.get("is_physical_event") is None:
        return None
    return {
        "isPhysical": bool(row["is_physical_event"]),
        "category": row.get("category"),
        "eventDate": row.get("event_date"),
        "placeName": row.get("place_name"),
        "placeWithin": row.get("place_within"),
        "placeCountry": row.get("place_country"),
        "placeKind": row.get("place_kind"),
        "quote": row.get("place_quote"),
        "otherPlaces": _json_list(row.get("other_places"))[:12],
        "keyEntities": _json_list(row.get("key_entities"))[:12],
    }


def build_item(row: dict[str, Any], *, send_text: bool = True) -> dict[str, Any]:
    """One row in the far end's `ScrapedItem` shape.

    `authors` is not sent. The receiving parser drops it deliberately, and `CLAUDE.md`'s rule
    against author names in a published body is easier to keep by not sending them than by trusting
    the far end to keep dropping them.
    """
    event = build_event(row)
    item: dict[str, Any] = {
        "id": row["story_id"],
        "outlet": row["outlet"],
        "title": row["headline"],
        "description": row.get("description"),
        "url": row["url"],
        "published": row.get("published"),
        "firstSeenAt": row.get("first_seen_at"),
        "lastSeenAt": row["last_seen_at"],
        "itemHash": row.get("item_hash"),
        "sections": row.get("sections") or [],
        "formatFlags": _json_list(row.get("format_flags")),
        "wordCount": row.get("word_count"),
        "thumbnail": row.get("thumbnail"),
        "textHash": row.get("text_hash"),
        "keywords": _json_list(row.get("categories")),
        # Veto-only by contract at both ends: an outlet tag can rule a place out and can never
        # supply one. Reuters datelines are excluded upstream for the same reason.
        "placeHints": [],
        "event": event,
    }
    if send_text and row.get("text"):
        item["text"] = row["text"]
    return item


def encode(items: Sequence[dict[str, Any]], generated_at: str) -> bytes:
    """The request body. `generatedAt` is an RFC3339 STRING, not a number.

    The far end's `Snapshot.generatedAt` is typed as a number, which is the PARSED form: it reads
    the field with `epochMs`, which requires a string and returns 0 for anything else. Sending the
    number that the interface names would silently become 0.
    """
    return json.dumps(
        {"version": 1, "generatedAt": generated_at, "items": list(items)},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


def batches(
    items: Sequence[dict[str, Any]],
    *,
    max_items: int = MAX_ITEMS,
    target_bytes: int = TARGET_BODY_BYTES,
) -> Iterator[list[dict[str, Any]]]:
    """Split into bodies the route will accept, by COUNT and by SIZE.

    A single item over the target is still sent alone rather than dropped: the ceiling that matters
    is the far end's 8 MiB, and one long article is well inside it. Dropping a story for being long
    would quietly lose exactly the live blogs and long reads the rail most wants.
    """
    batch: list[dict[str, Any]] = []
    size = 0
    for item in items:
        weight = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if batch and (len(batch) >= max_items or size + weight > target_bytes):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += weight
    if batch:
        yield batch


def post(url: str, secret: str, body: bytes, *, compress: bool = True, timeout: float = 60.0) -> dict[str, Any]:
    """One signed POST. Returns the parsed response.

    Compression is applied AFTER signing, never before, because the signature covers the content.
    """
    sent = headers(secret, body)
    payload = body
    if compress and len(body) > 1024:
        payload = gzip.compress(body)
        sent["content-encoding"] = "gzip"
    if len(payload) > MAX_WIRE_BYTES:
        raise ValueError(f"body is {len(payload)} bytes on the wire, over the {MAX_WIRE_BYTES} cap")

    request = urllib.request.Request(url, data=payload, headers=sent, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RailRefused(error.code, detail) from None


class RailRefused(Exception):
    """The route answered with a status that is not a success.

    The status says what to do, and the difference matters enough to name here:
    404 the secret is unset on the box, 401 ours is wrong or the clock has drifted, 413 the body is
    too big, 422 the body is malformed, 503 the maintenance curtain is up and the push should be
    retried later with the cursor UNCHANGED, because a refused push never advances it.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def probe(url: str, secret: str, generated_at: str) -> str:
    """Ask the box what it already holds. An empty cursor means "send everything".

    A batch with no items is valid and returns the cursor, which is how a cold start asks without
    needing a second, separately authenticated GET. After the box restarts the answer is "" again,
    because the store is process memory, and the recovery path is the first-run path.
    """
    answer = post(url, secret, encode([], generated_at), compress=False)
    return str(answer.get("cursor") or "")


def rows_since(store: Store, cursor: str, *, extract_hash: str, limit: int) -> list[dict[str, Any]]:
    """Stories whose `last_seen_at` is after the cursor, oldest first, with their sections.

    Ordering by `last_seen_at` is what makes the cursor work: the far end returns the high-water
    mark of what it ACCEPTED, so resuming from it can never skip a row it refused to store.
    """
    rows = [dict(row) for row in store.query(ITEM_SQL, (extract_hash, cursor, limit))]
    if not rows:
        return []

    marks = ",".join("?" for _ in rows)
    sections: dict[str, list[str]] = {}
    for story_id, section in store.query(
        f"SELECT story_id, section FROM story_sections WHERE story_id IN ({marks})"
        " ORDER BY story_id, section",
        [row["story_id"] for row in rows],
    ):
        sections.setdefault(story_id, []).append(section)

    for row in rows:
        row["sections"] = sections.get(row["story_id"], [])
        event = build_event(row)
        row["event_fingerprint"] = (
            json.dumps(event, sort_keys=True, ensure_ascii=False) if event else None
        )
        row["item_hash"] = item_hash(row)
    return rows


def add_arguments(parser: "argparse.ArgumentParser") -> None:
    parser.add_argument(
        "--limit", type=int, default=2000,
        help="Most stories to read in one run. Sent in batches under the route's own caps.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Ask the box for its cursor and say what would be sent, without sending anything. "
             "The probe itself is a real signed request, so this still proves the secret works.",
    )
    parser.add_argument(
        "--no-text", action="store_true",
        help="Leave article text out. The far end never serves text, and uses it to cluster, so "
             "this makes the batches smaller and the clustering worse.",
    )
    parser.add_argument(
        "--from-scratch", action="store_true",
        help="Ignore the box's cursor and send the whole window. For a box that lost its memory "
             "and reported a cursor anyway, which should not happen.",
    )


def run(args: "argparse.Namespace", settings: "Settings | None" = None) -> int:
    from .config import extract_config_hash
    from .settings import load

    settings = settings or load()
    if not settings.ingest_url or not settings.ingest_secret:
        print(
            "rail: NEWSFEED_INGEST_URL and NEWSFEED_INGEST_SECRET are not both set.\n"
            "      The secret is the same NEWS_INGEST_SECRET the Provenance box holds.\n"
            "      Without it every push answers 401 and nothing reaches the rail."
        )
        return 2

    url = settings.ingest_url.rstrip("/")
    if not url.endswith(PATH):
        url = url + PATH
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    extract_hash = extract_config_hash()

    try:
        cursor = "" if args.from_scratch else probe(url, settings.ingest_secret, generated_at)
    except RailRefused as refused:
        print(f"rail: the box refused the cursor probe, {refused}")
        return 1
    except OSError as error:
        print(f"rail: could not reach the box, {error}")
        return 1

    print(f"rail: box cursor {cursor or '(empty, it holds nothing)'}")

    with Store(settings.news_db) as store:
        rows = rows_since(store, cursor, extract_hash=extract_hash, limit=args.limit)
    items = [build_item(row, send_text=not args.no_text) for row in rows]
    if not items:
        print("rail: nothing new to send")
        return 0

    planned = list(batches(items))
    print(f"rail: {len(items)} stories to send in {len(planned)} batch(es)")
    if args.dry_run:
        for index, batch in enumerate(planned, 1):
            size = len(encode(batch, generated_at))
            print(f"  batch {index}: {len(batch)} items, {size / 1024:.0f} KiB uncompressed")
        return 0

    sent = stored = dropped = 0
    for index, batch in enumerate(planned, 1):
        try:
            answer = post(url, settings.ingest_secret, encode(batch, generated_at))
        except RailRefused as refused:
            # The cursor is the box's, never ours, so stopping here loses nothing: the next run
            # asks again and resumes from what it actually accepted.
            print(f"  batch {index}: refused, {refused}")
            return 1
        except OSError as error:
            print(f"  batch {index}: could not reach the box, {error}")
            return 1
        sent += answer.get("received", 0)
        stored += answer.get("stored", 0)
        dropped += answer.get("dropped", 0)
        refused_ids = answer.get("droppedIds") or []
        print(
            f"  batch {index}: received {answer.get('received')}, stored {answer.get('stored')}"
            f" (new {answer.get('accepted')}, unchanged {answer.get('unchanged')}),"
            f" box holds {answer.get('held')}"
        )
        if refused_ids:
            # Refused for their SHAPE, so they will be refused again. Log and move on: resending a
            # row that cannot parse is an infinite loop with extra steps.
            print(f"            refused permanently: {', '.join(refused_ids[:10])}")

    print(f"rail: sent {sent}, stored {stored}, dropped {dropped}")
    return 1 if dropped else 0
