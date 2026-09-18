"""Stage: publish (M10). Build the `provenance.newsfeed/1` snapshot from the store.

This module is the BUILDER only. Sending it is section 7.10 steps 2 to 6 and is not here yet,
because the route it posts to does not exist on the box: Provenance pull request B (section 9.1) is
unwritten and `/api/ingest/newsfeed` answers 404 today. Building and sending are separated on
purpose, so the part that can be tested offline against fixtures is tested that way, and the part
that needs a live box is the only thing waiting on one.

Nothing a model wrote reaches the body. Titles and headlines are the outlets' own, verbatim.
Coordinates come from GeoNames. `place` is built by the gazetteer from GeoNames name tables, never
by the model. The single quoted string is `evidence`, which is the extraction's verbatim quote from
the article, trimmed. Section 3, and section 8.1's "Never in the body".
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .geonames import Gazetteer
from .store import Store

SCHEMA = "provenance.newsfeed/1"

# Section 8.1. A pin stays for 7 days from lastReportedAt, a World news item for 72 hours.
PIN_WINDOW = timedelta(days=7)
WORLD_WINDOW = timedelta(hours=72)

MAX_ITEMS = 3000
MAX_REPORTS = 8
MAX_TITLE = 300
MAX_URL = 600
MAX_EVIDENCE = 200

OUTLETS = ("reuters", "bbc", "guardian", "pbs", "nyt")

# Section 8.1 allows only these three. Region and country are never pinned (section 6).
PINNABLE_PRECISIONS = frozenset({"point", "district", "city"})


def _utc(value: str | None) -> datetime | None:
    """Parse a stored timestamp, or None. Everything in the store is written as UTC ISO 8601."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """A UTC ISO 8601 string ending in Z, which is the only form the contract uses."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trim_evidence(quote: str | None, place_name: str | None) -> str | None:
    """The verbatim quote, trimmed around the place name to at most MAX_EVIDENCE characters.

    Nothing is added. No ellipsis, no marker, no rewording: a reader is told this is a quotation
    from the article, so every character in it has to be one the outlet wrote. A shortened quote is
    still verbatim; a quote with punctuation bolted on is not.

    The window is centred on the place name, because the place name is the whole reason the quote
    is carried: section 6 requires the pin's evidence to contain it. A quote that does not contain
    it keeps its first MAX_EVIDENCE characters, since nothing better is known.
    """
    if not quote:
        return None
    text = " ".join(quote.split())
    if len(text) <= MAX_EVIDENCE:
        return text

    start = 0
    if place_name:
        found = text.lower().find(place_name.strip().lower())
        if found >= 0:
            middle = found + len(place_name.strip()) // 2
            start = max(0, middle - MAX_EVIDENCE // 2)
            start = min(start, len(text) - MAX_EVIDENCE)

    window = text[start : start + MAX_EVIDENCE]
    # Snap to word boundaries so the quote never ends mid-word, but never so hard that the place
    # name itself is cut off: only whole words at the edges are given up.
    if start > 0 and " " in window:
        head = window.split(" ", 1)[1]
        if place_name and place_name.strip().lower() in head.lower():
            window = head
    if start + MAX_EVIDENCE < len(text) and " " in window:
        tail = window.rsplit(" ", 1)[0]
        if not place_name or place_name.strip().lower() in tail.lower():
            window = tail
    return window.strip()


def snapshot_id(body: dict[str, Any]) -> str:
    """`snap_` plus 16 hex characters, changing whenever the CONTENT changes.

    `snapshotId` and `generatedAt` are left out of the digest. Both change on every build by
    definition, so including either would make every snapshot look new and defeat section 7.10's
    "send only when the content changed".
    """
    content = {key: value for key, value in body.items() if key not in ("snapshotId", "generatedAt")}
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"snap_{digest[:16]}"


def _reports(connection: sqlite3.Connection, cluster_id: str, config_hash: str) -> list[dict[str, Any]]:
    """The cluster's reports, lead first, at most MAX_REPORTS.

    Section 8.1 defines the lead as the earliest PUBLISHED story, not the first one the pipeline
    inserted, so this orders by published time and never reads the founder column. Those two are
    not the same thing: see the note on `title` in `_items`.

    A report with no headline, no url, or a url that is not http is left out rather than sent, so
    the first report here is the earliest published report the body can actually carry.
    """
    rows = connection.execute(
        "SELECT s.outlet, s.headline, s.url, s.published"
        " FROM cluster_members m JOIN stories s ON s.story_id = m.story_id"
        " WHERE m.cluster_id = ? AND m.config_hash = ?"
        " ORDER BY s.published IS NULL, s.published, s.story_id",
        (cluster_id, config_hash),
    ).fetchall()

    reports: list[dict[str, Any]] = []
    for outlet, headline, url, published in rows[:MAX_REPORTS]:
        if not headline or not url:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        reports.append(
            {
                "outlet": outlet,
                "headline": headline[:MAX_TITLE],
                "url": url[:MAX_URL],
                "publishedAt": _iso(_utc(published)),
            }
        )
    return reports


def _location(
    gazetteer: Gazetteer,
    geonames_id: int | None,
    latitude: float | None,
    longitude: float | None,
    precision: str | None,
    quote: str | None,
    place_name: str | None,
    evidence_outlet: str | None,
) -> dict[str, Any] | None:
    """Section 8.1 `location`, or None when this cannot be a pin.

    The display name is read from the gazetteer at BUILD time rather than stored with the
    resolution, so a gazetteer rebuild that improves a name reaches every existing pin without
    re-resolving anything.
    """
    if geonames_id is None or latitude is None or longitude is None:
        return None
    if precision not in PINNABLE_PRECISIONS:
        return None
    place = gazetteer.place(geonames_id)
    if place is None:
        return None
    return {
        "lat": round(latitude, 5),
        "lon": round(longitude, 5),
        "precision": precision,
        "place": gazetteer.display_name(place),
        "geonamesId": geonames_id,
        "evidence": trim_evidence(quote, place_name),
        "evidenceOutlet": evidence_outlet,
    }


def outlets_block(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per outlet, in the contract's order, whether or not the store has heard from it.

    A missing outlet is reported as null rather than left out. Section 7.11 alerts on outlet
    silence, and an outlet that vanished from the array would read as "nothing to say about it".
    """
    known = {
        outlet: last
        for outlet, last in connection.execute(
            "SELECT outlet, last_new_story_at FROM outlet_health"
        )
    }
    return [
        {"outlet": outlet, "lastNewStoryAt": _iso(_utc(known.get(outlet)))} for outlet in OUTLETS
    ]


def build_snapshot(
    store: Store,
    gazetteer: Gazetteer,
    *,
    cluster_hash: str,
    extract_hash: str,
    resolve_hash: str,
    now: datetime,
    pins_withheld: bool,
    world_news_withheld: bool,
) -> dict[str, Any]:
    """The whole section 8 body.

    `pins_withheld` and `world_news_withheld` come from the gate (section 10) and are passed in
    rather than read here, because the gate is a separate decision with its own evidence and this
    function must be testable without one. Withheld means the items are LEFT OUT and the flag is
    true, not that they are sent with a marker: the box renders what it is given.

    `dataAsOf` is the store's own `data_as_of`, not a second query written here. Two definitions
    of "the newest completed scrape run" that drift apart would make the snapshot disagree with
    `newsfeed status` about how fresh it is, and the disagreement would show up as a support
    question rather than as a failure.

    All three config hashes are passed in, the way section 7.8 passes them, and none is inferred
    from the store. The chain runs extract, then resolve, then cluster, and it only runs one way:
    a cluster hash cannot be turned back into the resolve and extract hashes it was built from.
    Guessing one is worse than it looks, because every join still SUCCEEDS with the wrong hash and
    simply matches nothing, so the item would be built with no category, no date and no quote, and
    no error would be raised anywhere.
    """
    connection = store.connection
    rows = connection.execute(
        "SELECT c.cluster_id, c.is_pin, c.latitude, c.longitude, c.place_precision,"
        "       e.category, e.event_date, e.place_quote, e.place_name, s.outlet,"
        "       r.geonames_id"
        " FROM clusters c"
        " JOIN stories s ON s.story_id = c.founder_story"
        " LEFT JOIN extractions e ON e.story_id = c.founder_story AND e.config_hash = ?"
        " LEFT JOIN resolutions r ON r.story_id = c.founder_story AND r.config_hash = ?"
        " WHERE c.config_hash = ?",
        (extract_hash, resolve_hash, cluster_hash),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        (
            cluster_id, is_pin, latitude, longitude, precision,
            category, event_date, quote, place_name, founder_outlet, geonames_id,
        ) = row

        reported = connection.execute(
            "SELECT MIN(s.published), MAX(s.published)"
            " FROM cluster_members m JOIN stories s ON s.story_id = m.story_id"
            " WHERE m.cluster_id = ? AND m.config_hash = ?",
            (cluster_id, cluster_hash),
        ).fetchone()
        first_reported, last_reported = _utc(reported[0]), _utc(reported[1])
        if last_reported is None:
            continue

        location = None
        if is_pin:
            location = _location(
                gazetteer, geonames_id, latitude, longitude, precision,
                quote, place_name, founder_outlet,
            )

        window = PIN_WINDOW if location else WORLD_WINDOW
        if now - last_reported > window:
            continue
        if location and pins_withheld:
            continue
        if not location and world_news_withheld:
            continue

        reports = _reports(connection, cluster_id, cluster_hash)
        if not reports:
            continue

        # The title is the LEAD REPORT's headline, and the lead is the first report in the body,
        # which is the earliest published one. It is deliberately not the founder's headline.
        #
        # The stored founder is the story that created the cluster, and that is usually also the
        # earliest published member, but not always: a story published earlier can be scraped and
        # extracted later, and it then joins a cluster somebody else founded. Measured on the live
        # store on 2026-09-18, this was true of 1 of the 68 multi-member clusters. That one was a
        # BBC report of NATO downing a drone over Lithuania, published 16 hours before the Guardian
        # piece that founded the cluster, so the item would have carried the Guardian's much broader
        # headline over the BBC's timestamp. The gap widens as the outlets fall further out of step,
        # and Reuters already runs on a three hour timer against everyone else's hour.
        #
        # Only the title moves. The category, the event date, the coordinates and the quote stay
        # with the founder, because section 7.8 makes the founder the single subject of the pin
        # decision, and a cluster with two notions of its own subject would be worse than one with
        # an unexpected title.
        items.append(
            {
                "id": cluster_id,
                "category": category,
                "title": reports[0]["headline"],
                "firstReportedAt": _iso(first_reported),
                "lastReportedAt": _iso(last_reported),
                "eventDate": event_date,
                "location": location,
                "reports": reports,
            }
        )

    # Newest first, then by id so the order is stable for two items reported at the same second.
    # An unstable order would change the snapshot id without the content changing.
    items.sort(key=lambda item: (item["lastReportedAt"], item["id"]), reverse=True)

    body: dict[str, Any] = {
        "schema": SCHEMA,
        "snapshotId": "",
        "generatedAt": _iso(now),
        "dataAsOf": _iso(_utc(store.data_as_of())),
        "pinsWithheld": pins_withheld,
        "worldNewsWithheld": world_news_withheld,
        "outlets": outlets_block(connection),
        "items": items[:MAX_ITEMS],
    }
    body["snapshotId"] = snapshot_id(body)
    return body
