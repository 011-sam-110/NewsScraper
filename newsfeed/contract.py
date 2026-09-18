"""`provenance.newsfeed/1`: what a valid snapshot is, read off section 8.1 and nothing else.

This is written from the CONTRACT TABLE, deliberately not from `publish.py`. The builder and its
tests were written together, so they agree with each other by construction and cannot catch a field
that both of them get wrong in the same way. This module is the second reading: it knows what the
document says a body must look like and knows nothing about how one is made.

It is also the specification Provenance's own `validateSnapshot()` has to match (section 9.1, pull
request B). Two readings of one table is the point, not duplication: when they disagree, one of them
is wrong, and the disagreement is visible instead of silent.

Nothing here parses or repairs. It answers one question, with reasons.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

SCHEMA = "provenance.newsfeed/1"

SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{16}$")
ITEM_ID = re.compile(r"^nf_[0-9a-f]{12}$")
# Section 8.1 says "UTC ISO 8601". Every time in the section 8.2 example ends in Z, and a body that
# mixed offsets would be read by a browser as local time, so Z is required, not merely allowed.
UTC_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
EVENT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

OUTLETS = ("reuters", "bbc", "guardian", "pbs", "nyt")
PRECISIONS = ("point", "district", "city")

MAX_ITEMS = 3000
MIN_REPORTS = 1
MAX_REPORTS = 8
MAX_TITLE = 300
MAX_URL = 600
MAX_EVIDENCE = 200
MAX_PLACE = 120
MAX_DECIMALS = 5

PIN_WINDOW = timedelta(days=7)
WORLD_WINDOW = timedelta(hours=72)


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _decimals(value: float) -> int:
    """Digits after the point, as `json.dumps` would write the number.

    `repr` is what the JSON encoder emits, so counting its digits counts the wire rather than the
    binary float behind it. An int stays an int, because the encoder writes 51 and not 51.0.
    `Decimal` rather than a string split because `repr` may hand back exponent form, and `1e-05` is
    five decimal places, not a malformed number.
    """
    if isinstance(value, int):
        return 0
    return max(0, -Decimal(repr(value)).as_tuple().exponent)


def _check_location(location: Any, where: str, problems: list[str]) -> None:
    if not isinstance(location, dict):
        problems.append(f"{where}.location is neither an object nor null")
        return

    expected = {"lat", "lon", "precision", "place", "geonamesId", "evidence", "evidenceOutlet"}
    if missing := expected - set(location):
        problems.append(f"{where}.location is missing {sorted(missing)}")
    if extra := set(location) - expected:
        problems.append(f"{where}.location carries fields 8.1 does not name: {sorted(extra)}")

    for axis, low, high in (("lat", -90, 90), ("lon", -180, 180)):
        value = location.get(axis)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{where}.location.{axis} is not a number: {value!r}")
        elif not low <= value <= high:
            problems.append(f"{where}.location.{axis} is outside {low}..{high}: {value}")
        elif _decimals(value) > MAX_DECIMALS:
            problems.append(f"{where}.location.{axis} has over {MAX_DECIMALS} decimals: {value}")

    if location.get("precision") not in PRECISIONS:
        problems.append(
            f"{where}.location.precision is {location.get('precision')!r}, not one of {PRECISIONS}"
        )

    place = location.get("place")
    if not isinstance(place, str) or not place.strip():
        problems.append(f"{where}.location.place is missing or empty")
    elif len(place) > MAX_PLACE:
        problems.append(f"{where}.location.place is {len(place)} characters, over {MAX_PLACE}")

    geonames_id = location.get("geonamesId")
    if not isinstance(geonames_id, int) or isinstance(geonames_id, bool) or geonames_id <= 0:
        problems.append(f"{where}.location.geonamesId is not a positive integer: {geonames_id!r}")

    # 8.1 types evidence as a string and offers no "or null". A pin with no quote is also a pin no
    # reader can check, which section 6 requires of every one of them.
    evidence = location.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        problems.append(f"{where}.location.evidence is missing or empty")
    elif len(evidence) > MAX_EVIDENCE:
        problems.append(
            f"{where}.location.evidence is {len(evidence)} characters, over {MAX_EVIDENCE}"
        )

    if location.get("evidenceOutlet") not in OUTLETS:
        problems.append(f"{where}.location.evidenceOutlet is {location.get('evidenceOutlet')!r}")


def _check_reports(reports: Any, where: str, problems: list[str]) -> None:
    if not isinstance(reports, list):
        problems.append(f"{where}.reports is not an array")
        return
    if not MIN_REPORTS <= len(reports) <= MAX_REPORTS:
        problems.append(
            f"{where}.reports holds {len(reports)}, outside {MIN_REPORTS}..{MAX_REPORTS}"
        )

    published: list[str] = []
    objects = [report for report in reports if isinstance(report, dict)]
    for index, report in enumerate(reports):
        spot = f"{where}.reports[{index}]"
        if not isinstance(report, dict):
            problems.append(f"{spot} is not an object")
            continue
        if set(report) != {"outlet", "headline", "url", "publishedAt"}:
            problems.append(f"{spot} fields are {sorted(report)}")
        if report.get("outlet") not in OUTLETS:
            problems.append(f"{spot}.outlet is {report.get('outlet')!r}")

        headline = report.get("headline")
        if not isinstance(headline, str) or not headline.strip():
            problems.append(f"{spot}.headline is missing or empty")
        elif len(headline) > MAX_TITLE:
            problems.append(f"{spot}.headline is {len(headline)} characters, over {MAX_TITLE}")

        url = report.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"{spot}.url is not an http or https url: {url!r}")
        elif len(url) > MAX_URL:
            problems.append(f"{spot}.url is {len(url)} characters, over {MAX_URL}")

        stamp = report.get("publishedAt")
        if not isinstance(stamp, str) or not UTC_ISO.match(stamp):
            problems.append(f"{spot}.publishedAt is not UTC ISO 8601 ending in Z: {stamp!r}")
        else:
            published.append(stamp)

    # "lead report first", and 8.1 defines the lead as the founder, the EARLIEST published report.
    # A body that puts a later report first names the wrong outlet as the one that broke the story.
    if published and published[0] != min(published):
        problems.append(f"{where}.reports[0] is not the earliest published report")

    if len({(report.get("outlet"), report.get("url")) for report in objects}) != len(objects):
        problems.append(f"{where}.reports carries the same outlet and url twice")


def _check_item(item: Any, index: int, categories: frozenset[str], problems: list[str]) -> None:
    where = f"items[{index}]"
    if not isinstance(item, dict):
        problems.append(f"{where} is not an object")
        return

    expected = {"id", "category", "title", "firstReportedAt", "lastReportedAt", "eventDate",
                "location", "reports"}
    if missing := expected - set(item):
        problems.append(f"{where} is missing {sorted(missing)}")
    if extra := set(item) - expected:
        problems.append(f"{where} carries fields 8.1 does not name: {sorted(extra)}")

    item_id = item.get("id")
    if not isinstance(item_id, str) or not ITEM_ID.match(item_id):
        problems.append(f"{where}.id is not nf_ plus 12 hex characters: {item_id!r}")

    if item.get("category") not in categories:
        problems.append(f"{where}.category is {item.get('category')!r}, not a v1 category")

    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append(f"{where}.title is missing or empty")
    elif len(title) > MAX_TITLE:
        problems.append(f"{where}.title is {len(title)} characters, over {MAX_TITLE}")

    for field in ("firstReportedAt", "lastReportedAt"):
        stamp = item.get(field)
        if not isinstance(stamp, str) or not UTC_ISO.match(stamp):
            problems.append(f"{where}.{field} is not UTC ISO 8601 ending in Z: {stamp!r}")
    first, last = item.get("firstReportedAt"), item.get("lastReportedAt")
    if isinstance(first, str) and isinstance(last, str) and first > last:
        problems.append(f"{where} was first reported after it was last reported")

    event_date = item.get("eventDate")
    if event_date is not None and (
        not isinstance(event_date, str) or not EVENT_DATE.match(event_date)
    ):
        problems.append(f"{where}.eventDate is neither null nor YYYY-MM-DD: {event_date!r}")

    if item.get("location") is not None:
        _check_location(item["location"], where, problems)

    _check_reports(item.get("reports"), where, problems)

    # title is "the lead report's headline, verbatim", and 8.1 goes on to say that title and
    # firstReportedAt then describe the same report. Both are cross-field claims, so a body can
    # satisfy every single-field rule above and still contradict itself.
    reports = item.get("reports")
    if isinstance(reports, list) and reports and isinstance(reports[0], dict):
        lead = reports[0]
        if isinstance(title, str) and lead.get("headline") != title:
            problems.append(f"{where}.title is not the lead report's headline, verbatim")
        if isinstance(first, str) and lead.get("publishedAt") != first:
            problems.append(f"{where}.firstReportedAt is not when the lead report was published")


def validate(
    body: Any,
    categories: frozenset[str] | None = None,
    *,
    check_windows: bool = True,
) -> list[str]:
    """Every way this body breaks section 8.1. An empty list means it is valid.

    Every problem is reported, not just the first, because a sender wants one list to work from
    rather than one round trip per field.

    `check_windows` applies the 7 day and 72 hour rules against the body's own `generatedAt`. It is
    separable because it is the one rule in 8.1 about the sender rather than about the shape, so a
    reader handed an old body on purpose can still check everything else.
    """
    if categories is None:
        from .taxonomy import CATEGORIES

        categories = frozenset(entry.id for entry in CATEGORIES)

    problems: list[str] = []
    if not isinstance(body, dict):
        return ["the body is not a JSON object"]

    expected = {"schema", "snapshotId", "generatedAt", "dataAsOf", "pinsWithheld",
                "worldNewsWithheld", "outlets", "items"}
    if missing := expected - set(body):
        problems.append(f"the body is missing {sorted(missing)}")
    if extra := set(body) - expected:
        problems.append(f"the body carries fields 8.1 does not name: {sorted(extra)}")

    if body.get("schema") != SCHEMA:
        problems.append(f"schema is {body.get('schema')!r}, not {SCHEMA!r}")

    snapshot_id = body.get("snapshotId")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.match(snapshot_id):
        problems.append(f"snapshotId is not snap_ plus 16 hex characters: {snapshot_id!r}")

    # 8.1 types both of these as a string and offers no "or null" for either.
    for field in ("generatedAt", "dataAsOf"):
        stamp = body.get(field)
        if not isinstance(stamp, str) or not UTC_ISO.match(stamp):
            problems.append(f"{field} is not UTC ISO 8601 ending in Z: {stamp!r}")

    for flag in ("pinsWithheld", "worldNewsWithheld"):
        if not isinstance(body.get(flag), bool):
            problems.append(f"{flag} is not a boolean: {body.get(flag)!r}")

    outlets = body.get("outlets")
    if not isinstance(outlets, list):
        problems.append("outlets is not an array")
    else:
        named: list[Any] = []
        for index, row in enumerate(outlets):
            if not isinstance(row, dict):
                problems.append(f"outlets[{index}] is not an object")
                continue
            if set(row) != {"outlet", "lastNewStoryAt"}:
                problems.append(f"outlets[{index}] fields are {sorted(row)}")
            if row.get("outlet") not in OUTLETS:
                problems.append(f"outlets[{index}].outlet is {row.get('outlet')!r}")
            named.append(row.get("outlet"))
            last = row.get("lastNewStoryAt")
            if last is not None and (not isinstance(last, str) or not UTC_ISO.match(last)):
                problems.append(f"outlets[{index}].lastNewStoryAt is not UTC ISO 8601: {last!r}")
        # "One row per outlet": every outlet once, and no outlet twice.
        for outlet in OUTLETS:
            if named.count(outlet) != 1:
                problems.append(f"outlets names {outlet} {named.count(outlet)} times, not once")

    items = body.get("items")
    if not isinstance(items, list):
        problems.append("items is not an array")
        return problems
    if len(items) > MAX_ITEMS:
        problems.append(f"items holds {len(items)}, over the {MAX_ITEMS} cap")

    seen: set[str] = set()
    for index, item in enumerate(items):
        _check_item(item, index, categories, problems)
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            if item["id"] in seen:
                problems.append(f"items[{index}].id {item['id']} appears more than once")
            seen.add(item["id"])

    if body.get("pinsWithheld") is True and any(
        isinstance(item, dict) and item.get("location") for item in items
    ):
        problems.append("pinsWithheld is true and the body still carries a located item")
    if body.get("worldNewsWithheld") is True and any(
        isinstance(item, dict) and "location" in item and item["location"] is None
        for item in items
    ):
        problems.append("worldNewsWithheld is true and the body still carries an unlocated item")

    generated = body.get("generatedAt")
    if check_windows and isinstance(generated, str) and UTC_ISO.match(generated):
        floor = _moment(generated)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            last = item.get("lastReportedAt")
            if not isinstance(last, str) or not UTC_ISO.match(last):
                continue
            window = PIN_WINDOW if item.get("location") else WORLD_WINDOW
            if _moment(last) < floor - window:
                kind = "pin" if item.get("location") else "World news item"
                problems.append(
                    f"items[{index}] is a {kind} last reported {last}, outside its window"
                )

    return problems
