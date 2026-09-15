"""Reuters: the section API for listings, article pages for text and Reuters' own taxonomy."""

from __future__ import annotations

import json
import re
import sys
import time
from functools import cached_property
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from . import common
from .common import FetchError, clean_text

NAME = "reuters"
API_URL = "https://www.reuters.com/pf/api/v3/content/fetch/articles-by-section-alias-or-id-v1"
SITE_ROOT = "https://www.reuters.com"
# The sections in the Reuters World menu, read from reuters.com on 2026-09-14. Oceania
# stories are in Asia Pacific, and Reuters has no Antarctica section.
SECTIONS = {
    "africa": "/world/africa/",
    "americas": "/world/americas/",
    "asia-pacific": "/world/asia-pacific/",
    "europe": "/world/europe/",
    "middle-east": "/world/middle-east/",
    "china": "/world/china/",
    "india": "/world/india/",
    "japan": "/world/japan/",
    "uk": "/world/uk/",
    "us": "/world/us/",
    "iran": "/world/iran/",
    "israel-hamas": "/world/israel-hamas/",
    "ukraine-russia-war": "/world/ukraine-russia-war/",
    "reuters-next": "/world/reuters-next/",
    # The World menu links to the polls section outside /world/.
    "reuters-ipsos-polls": "/reuters-ipsos-polls/",
}
# The page the Chrome session opens first, and the Referer for article pages.
REFERER = urljoin(SITE_ROOT, "/world/")
# DataDome answers a blocked request with one of these statuses.
BLOCKED_STATUSES = (401, 403, 429)
# The section API carries Reuters' Arc deployment id as "d". A stale one answers 404 for every
# section at once, which is what happened on 2026-09-15 when Reuters moved from 381 to 382. So the
# id is read from the live page and this value is only the fallback for when that cannot be done.
DEFAULT_DEPLOYMENT = "382"
# The deployment id appears in the page's own asset URLs as ?d=<digits> or &d=<digits>.
DEPLOYMENT_PATTERN = re.compile(r"[?&]d=(\d{1,6})\b")
_browser_session: list[Any] = []
_deployment: list[str] = []


def browser_proxy() -> Any:
    if not common.PROXY_URL:
        return None
    parsed = urlsplit(common.PROXY_URL)
    proxy: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def read_deployment(page_html: str) -> str | None:
    """Reuters' current Arc deployment id, from its own page. None when the page does not show one.

    The most common value wins: the page references many assets, and they all carry the id it was
    built with, so a stray match cannot outvote them.
    """
    found = DEPLOYMENT_PATTERN.findall(page_html)
    if not found:
        return None
    return max(set(found), key=found.count)


def current_page_html() -> str | None:
    """The Reuters world page, from the Chrome tab when there is one, else a plain request."""
    if _browser_session:
        try:
            return _browser_session[0][3].content()
        except Exception:  # noqa: BLE001 - a dead tab must not stop the scrape
            return None
    try:
        return common.get_html(REFERER)
    except FetchError:
        return None


def deployment_id(refresh: bool = False) -> str:
    """The deployment id to put in the section API URL, read once per run and then reused."""
    if refresh:
        _deployment.clear()
    if _deployment:
        return _deployment[0]
    page_html = current_page_html()
    found = read_deployment(page_html) if page_html else None
    _deployment.append(found or DEFAULT_DEPLOYMENT)
    return _deployment[0]


def build_url(section_path: str, offset: int, request_id: int, size: int,
              deployment: str | None = None) -> str:
    """The section API URL. Pure: the caller passes the deployment id, so nothing here is fetched."""
    query = {
        "arc-site": "reuters",
        "fetch_type": "collection",
        "offset": offset,
        "requestId": request_id,
        "section_id": section_path,
        "section_optional_fields": "newsletters",
        "size": size,
        "uri": section_path,
        "website": "reuters",
    }
    params = {
        "query": json.dumps(query, separators=(",", ":")),
        "d": deployment or DEFAULT_DEPLOYMENT,
        "mxId": "00000000",
        "_website": "reuters",
    }
    return f"{API_URL}?{urlencode(params)}"


def fetch_page(section_path: str, offset: int, request_id: int, size: int) -> Any:
    """One listing page. A 404 means the deployment id has gone stale, so read it again and retry."""
    try:
        return fetch_page_once(section_path, offset, request_id, size)
    except FetchError as error:
        if error.status != 404:
            raise RuntimeError(f"Reuters: {error}") from error
    stale = deployment_id()
    fresh = deployment_id(refresh=True)
    if fresh == stale:
        raise RuntimeError(
            f"Reuters: HTTP 404 for {section_path} and the deployment id is still {fresh}. "
            "The section API may have moved."
        )
    print(f"Reuters moved from deployment {stale} to {fresh}; retrying.", file=sys.stderr)
    try:
        return fetch_page_once(section_path, offset, request_id, size)
    except FetchError as error:
        raise RuntimeError(f"Reuters: {error}") from error


