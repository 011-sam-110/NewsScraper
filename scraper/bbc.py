"""BBC News: the content-collection API for listings, article pages for text and BBC's own topics."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

from . import common
from .common import FetchError

NAME = "bbc"
API_URL = "https://web-cdn.api.bbci.co.uk/xd/content-collection/"
SITE_ROOT = "https://www.bbc.com"
# The "Latest updates" collection on each www.bbc.com/news/<path> page, read from the page's
# __NEXT_DATA__ on 2026-09-14. Every id answered with total 100 on that date.
SECTIONS = {
    "world": "07cedf01-f642-4b92-821f-d7b324b8ba73",
    "africa": "f7905f4a-3031-4e07-ac0c-ad31eeb6a08e",
    "asia": "ec977d36-fc91-419e-a860-b151836c176b",
    "china": "3d085ce3-0533-4259-8318-b0d550529500",
    "india": "1a3cd4db-fe3d-46f2-9c9a-927a01b00c91",
    "australia": "3307dc97-b7f0-47be-a1fb-c988b447cc72",
    "europe": "e2cc1064-8367-4b1e-9fb7-aed170edc48f",
    "latin_america": "16d132f4-d562-4256-8b68-743fe23dab8c",
    "middle_east": "b08a1d2f-6911-4738-825a-767895b8bfc4",
    "us_and_canada": "db5543a3-7985-4b9e-8fe0-2ac6470ea45b",
    "uk": "27d91e93-c35c-4e30-87bf-1bd443496470",
}
# The section page that holds each collection, sent as the Referer.
SECTION_PAGES = {
    "world": "/news/world",
    "africa": "/news/world/africa",
    "asia": "/news/world/asia",
    "china": "/news/world/asia/china",
    "india": "/news/world/asia/india",
    "australia": "/news/world/australia",
    "europe": "/news/world/europe",
    "latin_america": "/news/world/latin_america",
    "middle_east": "/news/world/middle_east",
    "us_and_canada": "/news/world/us_and_canada",
    "uk": "/news/uk",
}
# The API sends at most 100 items for a page, and at most 100 items for a collection.
MAX_PAGE_SIZE = 100
NEXT_DATA_PATTERN = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def build_url(collection_id: str, page: int, size: int) -> str:
    return f"{API_URL}{collection_id}?{urlencode({'page': page, 'size': size})}"


def clean_string(value: Any) -> str | None:
    return common.clean_text(value) or None if isinstance(value, str) else None


def normalise_item(item: dict[str, Any], collection_id: str) -> dict[str, Any]:
    image = (((item.get("indexImage") or {}).get("model") or {}).get("blocks")) or {}
    item_id = item.get("id")
    path = item.get("path")
    topics = item.get("topics") if isinstance(item.get("topics"), list) else []
    return {
        **common.empty_row(),
        "id": item_id,
        "asset_id": item_id.rsplit(":", 1)[-1] if isinstance(item_id, str) else None,
        # BBC titles and alt text can carry leading or trailing spaces.
        "headline": clean_string(item.get("title")),
        "description": clean_string(item.get("summary")),
        "published": item.get("firstPublishedAt"),
        "updated": item.get("lastPublishedAt"),
        "url": urljoin(SITE_ROOT, path) if path else None,
        "path": path,
        "thumbnail": image.get("src"),
        "thumbnail_alt": clean_string(image.get("altText")),
        "thumbnail_width": image.get("width"),
        "thumbnail_height": image.get("height"),
        "type": item.get("type"),
        "subtype": item.get("subtype"),
        "state": item.get("state"),
        "collection_id": collection_id,
        "categories": {"listing_topics": [topic for topic in topics if isinstance(topic, str)]},
    }


def list_page(section: str, page: int, size: int) -> list[dict[str, Any]]:
    collection_id = SECTIONS[section]
    url = build_url(collection_id, page, max(1, min(size, MAX_PAGE_SIZE)))
    try:
        payload = common.get_json(url, referer=urljoin(SITE_ROOT, SECTION_PAGES[section]))
    except FetchError as error:
        raise RuntimeError(f"BBC: {error}") from error
    items = payload.get("data") if isinstance(payload, dict) else None
    return [normalise_item(item, collection_id) for item in items or [] if isinstance(item, dict)]


def fragments(block: Any) -> list[dict[str, Any]]:
    """Every {type, model} inline under a paragraph: fragments and urlLinks, in order."""
    found: list[dict[str, Any]] = []
    if not isinstance(block, dict):
        return found
    for child in (block.get("model") or {}).get("blocks") or []:
        if isinstance(child, dict):
            found.append(child)
            if child.get("type") == "urlLink":
                found.extend(fragments(child))
    return found


def paragraph_kind(paragraph: dict[str, Any]) -> str:
    """'links' when the paragraph is only link text, 'italic' when all its text is italic, else 'paragraph'."""
    inlines = [inline for inline in fragments(paragraph) if ((inline.get("model") or {}).get("text") or "").strip()]
    top_level = [inline for inline in ((paragraph.get("model") or {}).get("blocks") or [])
                 if isinstance(inline, dict) and ((inline.get("model") or {}).get("text") or "").strip()]
    if top_level and all(inline.get("type") == "urlLink" for inline in top_level):
        return "links"
    text_fragments = [inline for inline in inlines if inline.get("type") == "fragment"]
    if text_fragments and all("italic" in ((inline.get("model") or {}).get("attributes") or [])
                              for inline in text_fragments):
        return "italic"
    return "paragraph"


def first_paragraph_text(block: Any) -> str | None:
    if isinstance(block, dict):
        if block.get("type") == "paragraph":
            return (block.get("model") or {}).get("text")
        for child in (block.get("model") or {}).get("blocks") or []:
            text = first_paragraph_text(child)
            if text is not None:
                return text
    return None


def body_entries(contents: list[Any]) -> list[tuple[str, str, bool]]:
    """(kind, text, marked) for each text-bearing block in page order. marked means BBC set
    suitableForAbridgement to false on the block."""
    entries: list[tuple[str, str, bool]] = []
    for content in contents:
        if not isinstance(content, dict):
            continue
        model = content.get("model") or {}
        if content.get("type") == "text":
            marked = model.get("suitableForAbridgement") is False
            for block in model.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "paragraph":
                    paragraphs = [block]
                elif block.get("type") in ("unorderedList", "orderedList"):
                    paragraphs = [
                        child
                        for item in (block.get("model") or {}).get("blocks") or []
                        if isinstance(item, dict)
                        for child in (item.get("model") or {}).get("blocks") or []
                        if isinstance(child, dict) and child.get("type") == "paragraph"
                    ]
                else:
                    continue
                for paragraph in paragraphs:
                    text = common.clean_text((paragraph.get("model") or {}).get("text") or "")
                    if text:
                        entries.append((paragraph_kind(paragraph), text, marked))
        elif content.get("type") in ("subheadline", "crosshead"):
            text = common.clean_text(first_paragraph_text(content) or "")
            if text:
                entries.append(("subheadline", text, False))
        elif content.get("type") == "links":
            entries.append(("block_links", "", False))
    return entries


def article_text(contents: list[Any]) -> str | None:
    """The body paragraphs, without the promos and related links BBC puts after them.

    The body ends at its last ordinary paragraph: not marked, not all italic, not only a link.
    After that point BBC places related links, then outlet promos. A paragraph there is kept
    only if BBC did not mark it and no links block comes before it, which keeps notes such as
    "Additional reporting by ..." and a correction. Subheadings are kept only inside the body.
    """
    entries = body_entries(contents)
    ordinary = [index for index, (kind, _, marked) in enumerate(entries) if kind == "paragraph" and not marked]
    if not ordinary:
        # A video page has one marked summary paragraph and nothing else.
        ordinary = [index for index, (kind, _, _) in enumerate(entries) if kind == "paragraph"]
    last = ordinary[-1] if ordinary else -1
    parts = [text for kind, text, _ in entries[: last + 1] if kind in ("paragraph", "italic", "subheadline")]
    after_links = False
    for kind, text, marked in entries[last + 1:]:
        if kind == "block_links":
            after_links = True
        elif kind in ("paragraph", "italic") and not marked and not after_links:
            parts.append(text)
    return common.paragraphs_text(parts)


def page_categories(page: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """BBC's own labels for one page: its topic tags, its section and its pillar."""
    return {
        "topics": [
            {"title": topic.get("title"), "id": topic.get("id"), "url": topic.get("url"), "is_event": topic.get("isEvent")}
            for topic in page.get("topics") or []
            if isinstance(topic, dict) and topic.get("title")
        ],
        "page_section": [
            {"title": section.get("title"), "url": section.get("url")}
            for section in page.get("section") or []
            if isinstance(section, dict) and section.get("title")
        ],
        "pillar": [
            pillar["title"]
            for pillar in metadata.get("pillar") or []
            if isinstance(pillar, dict) and pillar.get("title")
        ],
    }


