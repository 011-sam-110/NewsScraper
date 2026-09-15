"""The Guardian: section RSS feeds and paginated section pages for listings, page models for text.

Terms. The Guardian's robots.txt allows these paths, but its header says: "Guardian content is
made available under our terms and conditions of use. Any other uses are not permitted, incl.
but not limited to: for large language models (LLMs), machine learning and/or artificial
intelligence-related purposes; with any of the aforementioned technologies; and/or for any
commercial purposes. Contact licensing@theguardian.com for assistance"

No Content API. The Content API needs a key: api-key=test returned HTTP 401 on 2026-09-14.

Listing design, from responses recorded on 2026-09-14:
    page 0      <path>/rss. The feed is the richest listing: headline, standfirst, byline,
                publication date, image and every tag (tag id and name). It does not paginate.
                For the regional sections, the feed holds the same stories as section page 1.
    page N >= 1 <path>.json?page=N+1. Section page N+1 as {"html": ...}, about 20 cards each,
                back years (Africa: "About 41,853 results"). A card has the id, URL, headline,
                timestamp, image, short URL, kicker, standfirst and byline when shown, and the
                Guardian's card type and pillar. It has no tags and no word count; article_details
                adds the text and the tags. Past the last page the Guardian redirects to the
                unpaginated section page, which gives [].
The feed goes first because a card cannot carry the feed's byline, standfirst and tags, and
article_details can return only (text, categories), so it cannot fill those row fields later.

Sections that are fronts (world, uk, us, australia, global-development) do not paginate: their
page 2 redirects to a front with no cards, so they give the feed only. world/asia gives HTTP 404
for page 2, so it gives the feed only too.

Article pages: <url>.json?dcr=true is the page model the site renders from. It has one shape for
articles, live blogs, video, audio and interactives: tags [{id, type, title}] and blocks of
elements. Video and audio items have no text blocks, so their text is None. A page that answers
in the older {config, html} shape is read from config.page and the articleBody markup.
article_details also returns the page's row fields (word count, dates, byline, contributors,
standfirst, image, short URL and more). common.scrape uses them only to fill fields the listing
left empty, which matters most for section-page cards.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import common
from .common import FetchError, clean_text

NAME = "guardian"
SITE_ROOT = "https://www.theguardian.com"
# Section paths with a working RSS feed, verified on 2026-09-14. Only the regional ones paginate.
SECTIONS = {
    "africa": "world/africa",
    "americas": "world/americas",
    "asia-pacific": "world/asia-pacific",
    "asia": "world/asia",
    "south-central-asia": "world/south-and-central-asia",
    "europe": "world/europe-news",
    "middle-east": "world/middleeast",
    "world": "world",
    "uk": "uk-news",
    "us": "us-news",
    "australia": "australia-news",
    "global-development": "global-development",
}
# URL path segments that name a content type, for example /world/live/2026/... .
URL_CONTENT_TYPES = ("live", "video", "audio", "gallery", "picture", "ng-interactive", "interactive")
TEXT_ELEMENT_TYPES = ("TextBlockElement", "SubheadingBlockElement", "BlockquoteBlockElement")
BLOCK_END = re.compile(r"</(?:p|h2|h3|h4|h5|li|blockquote)\s*>|<br\s*/?>", re.IGNORECASE)


def fetch(url: str) -> tuple[str, bytes]:
    """(final URL after redirects, body). The Guardian redirects past the last listing page."""
    return common.fetch(url)


def fetch_json(url: str) -> Any:
    _, body = fetch(url)
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise FetchError(f"Non-JSON response from {url}") from error


def page_id(url: str | None) -> str | None:
    if not url:
        return None
    return urlsplit(url).path.strip("/") or None


def url_content_type(url: str | None) -> str | None:
    """The type segment of a Guardian URL path, or "article" when the path has none."""
    segments = (page_id(url) or "").split("/")
    for segment in segments:
        if segment in URL_CONTENT_TYPES:
            return segment
    return "article" if re.search(r"/\d{4}/[a-z]{3}/\d{2}/", "/" + "/".join(segments) + "/") else None


def html_paragraphs(fragment: str) -> list[str]:
    return [text for chunk in BLOCK_END.split(fragment) if (text := clean_text(chunk))]


def split_list(value: Any) -> list[str]:
    """config.page sends lists as comma-joined strings, and absent values as "" or "None"."""
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if not isinstance(value, str) or value in ("", "None"):
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# Feed (page 0)


def feed_descriptions(document: bytes) -> list[str | None]:
    """Each item's description as paragraphs. common.parse_rss joins <p> blocks with no space."""
    descriptions = []
    for item in ElementTree.fromstring(document).iter("item"):
        raw = item.findtext("description") or ""
        raw = re.sub(r"<a [^>]*>\s*Continue reading\.*\s*</a>", "", raw)
        descriptions.append(common.paragraphs_text(html_paragraphs(raw)))
    return descriptions


