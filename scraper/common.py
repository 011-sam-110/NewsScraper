"""Pieces every news source shares: HTTP, text cleaning, RSS parsing and the scrape loop.

A source is a module with:
    NAME: str                       the outlet id written to each row, for example "bbc"
    SECTIONS: dict[str, str]        section id -> the outlet's own path, URL or collection id
    list_page(section: str, page: int, size: int) -> list[dict]
        Rows for one listing page (page counts from 0). Return [] past the last page.
    article_details(url: str) -> tuple[str | None, dict] | tuple[str | None, dict, dict] | None
        (optional) Full text and page categories for one article, or None when the page gives
        nothing. A third item, a dict of row fields read from the page (published, updated,
        authors, word_count ...), fills fields the listing left empty; it never overwrites them.
    close() -> None
        (optional) Called when the outlet is done, in the same thread that scraped it.

scrape() and scrape_source() take an optional store sink. With no sink they behave exactly as they
always have: every row is written to output_dir/<outlet>/<section>.jsonl and each section is walked
to the end of its listing. With a sink they write to the sink instead of to JSON Lines, skip the
article page of a story the sink already has, and stop a section at the first listing page holding
no new story for it (docs/ARCHITECTURE.md section 7.5). A sink has three methods:

    known(row) -> bool        Is this row already stored for row["section"]?
    needs_text(row) -> bool   Does this story's article page still need fetching?
    save(row) -> None         Store the row. Called once per row, in the outlet's thread.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0 Safari/537.36"
)

# THE PROXY: RETRY THROUGH IT, DO NOT LIVE IN IT.
#
# NEWS_PROXY_URL is Sam's rotating residential gateway: one host that hands out a new exit IP for
# every connection. A plain request goes DIRECT first and goes through the proxy only when the
# direct answer is a block: one of RETRY_STATUSES, a timeout, or a refused or reset connection.
# Measured 2026-11-05 at 05:54 UTC: all 29 feeds Provenance reads answered 200 to the box directly,
# so a proxy on every request would buy nothing, cost money (the plan is billed by the gigabyte),
# and make every request slower and less reliable than the home line. One retry is enough because
# the gateway's next connection is already a different address.
#
# What the proxy is NOT for: a paywall, a login or a subscription wall. Those answer the same from
# every address, and this code never tries to get past them. No CAPTCHA service is called either.
#
# The value holds a password, so every message that can carry it goes through redact() first, and
# exceptions raised on the proxy path drop their cause (`from None`): urllib can put the proxy URL in
# an error's text, and a chained cause prints in a traceback.
PROXY_ENV = "NEWS_PROXY_URL"
RETRY_STATUSES = frozenset({401, 403, 429, 451, 503})
# A short pause before the retry, so a block is not answered with an instant second request.
PROXY_RETRY_PAUSE_SECONDS = 2.0

# Outlets put zero-width characters inside article text.
INVISIBLE_CHARACTERS: dict[int, None] = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))

# user:password@ in any URL-like text. The proxy URL is the one this file worries about, but a
# credential in any URL has no place in a log.
_CREDENTIALS_IN_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]*@")


def proxy_url() -> str | None:
    """The proxy gateway URL, read at call time so a test or a settings change takes effect."""
    value = os.environ.get(PROXY_ENV, "").strip()
    return value or None


def redact(text: Any) -> str:
    """The text with any URL credential, and the proxy's user name and password, replaced by ***."""
    out = _CREDENTIALS_IN_URL.sub(r"\1***:***@", str(text))
    url = proxy_url()
    if url:
        parts = urlsplit(url)
        for secret in (parts.password, parts.username):
            if not secret:
                continue
            for form in {secret, unquote(secret)}:
                if len(form) >= 3:
                    out = out.replace(form, "***")
    return out


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool | None = None) -> None:
        super().__init__(redact(message))
        self.status = status
        # Worth one more try from another address. Default: the block statuses and no-status errors.
        self.retryable = (status in RETRY_STATUSES or status is None) if retryable is None else retryable


def clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", value)
    return " ".join(html.unescape(without_tags).translate(INVISIBLE_CHARACTERS).split())


def paragraphs_text(paragraphs: list[str]) -> str | None:
    cleaned = [text for paragraph in paragraphs if (text := clean_text(paragraph))]
    return "\n\n".join(cleaned) or None