JSON_LD_PATTERN = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def iso_from_epoch_ms(value: Any) -> str | None:
    """BBC page times are epoch milliseconds; write them like the listing API does (UTC, ms, Z)."""
    try:
        moment = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def byline_contributors(contents: list[Any]) -> list[dict[str, str | None]]:
    """Each byline contributor as BBC gives it. BBC can put several people in one name, for
    example "A, B and C"; the name is kept whole rather than split by guesswork."""
    contributors = []
    for content in contents:
        if not isinstance(content, dict) or content.get("type") != "byline":
            continue
        for contributor in (content.get("model") or {}).get("blocks") or []:
            if not isinstance(contributor, dict) or contributor.get("type") != "contributor":
                continue
            parts = {
                part.get("type"): clean_string(first_paragraph_text(part))
                for part in (contributor.get("model") or {}).get("blocks") or []
                if isinstance(part, dict)
            }
            if parts.get("name"):
                contributors.append({"name": parts["name"], "role": parts.get("role"), "location": parts.get("location")})
    return contributors


def json_ld_author_names(page_html: str) -> list[str]:
    names = []
    for document in JSON_LD_PATTERN.findall(page_html):
        try:
            data = json.loads(document)
        except json.JSONDecodeError:
            continue
        authors = data.get("author") if isinstance(data, dict) else None
        for author in authors if isinstance(authors, list) else [authors]:
            if isinstance(author, dict) and clean_string(author.get("name")):
                names.append(clean_string(author["name"]))
    return names