def largest_image(media: list[dict[str, str]]) -> str | None:
    def width(entry: dict[str, str]) -> int:
        return int(entry["width"]) if str(entry.get("width", "")).isdigit() else 0

    return max(media, key=width)["url"] if media else None


def feed_row(item: dict[str, Any], description: str | None) -> dict[str, Any]:
    url = item["link"] or item["guid"]
    row = common.empty_row()
    row.update(
        {
            "id": page_id(url),
            "headline": item["title"],
            "description": description or item["description"],
            "published": item["published"],
            "url": url,
            "thumbnail": largest_image(item["media"]),
            "author": ", ".join(item["creators"]) or None,
            "authors": item["creators"],
            "content_type": url_content_type(url),
            "media": item["media"],
            "media_credit": item["media_credit"],
            "listing": "rss",
            "categories": {
                "feed_tags": [
                    {"id": page_id(tag["domain"]) or tag["domain"], "name": tag["name"]}
                    for tag in item["categories"]
                ],
            },
        }
    )
    return row


def feed_rows(document: bytes) -> list[dict[str, Any]]:
    items = common.parse_rss(document)
    descriptions = feed_descriptions(document)
    return [feed_row(item, description) for item, description in zip(items, descriptions)]


# Section pages (page >= 1)


class CardParser(HTMLParser):
    """The cards (data-test-id="facia-card") of a section page, in page order."""

    FIELD_CLASSES = {
        "fc-item__kicker": "kicker",
        "fc-item__standfirst": "standfirst",
        "fc-item__byline": "byline",
        "fc-item__video-duration": "video_duration",
        "js-headline-text": "headline",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, Any]] = []
        self.card: dict[str, Any] | None = None
        self.card_depth = 0
        self.field: str | None = None
        self.field_depth = 0
        self.field_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "").split()
        if self.card is None:
            if tag == "div" and attributes.get("data-test-id") == "facia-card":
                self.card = {
                    "id": attributes.get("data-id"),
                    "short_url": attributes.get("data-loyalty-short-url"),
                    "link_name": attributes.get("data-link-name"),
                    "card_type": next((c[len("fc-item--type-"):] for c in classes if c.startswith("fc-item--type-")), None),
                    "card_pillar": next((c[len("fc-item--pillar-"):] for c in classes if c.startswith("fc-item--pillar-")), None),
                }
                self.card_depth = 1
            return
        if tag == "div":
            self.card_depth += 1
        card = self.card
        if tag == "a" and "fc-item__link" in classes and not card.get("url"):
            card["url"] = attributes.get("href")
        elif tag == "time" and not card.get("datetime"):
            card["datetime"] = attributes.get("datetime")
        elif tag == "img" and not card.get("image"):
            card["image"] = attributes.get("src")
        if self.field:
            if tag in ("div", "span"):
                self.field_depth += 1
            return
        for css_class, field in self.FIELD_CLASSES.items():
            if css_class in classes and tag in ("div", "span") and not card.get(field):
                self.field, self.field_depth, self.field_parts = field, 1, []
                break

    def handle_data(self, data: str) -> None:
        if self.field:
            self.field_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.card is None:
            return
        if self.field and tag in ("div", "span"):
            self.field_depth -= 1
            if self.field_depth == 0:
                self.card[self.field] = clean_text("".join(self.field_parts)) or None
                self.field = None
        if tag == "div":
            self.card_depth -= 1
            if self.card_depth == 0:
                if self.card.get("url"):
                    self.cards.append(self.card)
                self.card = None
                self.field = None


def card_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").isoformat()
    except ValueError:
        return value


def card_row(card: dict[str, Any]) -> dict[str, Any]:
    url = card["url"]
    short_url = card.get("short_url")
    row = common.empty_row()
    row.update(
        {
            "id": card.get("id") or page_id(url),
            "headline": card.get("headline"),
            "description": card.get("standfirst"),
            "published": card_datetime(card.get("datetime")),
            "url": url,
            "thumbnail": card.get("image"),
            "author": card.get("byline"),
            "authors": [card["byline"]] if card.get("byline") else [],
            "content_type": url_content_type(url),
            "kicker": card.get("kicker"),
            "short_url": SITE_ROOT + short_url if short_url and short_url.startswith("/") else short_url,
            "video_duration": card.get("video_duration"),
            "listing": "page",
            "categories": {key: card[key] for key in ("card_type", "card_pillar") if card.get(key)},
        }
    )
    return row