def open_url(request: Request, via_proxy: bool = False) -> Any:
    if via_proxy:
        url = proxy_url()
        opener = build_opener(ProxyHandler({"http": url, "https": url}))
        return opener.open(request, timeout=30)
    return urlopen(request, timeout=30)


def _fetch_once(url: str, headers: dict[str, str], via_proxy: bool) -> tuple[str, bytes]:
    route = " through the proxy" if via_proxy else ""
    try:
        with open_url(Request(url, headers=headers), via_proxy=via_proxy) as response:
            body = response.read()
            # Some servers gzip the body unasked (The Independent, measured 2026-11-05), and urllib
            # does not unpack it. The header or gzip's own magic bytes say so.
            encoding = str((getattr(response, "headers", None) or {}).get("Content-Encoding") or "")
            if "gzip" in encoding.lower() or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return response.geturl(), body
    except HTTPError as error:
        failure = FetchError(f"HTTP {error.code} from {url}{route}", error.code)
    except URLError as error:
        failure = FetchError(f"Could not reach {url}{route}: {error.reason}")
    except (TimeoutError, socket.timeout, ConnectionError) as error:
        failure = FetchError(f"Could not reach {url}{route}: {type(error).__name__}")
    except (gzip.BadGzipFile, EOFError):
        failure = FetchError(f"{url} sent a gzip body that did not unpack", retryable=False)
    # Raised outside the except blocks, so the urllib error (which can hold the proxy URL) is not
    # chained onto it and cannot reach a traceback.
    raise failure


def fetch(
    url: str, accept: str = "*/*", referer: str | None = None, proxy_retry: bool = True
) -> tuple[str, bytes]:
    """GET a URL and return (final URL after redirects, body).

    Some outlets redirect past their last listing page, so the final URL is the end signal.
    Direct first; one retry through the proxy when the direct answer is a block (see PROXY_ENV).
    proxy_retry=False skips that retry, for a refusal that another address cannot change.
    """
    headers = {"Accept": accept, "Accept-Language": "en-GB,en;q=0.9", "User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        return _fetch_once(url, headers, via_proxy=False)
    except FetchError as error:
        if not proxy_retry or not proxy_url() or not error.retryable:
            raise
        log(f"{urlsplit(url).hostname}: {error}; trying once through the proxy.", sys.stderr)
    time.sleep(PROXY_RETRY_PAUSE_SECONDS)
    return _fetch_once(url, headers, via_proxy=True)


def http_get(url: str, accept: str = "*/*", referer: str | None = None, proxy_retry: bool = True) -> bytes:
    return fetch(url, accept, referer, proxy_retry)[1]


def get_json(url: str, referer: str | None = None, proxy_retry: bool = True) -> Any:
    try:
        return json.loads(http_get(url, "application/json", referer, proxy_retry))
    except json.JSONDecodeError as error:
        raise FetchError(f"Non-JSON response from {url}") from error


def get_html(url: str, referer: str | None = None, proxy_retry: bool = True) -> str:
    return http_get(url, "text/html", referer, proxy_retry).decode("utf-8", errors="replace")


RSS_NAMESPACES = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom",
}