def fetch_page_once(section_path: str, offset: int, request_id: int, size: int) -> Any:
    url = build_url(section_path, offset, request_id, size, deployment_id())
    if _browser_session:
        return fetch_page_in_browser(url)
    try:
        return common.get_json(url, referer=urljoin(SITE_ROOT, section_path))
    except FetchError as error:
        if error.status in BLOCKED_STATUSES:
            return fetch_page_in_browser(url)
        raise


def get_browser_session() -> Any:
    if _browser_session:
        return _browser_session[0]
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as error:
        raise RuntimeError("Install Playwright with: pip install playwright") from error

    playwright = sync_playwright().start()
    browser = None
    try:
        # DataDome blocks Playwright's bundled Chromium and any browser that reports
        # navigator.webdriver, so drive the installed Google Chrome with its own user agent.
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=False,
            proxy=browser_proxy(),
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(REFERER, wait_until="domcontentloaded", timeout=30000)
        if not wait_for_reuters_page(page, 30):
            print(
                "Reuters is showing a browser check. Complete it in the Chrome window; "
                "scraping continues when the Reuters page loads.",
                file=sys.stderr,
            )
            if not wait_for_reuters_page(page, 300):
                raise RuntimeError("The Reuters browser check did not clear within 5 minutes.")
    except (PlaywrightError, RuntimeError) as error:
        if browser is not None:
            browser.close()
        playwright.stop()
        if isinstance(error, PlaywrightError):
            reason = str(error).splitlines()[0]
            raise RuntimeError(f"Could not open Reuters in Google Chrome: {reason}") from error
        raise
    _browser_session.append((playwright, browser, context, page))
    return _browser_session[0]


def wait_for_reuters_page(page: Any, timeout_seconds: float) -> bool:
    """Wait until the Reuters app loads, which means the DataDome check has cleared."""
    from playwright.sync_api import Error as PlaywrightError

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if page.evaluate("() => Boolean(window.Fusion && window.Fusion.globalContent)"):
                return True
        except PlaywrightError:
            pass  # The check reloads the page, which destroys the execution context.
        page.wait_for_timeout(1000)
    return False


def fetch_in_browser(url: str, accept: str) -> str:
    """GET a Reuters URL from inside the Chrome tab, so the request carries its DataDome cookie."""
    page = get_browser_session()[3]
    result = page.evaluate(
        """
        async ([requestUrl, accept]) => {
            const response = await fetch(requestUrl, {credentials: 'include', headers: {Accept: accept}});
            return {status: response.status, body: response.ok ? await response.text() : ''};
        }
        """,
        [url, accept],
    )
    status = result["status"]
    if status == 200:
        return result["body"]
    message = f"Reuters returned HTTP {status} to the Chrome session."
    if status in BLOCKED_STATUSES:
        message += (
            " DataDome did not accept the session; a VPN, a rotating proxy IP or too many "
            "requests can cause this."
        )
    # FetchError is a RuntimeError, so every existing caller still catches it, but the status is
    # now readable and a stale deployment id can be told apart from a block.
    raise FetchError(message, status)