def listing_html(body: bytes) -> str:
    """Section pages answer .json with {"html": ...}; fronts redirect to plain HTML."""
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    return payload.get("html") or "" if isinstance(payload, dict) else ""


def list_page(section: str, page: int, size: int) -> list[dict[str, Any]]:
    """size is not used: the Guardian sets the page size (20 in the feed and on a section page)."""
    path = SECTIONS[section]
    if page == 0:
        try:
            _, document = fetch(f"{SITE_ROOT}/{path}/rss")
        except FetchError as error:
            raise RuntimeError(f"The Guardian: {error}") from error
        return [{**row, "section_path": path} for row in feed_rows(document)]

    site_page = page + 1
    try:
        final_url, body = fetch(f"{SITE_ROOT}/{path}.json?page={site_page}")
    except FetchError as error:
        if error.status == 404:
            return []
        raise RuntimeError(f"The Guardian: {error}") from error
    if parse_qs(urlsplit(final_url).query).get("page") != [str(site_page)]:
        return []  # Redirected to the unpaginated section page: past the last page.
    parser = CardParser()
    parser.feed(listing_html(body))
    parser.close()
    return [{**card_row(card), "section_path": path} for card in parser.cards]


# Article pages


def tag_titles(tags: list[dict[str, Any]], *types: str) -> list[str]:
    return [tag["title"] for tag in tags if tag.get("type") in types and tag.get("title")]


def model_categories(model: dict[str, Any]) -> dict[str, Any]:
    """The Guardian's own tags and section labels from the page model."""
    tags = [tag for tag in model.get("tags") or [] if isinstance(tag, dict) and tag.get("id")]
    config = model.get("config") if isinstance(model.get("config"), dict) else {}
    page_format = model.get("format") if isinstance(model.get("format"), dict) else {}
    return {
        "page_tags": [{"id": tag["id"], "type": tag.get("type"), "title": tag.get("title")} for tag in tags],
        "keywords": tag_titles(tags, "Keyword"),
        "keyword_ids": [tag["id"] for tag in tags if tag.get("type") == "Keyword"],
        "tones": tag_titles(tags, "Tone"),
        "series": tag_titles(tags, "Series"),
        "blogs": tag_titles(tags, "Blog"),
        "contributors": tag_titles(tags, "Contributor"),
        "page_type": tag_titles(tags, "Type"),
        "tracking": tag_titles(tags, "Tracking"),
        "newspaper": tag_titles(tags, "NewspaperBook", "NewspaperBookSection"),
        "publication": tag_titles(tags, "Publication"),
        "commissioning_desks": split_list(config.get("commissioningDesks")),
        "page_section": {
            "id": model.get("sectionName"),
            "label": model.get("sectionLabel"),
            "url": model.get("sectionUrl"),
        },
        "pillar": model.get("pillar"),
        "design": page_format.get("design"),
    }


def model_text(model: dict[str, Any]) -> str | None:
    """Block text in page order. A live blog post starts with its title."""
    paragraphs: list[str] = []
    for block in model.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("title"), str):
            paragraphs.append(block["title"])
        for element in block.get("elements") or []:
            if not isinstance(element, dict) or not isinstance(element.get("html"), str):
                continue
            if str(element.get("_type", "")).rsplit(".", 1)[-1] in TEXT_ELEMENT_TYPES:
                paragraphs.extend(html_paragraphs(element["html"]))
    return common.paragraphs_text(paragraphs)


class ArticleBodyParser(HTMLParser):
    """Paragraphs inside the articleBody element of the older page html."""

    TEXT_TAGS = ("p", "h2", "h3", "li", "blockquote")
    VOID_TAGS = ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.text_depth = 0
        self.parts: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID_TAGS:
            return
        attributes = {key: value or "" for key, value in attrs}
        if self.depth == 0:
            if "articleBody" in attributes.get("itemprop", "") or "article-body-commercial-selector" in attributes.get("class", ""):
                self.depth = 1
            return
        self.depth += 1
        if tag in self.TEXT_TAGS:
            if self.text_depth == 0:
                self.parts = []
            self.text_depth += 1

    def handle_data(self, data: str) -> None:
        if self.text_depth:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.depth == 0 or tag in self.VOID_TAGS:
            return
        if tag in self.TEXT_TAGS and self.text_depth:
            self.text_depth -= 1
            if self.text_depth == 0 and (text := clean_text("".join(self.parts))):
                self.paragraphs.append(text)
        self.depth -= 1


