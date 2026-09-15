"""Helpers for feed-only sources: sources that read the outlet's public RSS feeds and nothing else.

A feed-only source defines no article_details, so common.scrape never requests an article page
for it, and each row's text stays None. A feed has no pages: page 0 returns every item and
later pages return [] without a request.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from typing import Any
from urllib.parse import urlsplit

from . import common

FEED_ACCEPT = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"
# "A, B and C", "A, B, and C" and "A and B" as written in one dc:creator element.
AUTHOR_SEPARATOR = re.compile(r",\s*and\s+|,\s*|\s+and\s+")


def fetch_items(outlet: str, url: str) -> list[dict[str, Any]]:
    """Every item in one feed, as common.parse_rss gives it, plus media credits nested in media:content."""
    try:
        document = common.http_get(url, FEED_ACCEPT)
        items = common.parse_rss(document)
        nested = nested_media_text(document)
    except common.FetchError as error:
        raise RuntimeError(f"{outlet}: {error}") from error
    except ElementTree.ParseError as error:
        raise RuntimeError(f"{outlet}: the feed at {url} is not valid XML: {error}") from error
    for item, (credit, description) in zip(items, nested):
        item["media_credit"] = item["media_credit"] or credit
        item["media_description"] = item["media_description"] or description
    return items


def nested_media_text(document: bytes) -> list[tuple[str | None, str | None]]:
    """(media:credit, media:description) at any depth in each item. Some feeds put them inside media:content."""
    results = []
    for item in ElementTree.fromstring(document).iter("item"):
        values = []
        for tag in ("media:credit", "media:description"):
            node = item.find(f".//{tag}", common.RSS_NAMESPACES)
            values.append(node.text.strip() if node is not None and node.text and node.text.strip() else None)
        results.append((values[0], values[1]))
    return results


def url_section(url: str | None) -> list[str]:
    """The outlet's own section path in a story URL: the segments before the date, id or slug.

    /2026/09/10/arts/design/slug.html -> [arts, design]; /world/middle-east/slug -> [world, middle-east];
    /middle-east-and-africa/2026/09/13/slug -> [middle-east-and-africa].
    """
    if not url:
        return []
    segments = [segment for segment in urlsplit(url).path.split("/") if segment][:-1]
    while segments and segments[0].isdigit():
        segments.pop(0)
    section = []
    for segment in segments:
        if segment.isdigit():
            break
        section.append(segment)
    return section


def split_authors(creators: list[str]) -> list[str]:
    """One name per author, from dc:creator values that may join several names or be a bare "and"."""
    names = []
    for creator in creators:
        for name in AUTHOR_SEPARATOR.split(creator):
            name = name.strip()
            if name and name.lower() != "and":
                names.append(name)
    return list(dict.fromkeys(names))


def base_row(item: dict[str, Any], feed_url: str, url: str | None = None) -> dict[str, Any]:
    """A row with every field the feed item carries. Sources set categories."""
    url = url or item["link"]
    authors = split_authors(item["creators"])
    section = url_section(url)
    row = common.empty_row()
    row.update(
        id=item["guid"] or url,
        guid=item["guid"],
        headline=item["title"],
        description=item["description"],
        published=item["published"],
        url=url,
        thumbnail=item["media"][0]["url"] if item["media"] else None,
        author=authors[0] if authors else None,
        authors=authors,
        media=item["media"],
        media_credit=item["media_credit"],
        media_description=item["media_description"],
        url_section="/".join(section) or None,
        feed_url=feed_url,
    )
    row["categories"] = {"url_section": section}
    return row