def rss_date(value: str | None) -> str | None:
    """RFC 822 feed dates as ISO 8601; other values pass through unchanged."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value.strip()


# RSS 1.0 (RDF) puts its own elements in this namespace: DW publishes it.
RSS1_NAMESPACE = "http://purl.org/rss/1.0/"
# Atom: GOV.UK publishes only Atom (2026-11-05). Its entries are <entry>, in the Atom namespace.
ATOM_ENTRY = f"{{{RSS_NAMESPACES['atom']}}}entry"


def feed_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    """The items of an RSS 2.0, an RSS 1.0 (RDF) or an Atom document, in document order."""
    return [node for node in root.iter() if node.tag in ("item", f"{{{RSS1_NAMESPACE}}}item", ATOM_ENTRY)]


def atom_entry(entry: ElementTree.Element) -> dict[str, Any]:
    """The same fields parse_rss gives for an RSS item, read from one Atom <entry>.

    Atom dates are ISO 8601 already, so they pass through as given. The link is the entry's
    rel="alternate" link (a link with no rel is alternate by the Atom spec).
    """

    def text(tag: str) -> str | None:
        node = entry.find(f"atom:{tag}", RSS_NAMESPACES)
        return node.text.strip() if node is not None and node.text and node.text.strip() else None

    link = None
    for node in entry.findall("atom:link", RSS_NAMESPACES):
        if node.get("href") and node.get("rel", "alternate") == "alternate":
            link = node.get("href").strip()
            break
    return {
        "title": text("title"),
        "link": link,
        "guid": text("id"),
        "description": clean_text(text("summary") or text("content") or "") or None,
        "published": text("published") or text("updated"),
        "creators": [
            node.text.strip()
            for node in entry.findall("atom:author/atom:name", RSS_NAMESPACES)
            if node.text and node.text.strip()
        ],
        "categories": [
            {"domain": node.get("scheme") or None, "name": node.get("term").strip()}
            for node in entry.findall("atom:category", RSS_NAMESPACES)
            if node.get("term") and node.get("term").strip()
        ],
        "media": [],
        "media_credit": None,
        "media_description": None,
    }


def parse_rss(document: bytes) -> list[dict[str, Any]]:
    """Every field an RSS 2.0, RSS 1.0 or Atom item carries, with outlet taxonomy kept as {domain, name}."""
    items = []
    for item in feed_items(ElementTree.fromstring(document)):
        if item.tag == ATOM_ENTRY:
            items.append(atom_entry(item))
            continue
        rss1 = item.tag.startswith("{")

        def text(tag: str) -> str | None:
            node = item.find(tag, RSS_NAMESPACES)
            if node is None and rss1 and ":" not in tag:
                node = item.find(f"{{{RSS1_NAMESPACE}}}{tag}")
            return node.text.strip() if node is not None and node.text and node.text.strip() else None

        media = [
            node.attrib
            for tag in ("media:content", "media:thumbnail")
            for node in item.findall(tag, RSS_NAMESPACES)
            if node.get("url")
        ]
        items.append(
            {
                "title": text("title"),
                "link": text("link"),
                "guid": text("guid"),
                "description": clean_text(text("description") or "") or None,
                "published": rss_date(text("pubDate")) or text("dc:date"),
                "creators": [node.text.strip() for node in item.findall("dc:creator", RSS_NAMESPACES) if node.text],
                "categories": [
                    {"domain": node.get("domain") or None, "name": node.text.strip()}
                    for node in item.findall("category")
                    if node.text and node.text.strip()
                ],
                "media": media,
                "media_credit": text("media:credit"),
                "media_description": text("media:description"),
            }
        )
    return items


def empty_row() -> dict[str, Any]:
    """The fields every outlet's row has. Sources may add more; more data is kept, not pruned."""
    return {
        "id": None,
        "headline": None,
        "description": None,
        "published": None,
        "updated": None,
        "url": None,
        "thumbnail": None,
        "author": None,
        "authors": [],
        "text": None,
        "word_count": None,
        "categories": {},
    }


# Outlet threads print at the same time; the lock keeps each message whole.
_print_lock = threading.Lock()


def log(message: str, stream: TextIO | None = None) -> None:
    with _print_lock:
        print(redact(message), file=stream)


@contextmanager
def section_output(folder: Path, section: str, sink: Any | None) -> Any:
    """The section's JSON Lines file, or None when a sink is storing the rows instead."""
    if sink is not None:
        yield None
        return
    with (folder / f"{section}.jsonl").open("w", encoding="utf-8") as output:
        yield output


