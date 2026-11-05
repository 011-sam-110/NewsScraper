"""Stage: headline-places. The places DeepSeek read out of the feed headlines, for Provenance's map.

    python -m newsfeed extract --feeds          # read each new feed row (headline and RSS summary)
    python -m newsfeed headline-places --dry-run
    python -m newsfeed headline-places          # one signed relay snapshot to Provenance

WHAT IT SENDS. One signed snapshot to Provenance's relay (`POST /api/relay/headline-places`, the same
route and scheme as the war feeds). Each item is a feed headline's URL and the place its story happened,
and nothing else a model wrote:

- the place is a GeoNames row (name, coordinates, precision), never the model's own words or numbers;
- the quote is the outlet's own text, checked word for word by extract's quote check;
- only a physical happening that passed every extract check, at point, district or city precision.
  A region or a country is never sent, a place that resolve could only find by climbing to its
  container is never sent, and a place the model could not name is no item at all.

Provenance's Headline sources layer joins these to its own stream by URL. A headline with no item
here has no mark there. The layer uses this feed only when its switch is on, and the switch waits for
the labelled gate (docs/ARCHITECTURE.md section 10) measured with the final settings.

Feed rows cross as headline and link only (CLAUDE.md): the snapshot carries the URL, the quote that
proves the place, and the place. It carries no headline, no summary and no article text.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from scraper import DROPPED_OUTLETS

from . import relay
from .config import DEFAULT_MODEL, extract_config_hash
from .settings import Settings, SettingsError, environment, load
from .store import STATUS_FEED, Store

FEED = "headline-places"
WINDOW_HOURS = 48
MAX_ITEMS = 3000
QUOTE_MAX = 500

# resolve's precision to Provenance's place level, as the war news sender maps it.
PRECISION = {"point": "facility", "district": "area", "city": "area"}


def placed_rows(store: Store, config_hash: str, since: str) -> list[dict[str, Any]]:
    """Feed rows in the window whose extraction under this hash is a placed physical happening."""
    rows = store.query(
        """SELECT s.story_id, s.outlet, s.url, s.source_url, s.first_seen_at,
                  e.place_name, e.place_within, e.place_country, e.place_kind, e.place_quote, e.created_at
             FROM stories s
             JOIN extractions e ON e.story_id = s.story_id AND e.config_hash = ?
            WHERE s.status = ? AND s.first_seen_at >= ?
              AND e.is_physical_event = 1 AND e.failure_reason IS NULL
              AND e.place_name IS NOT NULL AND e.place_quote IS NOT NULL
            ORDER BY s.first_seen_at DESC""",
        (config_hash, STATUS_FEED, since),
    )
    return [dict(row) for row in rows if row["outlet"] not in DROPPED_OUTLETS]


def build_items(rows: list[dict[str, Any]], place_of) -> list[dict[str, Any]]:
    """One item per URL, newest first. `place_of(name, within, country, kind)` returns the item's
    place, or None when it is no place to draw (not found, climbed, or not pinnable).

    The URL is the story's canonical one (identity.canonical_url), the same address the news rail
    sends. Provenance reduces its own copy of the link the same way before it joins the two, so the
    outlet's tracking query ("?traffic_source=rss") does not stop a match. It is a join key only:
    Provenance links the article from its own stream, so an http link (Africanews) is kept."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = row.get("url") or row.get("source_url")
        if not url or not url.startswith(("https://", "http://")) or url in seen:
            continue
        place = place_of(row["place_name"], row["place_within"], row["place_country"], row["place_kind"])
        if place is None:
            continue
        seen.add(url)
        items.append({
            "url": url,
            "place": place,
            "quote": (row["place_quote"] or "")[:QUOTE_MAX],
            "extractedAt": row["created_at"],
        })
        if len(items) >= MAX_ITEMS:
            break
    return items


def gazetteer_placer(gazetteer: Any):
    """resolve.py over the pipeline's gazetteer. Read-only. A climbed answer is no place."""
    from .resolve import resolve_place

    def place_of(name: str, within: str | None, country: str | None, kind: str | None) -> dict[str, Any] | None:
        result = resolve_place(gazetteer, name, within, country, kind)
        if not result.pinnable or result.place is None or result.climbed:
            return None
        found = result.place
        level = PRECISION.get(found.precision or "")
        if level is None:
            return None
        return {
            "id": int(found.geonames_id),
            "name": gazetteer.display_name(found)[:200],
            "lat": round(found.latitude, 4),
            "lon": round(found.longitude, 4),
            "precision": level,
        }

    return place_of


def snapshot(items: list[dict[str, Any]], config_hash: str, model: str) -> dict[str, Any]:
    return {"items": items, "configHash": config_hash, "model": model}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hours", type=int, default=WINDOW_HOURS, help="How far back the snapshot reaches.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="The model whose answers are read.")
    parser.add_argument("--dry-run", action="store_true", help="Build the snapshot and print a summary. Send nothing.")


def run(args: argparse.Namespace, settings: Settings | None = None) -> int:
    from .geonames import Gazetteer

    settings = settings or load()
    config_hash = extract_config_hash(args.model)
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        gazetteer = Gazetteer.open(settings.geonames_db)
    except FileNotFoundError as error:
        print(f"No gazetteer: {error}", file=sys.stderr)
        return 2
    try:
        with Store(settings.news_db) as store:
            rows = placed_rows(store, config_hash, since)
        items = build_items(rows, gazetteer_placer(gazetteer))
    finally:
        gazetteer.close()

    data = snapshot(items, config_hash, args.model)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = relay.body_bytes(relay.snapshot_envelope(FEED, fetched_at, data))
    print(
        f"config {config_hash}  {len(rows)} placed feed rows in {args.hours} h  "
        f"{len(items)} items with a GeoNames place  {len(body)} bytes",
        file=sys.stderr,
    )
    if args.dry_run:
        for item in items[:10]:
            place = item["place"]
            print(f"  {place['precision']:<8} {place['name'][:40]:<40} {item['url'][:80]}", file=sys.stderr)
        return 0

    env = environment()
    secret = env.get("RELAY_SIGNING_SECRET") or ""
    base = env.get("PROVENANCE_RELAY_URL") or ""
    try:
        url = relay.feed_url(base, FEED)
        answer = relay.post(url, secret, body)
    except relay.SigningError as error:
        print(f"Not sent: {error}", file=sys.stderr)
        return 2
    except relay.RelayRefused as error:
        print(f"Refused by the relay: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Not sent: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"sent {len(items)} items: {answer}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m newsfeed headline-places", description=__doc__)
    add_arguments(parser)
    try:
        return run(parser.parse_args(argv))
    except SettingsError as error:
        print(f"Settings: {error}", file=sys.stderr)
        return 2