def page_fields(page_html: str, page_props: dict[str, Any], contents: list[Any], text: str | None) -> dict[str, Any]:
    """Row fields only the article page has. common.scrape fills a row field from here only
    when the listing left it empty, so the page's own times also go in page_* fields."""
    metadata = page_props.get("metadata") if isinstance(page_props.get("metadata"), dict) else {}
    analytics = page_props.get("analytics") if isinstance(page_props.get("analytics"), dict) else {}
    index_image = metadata.get("indexImage") if isinstance(metadata.get("indexImage"), dict) else {}
    raw_image = next(
        (block.get("model") or {} for block in index_image.get("blocks") or []
         if isinstance(block, dict) and block.get("type") == "rawImage"),
        {},
    )
    video = metadata.get("videoMetadata") if isinstance(metadata.get("videoMetadata"), dict) else {}
    versions = [version for version in video.get("versions") or [] if isinstance(version, dict)]

    authors = byline_contributors(contents)
    if not authors:
        authors = [{"name": name, "role": None, "location": None} for name in json_ld_author_names(page_html)]
    first_published = iso_from_epoch_ms(metadata.get("firstPublished"))
    last_published = iso_from_epoch_ms(metadata.get("lastPublished"))
    last_updated = iso_from_epoch_ms(metadata.get("lastUpdated"))
    return {
        "authors": authors,
        "author": clean_string(metadata.get("contributors")) or (authors[0]["name"] if authors else None),
        "published": first_published,
        "updated": last_updated or last_published,
        "word_count": len(text.split()) if text else None,
        "page_first_published": first_published,
        "page_last_published": last_published,
        "page_last_updated": last_updated,
        # BBC's own count covers more than the body text (captions and link titles too).
        "bbc_word_count": metadata.get("wordCount"),
        "content_id": analytics.get("contentId"),
        "language": analytics.get("language"),
        "producer": analytics.get("producer"),
        "page_type": page_props.get("type"),
        "page_subtype": page_props.get("subtype"),
        "seo_headline": clean_string(metadata.get("seoHeadline")),
        "promo_headline": clean_string(metadata.get("promoHeadline")),
        "page_description": clean_string(metadata.get("description")),
        "image_original": index_image.get("originalSrc"),
        "image_copyright": raw_image.get("copyrightHolder"),
        "video_pid": video.get("id"),
        "video_duration_seconds": versions[0].get("duration") if versions else None,
    }


def parse_article_page(page_html: str) -> tuple[str | None, dict[str, Any], dict[str, Any]] | None:
    """Text, categories and page-only row fields from a www.bbc.com page's __NEXT_DATA__. Pages
    built on the older BBC platform (for example /sport) have no __NEXT_DATA__ and give None."""
    match = NEXT_DATA_PATTERN.search(page_html)
    if not match:
        return None
    try:
        page_props = json.loads(match.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    pages = page_props.get("page") if isinstance(page_props, dict) else None
    if not isinstance(pages, dict) or not pages:
        return None
    page = next(iter(pages.values()))
    if not isinstance(page, dict):
        return None
    metadata = page_props.get("metadata") if isinstance(page_props.get("metadata"), dict) else {}
    contents = page.get("contents") if isinstance(page.get("contents"), list) else []
    text = article_text(contents)
    return text, page_categories(page, metadata), page_fields(page_html, page_props, contents, text)


def article_details(url: str) -> tuple[str | None, dict[str, Any], dict[str, Any]] | None:
    try:
        page_html = common.get_html(urljoin(SITE_ROOT, url), referer=SITE_ROOT + "/news")
    except FetchError:
        return None
    return parse_article_page(page_html)