def scrape(
    output_dir: Path,
    jobs: list[tuple[ModuleType, str]],
    size: int,
    delay: float,
    max_pages: int,
    include_text: bool,
    sink: Any | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Scrape every outlet at the same time, one thread each, to output_dir/<outlet>/<section>.jsonl.

    An outlet's sections run in turn in its own thread, so delay still spaces that outlet's
    requests. max_pages applies to each section. An outlet that raises stops, and the others
    continue. Returns (articles saved per outlet, error message per failed outlet).
    """
    sections_by_source: dict[Any, list[str]] = {}
    for source, section in jobs:
        sections_by_source.setdefault(source, []).append(section)
    totals = {source.NAME: 0 for source in sections_by_source}
    errors: dict[str, str] = {}
    if not sections_by_source:
        return totals, errors

    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=len(sections_by_source), thread_name_prefix="scrape") as pool:
        futures = {
            pool.submit(
                scrape_source, source, sections, output_dir / source.NAME,
                size, delay, max_pages, include_text, totals, stop, sink,
            ): source.NAME
            for source, sections in sections_by_source.items()
        }
        try:
            pending = set(futures)
            while pending:
                # Wait in short steps, so that Ctrl+C can reach this thread during a long run.
                _, pending = wait(pending, timeout=0.5)
        except KeyboardInterrupt:
            stop.set()  # Each outlet stops after its current request.
            raise
    for future, name in futures.items():
        try:
            future.result()
        except RuntimeError as error:
            errors[name] = redact(error)
        except Exception as error:  # A bug in one outlet must not hide the other outlets' results.
            errors[name] = redact(f"{type(error).__name__}: {error}")
    return totals, errors


def scrape_source(
    source: ModuleType,
    sections: list[str],
    folder: Path,
    size: int,
    delay: float,
    max_pages: int,
    include_text: bool,
    totals: dict[str, int],
    stop: threading.Event,
    sink: Any | None = None,
) -> None:
    """Scrape one outlet's sections in turn. Adds each saved page's row count to totals[source.NAME].

    close() runs in this thread because a Playwright session (Reuters) works only in the
    thread that started it.

    With no sink, the first section to raise ends the outlet, which is what a one-shot CLI run
    wants. With a sink, every section gets its turn and the failures are reported together at the
    end: on a schedule, one transient 5xx in an early section must not cost the outlet the fifteen
    sections behind it. The outlet is still reported as failed either way.
    """
    # A story listed by several sections gets a row in each, so rows keep every section
    # label. Its article page is fetched once, and later rows reuse the (text, categories).
    # This holds every story's text in memory for the outlet's run: about 10 KB a story.
    article_pages: dict[str, tuple[Any, ...]] = {}
    details = getattr(source, "article_details", None)
    if sink is None:
        folder.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    try:
        for section in sections:
            try:
                scrape_section(
                    source, section, folder, size, delay, max_pages, include_text,
                    totals, stop, sink, details, article_pages,
                )
            except Exception as error:  # noqa: BLE001 - reported per section, then re-raised together
                if sink is None:
                    raise
                failures.append(f"{section}: {error}")
                log(f"{source.NAME}:{section} failed: {error}", sys.stderr)
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    if failures:
        raise RuntimeError("; ".join(failures))


def scrape_section(
    source: ModuleType,
    section: str,
    folder: Path,
    size: int,
    delay: float,
    max_pages: int,
    include_text: bool,
    totals: dict[str, int],
    stop: threading.Event,
    sink: Any | None,
    details: Any | None,
    article_pages: dict[str, tuple[Any, ...]],
) -> None:
    """One section of one outlet. article_pages is shared across the outlet's sections, so a story
    listed in two sections has its article page fetched once."""
    section_total = 0
    page_count = 0
    seen: set[str] = set()
    with section_output(folder, section, sink) as output:
        while (max_pages == 0 or page_count < max_pages) and not stop.is_set():
            new_rows = []
            # With a sink, a page where the sink knows every row is the end of the new
            # stories in this section, so the section stops there instead of walking the
            # listing back to where it ends.
            page_all_known = True
            for row in source.list_page(section, page_count, size):
                key = str(row["id"] or row["url"] or row["headline"])
                if key in seen:
                    continue
                seen.add(key)
                row = {"outlet": source.NAME, "section": section, **row}
                want_text = bool(include_text and details and row["url"] and not row["text"])
                if sink is not None:
                    if not sink.known(row):
                        page_all_known = False
                    want_text = want_text and sink.needs_text(row)
                if want_text:
                    if row["url"] not in article_pages:
                        article_pages[row["url"]] = details(row["url"]) or (None, {})
                        stop.wait(delay)
                    text, page_categories, *page_fields = article_pages[row["url"]]
                    row["text"] = text
                    row["categories"] = {**row["categories"], **page_categories}
                    for name, value in (page_fields[0] if page_fields else {}).items():
                        if row.get(name) in (None, "", [], {}):
                            row[name] = value
                new_rows.append(row)

            # Past the last page, listings return nothing or stories the section already sent.
            if not new_rows:
                break

            for row in new_rows:
                if output is None:
                    sink.save(row)
                else:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                log(f"[{source.NAME}] {row['headline'] or '(untitled)'}\n{row['url'] or ''}\n")
            if output is not None:
                output.flush()
            section_total += len(new_rows)
            totals[source.NAME] += len(new_rows)
            page_count += 1
            log(
                f"{source.NAME}:{section} page {page_count}: saved {len(new_rows)} articles; "
                f"section total {section_total}.",
                sys.stderr,
            )
            if page_all_known and sink is not None:
                log(
                    f"{source.NAME}:{section}: the store already had every story on this page; "
                    "stopping this section.",
                    sys.stderr,
                )
                break
            stop.wait(delay)