def legacy_categories(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_tag_ids": split_list(page.get("keywordIds")) + split_list(page.get("nonKeywordTagIds")),
        "keywords": split_list(page.get("keywords")),
        "keyword_ids": split_list(page.get("keywordIds")),
        "tones": split_list(page.get("tones")),
        "series": split_list(page.get("series")),
        "blogs": split_list(page.get("blogs")),
        "tracking": split_list(page.get("trackingNames")),
        "commissioning_desks": split_list(page.get("commissioningDesks")),
        "page_section": {"id": page.get("section"), "label": page.get("sectionName"), "url": None},
        "pillar": page.get("pillar"),
    }


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


def epoch_ms_iso(value: Any) -> str | None:
    milliseconds = as_int(value)
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def byline(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def news_article_data(model: dict[str, Any]) -> dict[str, Any]:
    for entry in model.get("linkedData") or []:
        if isinstance(entry, dict) and entry.get("@type") in ("NewsArticle", "LiveBlogPosting", "Article"):
            return entry
    return {}


def without_empty(fields: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in fields.items() if value not in (None, "", [], {})}


def model_fields(model: dict[str, Any]) -> dict[str, Any]:
    """Row fields the page model has and a listing card lacks. Sent only when the page has them."""
    config = model.get("config") if isinstance(model.get("config"), dict) else {}
    open_graph = model.get("openGraphData") if isinstance(model.get("openGraphData"), dict) else {}
    page_type = model.get("pageType") if isinstance(model.get("pageType"), dict) else {}
    linked = news_article_data(model)
    tags = [tag for tag in model.get("tags") or [] if isinstance(tag, dict)]
    contributors = tag_titles(tags, "Contributor")
    author = byline(model.get("byline")) or byline(config.get("byline"))
    standfirst = model.get("standfirst")
    duration = as_int(config.get("videoDuration"))
    return without_empty(
        {
            "id": model.get("pageId") or config.get("contentId"),
            "headline": model.get("headline"),
            "description": common.paragraphs_text(html_paragraphs(standfirst)) if isinstance(standfirst, str) else None,
            "published": model.get("webPublicationDate") or linked.get("datePublished"),
            "updated": open_graph.get("article:modified_time") or linked.get("dateModified"),
            "thumbnail": open_graph.get("og:image"),
            "author": author,
            "authors": contributors or ([author] if author else []),
            "word_count": as_int(config.get("wordCount")),
            "short_url": config.get("shortUrl"),
            "canonical_url": model.get("canonicalUrl"),
            "trail_text": clean_text(model["trailText"]) if isinstance(model.get("trailText"), str) else None,
            "page_content_type": model.get("contentType"),
            "contributor_ids": [tag["id"] for tag in tags if tag.get("type") == "Contributor" and tag.get("id")],
            "edition": model.get("editionId"),
            "production_office": config.get("productionOffice"),
            "video_duration_seconds": duration or None,
            "is_commentable": model.get("isCommentable"),
            "is_paid_content": page_type.get("isPaidContent", config.get("isPaidContent")),
            "is_sensitive": page_type.get("isSensitive", config.get("isSensitive")),
            "is_special_report": model.get("isSpecialReport"),
        }
    )


def legacy_fields(page: dict[str, Any]) -> dict[str, Any]:
    author = byline(page.get("byline"))
    duration = as_int(page.get("videoDuration"))
    return without_empty(
        {
            "id": page.get("contentId") or page.get("pageId"),
            "headline": page.get("headline"),
            "published": epoch_ms_iso(page.get("webPublicationDate")),
            "thumbnail": page.get("thumbnail"),
            "author": author,
            "authors": [author] if author else [],
            "word_count": as_int(page.get("wordCount")),
            "short_url": page.get("shortUrl"),
            "page_content_type": page.get("contentType"),
            "contributor_ids": split_list(page.get("authorIds")),
            "production_office": page.get("productionOffice"),
            "video_duration_seconds": duration or None,
            "is_commentable": page.get("commentable"),
            "is_paid_content": page.get("isPaidContent"),
            "is_sensitive": page.get("isSensitive"),
        }
    )


def article_details(url: str) -> tuple[str | None, dict[str, Any], dict[str, Any]] | None:
    parts = urlsplit(url)
    try:
        model = fetch_json(f"{parts.scheme}://{parts.netloc}{parts.path}.json?dcr=true")
    except FetchError:
        return None
    if not isinstance(model, dict):
        return None
    if isinstance(model.get("tags"), list):
        return model_text(model), model_categories(model), model_fields(model)
    page = (model.get("config") or {}).get("page") if isinstance(model.get("config"), dict) else None
    if isinstance(page, dict) and isinstance(model.get("html"), str):
        parser = ArticleBodyParser()
        parser.feed(model["html"])
        parser.close()
        return common.paragraphs_text(parser.paragraphs), legacy_categories(page), legacy_fields(page)
    return None
