"""PBS NewsHour: section and tag listing pages for rows, article pages for text and PBS's own tags."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Callable, Iterator
from urllib.parse import urljoin, urlsplit

from . import common
from .common import FetchError, clean_text

NAME = "pbs"
SITE_ROOT = "https://www.pbs.org"
# Topic sections from sitemaps/maps/categories.xml, and the region tags, read on 2026-09-14.
# PBS has no /world/{region} sections; regions exist only as tags.
SECTIONS = {
    "world": "/newshour/world",
    "politics": "/newshour/politics",
    "nation": "/newshour/nation",
    "economy": "/newshour/economy",
    "science": "/newshour/science",
    "health": "/newshour/health",
    "arts": "/newshour/arts",
    "education": "/newshour/education",
    "africa": "/newshour/tag/africa",
    "middle-east": "/newshour/tag/middle-east",
    "europe": "/newshour/tag/europe",
    "asia": "/newshour/tag/asia",
    "latin-america": "/newshour/tag/latin-america",
}
CARD_TYPES = ("card-xl", "card-lg", "card-horiz")
VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def iter(self) -> Iterator[Node]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, match: Callable[[Node], bool]) -> list[Node]:
        return [node for node in self.iter() if match(node)]

    def find(self, match: Callable[[Node], bool]) -> Node | None:
        return next((node for node in self.iter() if match(node)), None)

    def raw_text(self, skip: Callable[[Node], bool] | None = None) -> str:
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif not (skip and skip(child)):
                parts.append(child.raw_text(skip))
        return "".join(parts)

    def text(self) -> str:
        return clean_text(self.raw_text())


def has_class(name: str, tag: str | None = None) -> Callable[[Node], bool]:
    return lambda node: name in node.classes and (tag is None or node.tag == tag)


def has_attr(name: str, value: str, tag: str | None = None) -> Callable[[Node], bool]:
    return lambda node: node.attrs.get(name) == value and (tag is None or node.tag == tag)


class TreeBuilder(HTMLParser):
    """A forgiving element tree: void tags never open, and a stray end tag closes the nearest match."""

    def __init__(self) -> None:
        # Character references stay escaped so clean_text unescapes each value exactly once.
        super().__init__(convert_charrefs=False)
        self.root = Node("#root", {}, None)
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {name: value or "" for name, value in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.current.children.append(Node(tag, {name: value or "" for name, value in attrs}, self.current))

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)

    def handle_entityref(self, name: str) -> None:
        self.current.children.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.current.children.append(f"&#{name};")


def parse_html(document: str) -> Node:
    builder = TreeBuilder()
    builder.feed(document)
    builder.close()
    return builder.root


def attribute_text(value: str | None) -> str | None:
    return clean_text(value or "") or None


def meta_content(root: Node, attribute: str, value: str) -> str | None:
    node = root.find(has_attr(attribute, value, "meta"))
    return attribute_text(node.attrs.get("content")) if node else None


def comma_list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def url_section(url: str | None) -> str | None:
    """The first path segment after /newshour/, for example "world" or "show"."""
    parts = [part for part in urlsplit(url or "").path.split("/") if part]
    return parts[1] if len(parts) > 2 and parts[0] == "newshour" else None


def largest_srcset_url(srcset: str) -> str | None:
    best: tuple[int, str] | None = None
    for candidate in srcset.split(","):
        pieces = candidate.split()
        if not pieces:
            continue
        width = int(pieces[1][:-1]) if len(pieces) > 1 and pieces[1][:-1].isdigit() else 0
        if best is None or width > best[0]:
            best = (width, pieces[0])
    return best[1] if best else None


def card_thumbnail(card: Node) -> str | None:
    """The card's real image. src is often a placeholder; the lazy-load attributes hold the image."""
    for node in card.iter():
        if node.attrs.get("data-bg"):
            return node.attrs["data-bg"]
        if node.tag != "img":
            continue
        if node.attrs.get("data-src"):
            return node.attrs["data-src"]
        if node.attrs.get("data-srcset"):
            return largest_srcset_url(node.attrs["data-srcset"])
        src = node.attrs.get("src")
        if src and "placeholder" not in src:
            return src
    return None