def fetch_page_in_browser(url: str) -> Any:
    try:
        return json.loads(fetch_in_browser(url, "application/json"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Reuters returned a non-JSON response to the Chrome session.") from error


def close() -> None:
    if _browser_session:
        playwright, browser, _, _ = _browser_session.pop()
        browser.close()
        playwright.stop()


def find_articles(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("content_elements", "articles", "items"):
            children = value.get(key)
            if isinstance(children, list) and all(isinstance(item, dict) for item in children):
                candidates = [item for item in children if is_article(item)]
                if candidates:
                    return candidates
        for child in value.values():
            found = find_articles(child)
            if found:
                return found
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict) and is_article(item)]
        if candidates:
            return candidates
        for child in value:
            found = find_articles(child)
            if found:
                return found
    return []


def is_article(value: dict[str, Any]) -> bool:
    return bool(
        value.get("canonical_url")
        or value.get("url")
        or value.get("headlines")
        or value.get("title")
    )


def text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("basic", "text", "value"):
            result = text_value(value.get(key))
            if result:
                return result
    return None


def first_value(article: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = article
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, "", []):
            return value
    return None


TEXT_ELEMENT_TYPES = ("text", "paragraph", "subhead", "header")


def article_text(article: dict[str, Any]) -> str | None:
    content = article.get("content_elements") or article.get("body") or article.get("text")
    if isinstance(content, str):
        return common.paragraphs_text(content.splitlines())
    if isinstance(content, list):
        paragraphs = []
        for element in content:
            if isinstance(element, dict) and element.get("type", "text") in TEXT_ELEMENT_TYPES:
                value = text_value(element.get("content") or element.get("text"))
                if value:
                    paragraphs.append(value)
        return common.paragraphs_text(paragraphs)
    return None


class ArticlePageParser(HTMLParser):
    """Find article text in a Reuters page: Fusion content, then JSON-LD, then paragraph divs."""

    def __init__(self) -> None:
        super().__init__()
        self.script_type: str | None = None
        self.script_parts: list[str] = []
        self.json_ld_documents: list[str] = []
        self.fusion_scripts: list[str] = []
        self.paragraph_tag: str | None = None
        self.paragraph_depth = 0
        self.current_paragraph: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script":
            self.script_type = attributes.get("type") or "text/javascript"
            self.script_parts = []
        elif self.paragraph_tag:
            if tag == self.paragraph_tag:
                self.paragraph_depth += 1
        elif (attributes.get("data-testid") or "").startswith("paragraph-"):
            self.paragraph_tag = tag
            self.paragraph_depth = 1
            self.current_paragraph = []

    def handle_data(self, data: str) -> None:
        if self.script_type is not None:
            self.script_parts.append(data)
        elif self.paragraph_tag:
            self.current_paragraph.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script_type is not None:
            script = "".join(self.script_parts)
            if self.script_type == "application/ld+json":
                self.json_ld_documents.append(script)
            elif "Fusion.globalContent" in script:
                self.fusion_scripts.append(script)
            self.script_type = None
        elif tag == self.paragraph_tag:
            self.paragraph_depth -= 1
            if self.paragraph_depth == 0:
                paragraph = clean_text("".join(self.current_paragraph))
                if paragraph:
                    self.paragraphs.append(paragraph)
                self.paragraph_tag = None

    # Read only after feed() has run: the page's scripts are decoded once, on first use.
    @cached_property
    def fusion_results(self) -> list[dict[str, Any]]:
        results = []
        for script in self.fusion_scripts:
            match = re.search(r"Fusion\.globalContent\s*=\s*", script)
            if not match:
                continue
            try:
                content, _ = json.JSONDecoder().raw_decode(script, match.end())
            except json.JSONDecodeError:
                continue
            result = content.get("result") if isinstance(content, dict) else None
            if isinstance(result, dict):
                results.append(result)
        return results

    @cached_property
    def json_ld_objects(self) -> list[dict[str, Any]]:
        objects = []
        for document in self.json_ld_documents:
            try:
                metadata = json.loads(document)
            except json.JSONDecodeError:
                continue
            candidates = metadata if isinstance(metadata, list) else [metadata]
            objects.extend(candidate for candidate in candidates if isinstance(candidate, dict))
        return objects

    def get_categories(self) -> dict[str, Any]:
        for result in self.fusion_results:
            categories = page_categories(result)
            if categories:
                return categories
        return {}

    def get_article_body(self) -> str | None:
        for result in self.fusion_results:
            text = article_text(result)
            if text:
                return text
        for candidate in self.json_ld_objects:
            if candidate.get("articleBody"):
                return article_text({"body": str(candidate["articleBody"])})
            if candidate.get("@type") == "LiveBlogPosting":
                # A live blog's Fusion body is only an Arena embed; its posts are here.
                text = live_blog_text(candidate)
                if text:
                    return text
        return "\n\n".join(self.paragraphs) or None


def live_blog_text(posting: dict[str, Any]) -> str | None:
    """Each post as its headline then its body, in page order."""
    parts = []
    for update in posting.get("liveBlogUpdate") or []:
        if not isinstance(update, dict):
            continue
        for value in (update.get("headline"), update.get("articleBody")):
            if isinstance(value, str) and (text := article_text({"body": value})):
                parts.append(text)
    return "\n\n".join(parts) or None


def page_categories(result: dict[str, Any]) -> dict[str, Any]:
    """Reuters' own labels for one article page. DEST: tags route wire copy, so skip them."""
    taxonomy = result.get("taxonomy")
    if not isinstance(taxonomy, dict):
        return {}
    tags = [tag for tag in taxonomy.get("tags") or [] if isinstance(tag, dict)]
    place = first_value(result, ("additional_properties", "article_properties", "place"))
    # A dateline without a city leaves only the date here, for example "Sept 14 (Reuters)".
    if not isinstance(place, str) or "(Reuters)" in place:
        place = None
    return {
        "topics": [tag["short_bio"] for tag in tags if tag.get("is_topic") and tag.get("short_bio")],
        "subjects": [
            {"code": entry["code"], "name": entry.get("name")}
            for entry in taxonomy.get("n2") or []
            if isinstance(entry, dict) and entry.get("code")
        ],
        "sections": [
            section["name"]
            for section in taxonomy.get("other_sections") or []
            if isinstance(section, dict) and section.get("name")
        ],
        "keywords": [keyword for keyword in taxonomy.get("keywords") or [] if isinstance(keyword, str)],
        "place": place,
    }


def fetch_article_page(url: str) -> ArticlePageParser | None:
    if _browser_session:
        return fetch_article_page_in_browser(url)
    try:
        page_html = common.get_html(urljoin(SITE_ROOT, url), referer=REFERER)
    except FetchError as error:
        if error.status in BLOCKED_STATUSES:
            return fetch_article_page_in_browser(url)
        return None
    parser = ArticlePageParser()
    parser.feed(page_html)
    return parser


def fetch_article_page_in_browser(url: str) -> ArticlePageParser | None:
    # Fetch the server-rendered HTML from inside the tab. Navigating the tab to the page
    # would also load its ads, images and scripts, which takes seconds per article.
    try:
        page_html = fetch_in_browser(urljoin(SITE_ROOT, url), "text/html")
    except RuntimeError:
        return None
    parser = ArticlePageParser()
    parser.feed(page_html)
    return parser


def listing_categories(article: dict[str, Any]) -> dict[str, Any]:
    """Reuters' labels that the section API sends with every article."""
    kicker = article.get("kicker")
    kicker = kicker if isinstance(kicker, dict) else {}
    primary_tag = article.get("primary_tag")
    primary_tag = primary_tag if isinstance(primary_tag, dict) else {}
    ad_topics = article.get("ad_topics")
    ad_topics = ad_topics if isinstance(ad_topics, list) else []
    return {
        "section": [name for name in kicker.get("names") or [] if isinstance(name, str)],
        "primary_topic": primary_tag.get("short_bio") or primary_tag.get("text"),
        # Reuters repeats ad topics; keep the first of each.
        "ad_topics": list(dict.fromkeys(topic for topic in ad_topics if isinstance(topic, str))),
    }


def normalise_article(article: dict[str, Any]) -> dict[str, Any]:
    headline = text_value(article.get("headlines")) or text_value(article.get("title"))
    description = text_value(article.get("description")) or text_value(article.get("summary"))
    url = article.get("canonical_url") or article.get("url")
    absolute_url = urljoin(SITE_ROOT, url) if url else None
    thumbnail = first_value(
        article,
        ("promo_items", "basic", "url"),
        ("image", "url"),
        ("thumbnail", "url"),
    )
    authors = article.get("authors") if isinstance(article.get("authors"), list) else []
    first_author = authors[0] if authors and isinstance(authors[0], dict) else {}
    slug = first_value(article, ("slug",), ("canonical_url",))
    if isinstance(slug, str) and "/" in slug:
        slug = slug.rstrip("/").rsplit("/", 1)[-1]
    return {
        "id": article.get("_id") or article.get("id") or url,
        "headline": headline,
        "description": description,
        "published": first_value(
            article,
            ("published_time",),
            ("display_time",),
            ("publish_date",),
            ("first_publish_date",),
            ("display_date",),
            ("published_at",),
            ("date",),
        ),
        "url": absolute_url,
        "thumbnail": thumbnail,
        "read_minutes": first_value(article, ("read_minutes",), ("read_time",), ("readTime",)),
        "short_bio": first_value(
            article,
            ("primary_tag", "short_bio"),
            ("short_bio",),
            ("author", "short_bio"),
            ("byline", "short_bio"),
        ) or first_author.get("description"),
        "author": first_author.get("byline") or first_author.get("name"),
        "author_bio": first_author.get("description"),
        "authors": authors,
        "slug": slug,
        "text": article_text(article),
        "word_count": article.get("word_count"),
        "article_type": article.get("article_type"),
        "source": article.get("source"),
        "primary_media_type": article.get("primary_media_type"),
        "primary_tag": article.get("primary_tag"),
        "kicker": article.get("kicker"),
        "updated": article.get("updated_time"),
        "categories": listing_categories(article),
    }


def list_page(section: str, page: int, size: int) -> list[dict[str, Any]]:
    path = SECTIONS[section]
    payload = fetch_page(path, page * size, page + 1, size)
    return [{**normalise_article(raw), "section_path": path} for raw in find_articles(payload)]


def article_details(url: str) -> tuple[str | None, dict[str, Any]] | None:
    page = fetch_article_page(url)
    if page is None:
        return None
    return page.get_article_body(), page.get_categories()