def parse_card(card: Node, card_type: str) -> dict[str, Any] | None:
    title = card.find(has_class(f"{card_type}__title", "a"))
    if title is None or not title.attrs.get("href"):
        return None
    url = urljoin(SITE_ROOT, title.attrs["href"])
    meta = card.find(has_class(f"{card_type}__meta"))
    section_link = meta.find(lambda node: node.tag == "a") if meta else None
    date_label = None
    if meta is not None:
        date_node = meta.find(lambda node: node.tag == "span")
        date_label = date_node.text() if date_node else clean_text(meta.raw_text(skip=lambda node: node.tag == "a"))
    byline_node = card.find(has_class(f"{card_type}__byline"))
    byline = clean_text(byline_node.raw_text(skip=lambda node: node.tag == "span")) if byline_node else None
    excerpt = card.find(has_class(f"{card_type}__excerpt"))
    play = card.find(has_class(f"{card_type}__play-cta"))
    duration = play.find(lambda node: node.tag == "span") if play else None
    link = card.find(has_class(f"{card_type}__link"))

    categories: dict[str, Any] = {}
    if section_link is not None and section_link.attrs.get("href"):
        categories["card_section"] = {
            "name": section_link.text(),
            "url": urljoin(SITE_ROOT, section_link.attrs["href"]),
        }
    row = common.empty_row()
    row.update(
        {
            # Cards carry no post id. The canonical URL path is stable and names the story.
            "id": urlsplit(url).path,
            "headline": title.text() or None,
            "description": excerpt.text() if excerpt else None,
            "url": url,
            "thumbnail": card_thumbnail(card),
            "author": byline or None,
            # The card byline is one comma-joined string, so an agency such as
            # "Associated Press" is its own entry here.
            "authors": comma_list(byline),
            "categories": categories,
            # The card shows a month and day with no year, for example "Sep 14".
            "listing_date": date_label or None,
            "card_type": card_type,
            "url_section": url_section(url),
            "is_video": play is not None,
            "video_duration": duration.text() if duration else None,
            "call_to_action": link.text() if link else None,
        }
    )
    return row


def parse_listing(document: str) -> list[dict[str, Any]]:
    """Every story card on a section or tag page, in page order. Episode promos are not stories."""
    root = parse_html(document)
    rows = []
    for node in root.iter():
        if node.tag != "article":
            continue
        card_type = next((name for name in CARD_TYPES if name in node.classes), None)
        if card_type and (row := parse_card(node, card_type)):
            rows.append(row)
    return rows


def page_url(section: str, page: int) -> str:
    base = urljoin(SITE_ROOT, SECTIONS[section])
    return base if page == 0 else f"{base}/page/{page + 1}"


def fetch_page(url: str) -> tuple[str, str]:
    """(final URL, HTML). PBS redirects past the last page."""
    final_url, body = common.fetch(url, "text/html")
    return final_url, body.decode("utf-8", errors="replace")


def list_page(section: str, page: int, size: int) -> list[dict[str, Any]]:
    """One PBS listing page. PBS sets the page size (about 10 cards), so size is not used."""
    url = page_url(section, page)
    try:
        final_url, document = fetch_page(url)
    except FetchError as error:
        # A tag page past its last page answers 404.
        if error.status == 404:
            return []
        raise RuntimeError(f"PBS: {error}") from error
    # A section page past page 500 redirects to the NewsHour home page.
    if final_url.rstrip("/") != url.rstrip("/"):
        return []
    return [{**row, "listing_url": url} for row in parse_listing(document)]


def json_ld_objects(root: Node) -> list[dict[str, Any]]:
    objects = []
    for script in root.find_all(has_attr("type", "application/ld+json", "script")):
        try:
            data = json.loads(script.raw_text())
        except json.JSONDecodeError:
            continue
        for candidate in data if isinstance(data, list) else [data]:
            if isinstance(candidate, dict):
                objects.append(candidate)
    return objects


def is_page_furniture(node: Node) -> bool:
    return (
        any(name.startswith(("inline-invite", "newsletter-inline")) for name in node.classes)
        or node.tag in ("script", "style", "figure", "form")
    )


def body_paragraphs(container: Node) -> list[str]:
    """Paragraphs and subheads in page order, without the funding box or READ MORE promo links."""
    paragraphs = []
    stack = list(reversed(container.children))
    while stack:
        node = stack.pop()
        if isinstance(node, str) or is_page_furniture(node):
            continue
        if node.tag in ("p", "h2", "h3", "h4", "li", "blockquote"):
            text = node.text()
            if text and not text.upper().startswith("READ MORE:"):
                paragraphs.append(text)
            continue
        stack.extend(reversed(node.children))
    return paragraphs


def post_attributes(root: Node) -> tuple[str | None, str | None]:
    """WordPress puts the post id and post type in body classes: postid-554954, single-post."""
    body = root.find(lambda node: node.tag == "body")
    classes = body.classes if body else []
    post_id = next((name[len("postid-"):] for name in classes if name.startswith("postid-")), None)
    post_type = next(
        (name[len("single-"):] for name in classes if name.startswith("single-") and name != "single-format-standard"),
        None,
    )
    return post_id, post_type


def page_tags(root: Node) -> list[dict[str, str]]:
    """The first ul.tag__list is the story's "Go Deeper" tag list."""
    tag_list = root.find(has_class("tag__list", "ul"))
    if tag_list is None:
        return []
    return [
        {"name": link.text(), "url": urljoin(SITE_ROOT, link.attrs["href"])}
        for link in tag_list.find_all(lambda node: node.tag == "a" and bool(node.attrs.get("href")))
        if link.text()
    ]


def parse_article_page(document: str) -> dict[str, Any]:
    root = parse_html(document)
    ld_objects = json_ld_objects(root)
    news_article = next((item for item in ld_objects if item.get("@type") == "NewsArticle"), {})
    video = next((item for item in ld_objects if item.get("@type") == "VideoObject"), {})

    body = root.find(has_attr("itemprop", "articleBody", "article"))
    # A newsletter box can split the body into several div.body-text blocks; read all of them.
    body_blocks = body.find_all(has_class("body-text", "div")) if body else []
    transcript = root.find(has_class("video-transcript"))
    if body_blocks:
        text = common.paragraphs_text([paragraph for block in body_blocks for paragraph in body_paragraphs(block)])
    elif transcript is not None:
        text = common.paragraphs_text(body_paragraphs(transcript))
    else:
        text = None
    if not text and isinstance(news_article.get("articleBody"), str):
        text = clean_text(news_article["articleBody"]) or None

    # The byline repeats at the foot of the page; keep each author once.
    author_links = root.find_all(lambda node: node.attrs.get("itemprop") == "author")
    authors: dict[str, str | None] = {}
    for link in author_links:
        name_node = link.find(has_attr("itemprop", "name"))
        name = name_node.text() if name_node else link.text()
        if name and name not in authors:
            authors[name] = urljoin(SITE_ROOT, link.attrs["href"]) if link.attrs.get("href") else None
    if not authors:
        for person in news_article.get("author") or []:
            if isinstance(person, dict) and person.get("name"):
                authors.setdefault(clean_text(person["name"]), None)

    title = root.find(has_class("post__title", "h1")) or root.find(has_class("video-single__title", "h1"))
    published_node = root.find(has_attr("itemprop", "datePublished", "time"))
    canonical = root.find(lambda node: node.tag == "link" and node.attrs.get("rel") == "canonical")
    summary = root.find(has_class("vt__excerpt"))
    post_id, post_type = post_attributes(root)
    return {
        "post_id": post_id,
        "post_type": post_type,
        "canonical_url": canonical.attrs.get("href") if canonical else None,
        "headline": (title.text() if title else None) or meta_content(root, "property", "og:title"),
        "description": meta_content(root, "name", "description") or meta_content(root, "property", "og:description"),
        "published": (published_node.attrs.get("content") if published_node else None)
        or meta_content(root, "property", "article:published_time")
        or news_article.get("datePublished"),
        "updated": meta_content(root, "itemprop", "dateModified") or news_article.get("dateModified"),
        "authors": list(authors),
        "author_urls": [url for url in authors.values() if url],
        "thumbnail": meta_content(root, "itemprop", "thumbnailUrl") or meta_content(root, "property", "og:image"),
        "summary": summary.text() if summary else None,
        "video_duration": video.get("duration"),
        "video_embed_url": video.get("embedUrl"),
        "text": text,
        "categories": {
            "tags": page_tags(root),
            "article_section": meta_content(root, "property", "article:section")
            or meta_content(root, "itemprop", "articleSection"),
            "news_keywords": comma_list(meta_content(root, "name", "news_keywords")),
        },
    }


PAGE_FIELDS = (
    "headline", "description", "published", "updated", "thumbnail", "post_id", "post_type",
    "canonical_url", "author_urls", "summary", "video_duration", "video_embed_url",
)


def page_fields(page: dict[str, Any]) -> dict[str, Any]:
    """Row fields from the article page. common.scrape fills only fields the listing left empty."""
    fields = {name: page[name] for name in PAGE_FIELDS}
    fields.update(
        {
            "authors": page["authors"],
            "author": ", ".join(page["authors"]) or None,
            # Listing rows already hold the card byline, so keep the page authors under their own key too.
            "page_authors": page["authors"],
            "word_count": len(page["text"].split()) if page["text"] else None,
        }
    )
    return fields


def article_details(url: str) -> tuple[str | None, dict[str, Any], dict[str, Any]] | None:
    try:
        document = common.get_html(urljoin(SITE_ROOT, url))
    except FetchError:
        return None
    page = parse_article_page(document)
    return page["text"], page["categories"], page_fields(page)
