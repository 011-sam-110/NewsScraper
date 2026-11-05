"""Provenance's RSS feeds, read on this box every 5 minutes and kept in the store.

WHY THE BOX READS THEM TOO. Provenance's server reads these feeds itself, but it keeps no memory: an
item that leaves a feed is gone, and a deploy (several a day) forgets everything. A feed lists only
its newest 10 to 100 items, and eight of them roll over in less than a day (measured 2026-11-05). The
store here keeps every item it ever saw, and the rail pushes the last week of them to Provenance, so
World Headlines can hold all of it. Provenance still reads the feeds itself as the fallback.

Each feed is its own outlet. The slug is the outlet id in the store and in every pushed row, and it
is the `id` of the same feed in Provenance's FEEDS list (lib/news/feed.ts), which maps it to the
name a reader sees. Keep the two lists in step.

What is NOT here, on purpose:
- BBC and PBS: their scrapers read far more than their world feeds hold.
- The Guardian and The New York Times: dropped by Sam on 2026-11-05.
- Al Jazeera and UN News: their terms say "personal, non-commercial use", and the paid site does
  not serve them (lib/access/dataTerms.ts in Provenance). A source the site does not serve is not
  fetched here either.

Feed rows are stored with status `feed`, not `scraped`, so the paid extract stage never claims them.
Whether they go through extract and resolve is a separate decision (the no-location-pins work).
"""

from __future__ import annotations

from typing import Any

from . import feeds

# (slug, name a reader sees, the publisher's own feed URL). Same URLs as Provenance's FEEDS.
FEEDS: tuple[tuple[str, str, str], ...] = (
    ("npr", "NPR", "https://feeds.npr.org/1004/rss.xml"),
    ("dw", "DW", "https://rss.dw.com/rdf/rss-en-world"),
    ("france24", "France 24", "https://www.france24.com/en/rss"),
    ("skynews", "Sky News", "https://feeds.skynews.com/feeds/rss/world.xml"),
    ("cbsnews", "CBS News", "https://www.cbsnews.com/latest/rss/world"),
    ("abcnews", "ABC News", "https://abcnews.com/abcnews/internationalheadlines"),
    ("independent", "The Independent", "https://www.independent.co.uk/news/world/rss"),
    ("euronews", "Euronews", "https://www.euronews.com/rss?level=theme&name=news"),
    ("scmp", "SCMP", "https://www.scmp.com/rss/91/feed/"),
    ("timesofindia", "Times of India", "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms"),
    ("jpost", "The Jerusalem Post", "https://www.jpost.com/rss/rssfeedsinternational"),
    ("africanews", "Africanews", "https://www.africanews.com/feed/rss"),
    # RNZ's robots.txt asks for a Crawl-delay of 7 s. One request per 5 minutes is far wider.
    ("rnz", "RNZ", "https://www.rnz.co.nz/rss/pacific.xml"),
    ("mercopress", "MercoPress", "https://en.mercopress.com/rss"),
    ("batimes", "Buenos Aires Times", "https://www.batimes.com.ar/feed"),
    ("cbc", "CBC", "https://www.cbc.ca/webfeed/rss/rss-world"),
    ("yonhap", "Yonhap", "https://en.yna.co.kr/RSS/news.xml"),
    ("kyivindependent", "Kyiv Independent", "https://kyivindependent.com/news-archive/rss/"),
    ("abcaustralia", "ABC Australia", "https://www.abc.net.au/news/feed/51120/rss.xml"),
    ("rfi", "RFI", "https://www.rfi.fr/en/rss"),
    ("ansa", "ANSA", "https://www.ansa.it/english/news/english_nr_rss.xml"),
    ("focustaiwan", "Focus Taiwan", "https://feeds.feedburner.com/rsscna/engnews/"),
    ("premiumtimes", "Premium Times", "https://www.premiumtimesng.com/feed"),
    ("sabcnews", "SABC News", "https://www.sabcnews.com/sabcnews/feed/"),
    # Added 2026-11-05 to replace the Guardian and the NYT. Both say yes to other websites:
    # The Diplomat: "Websites, blogs and other publications are free to reproduce extracts of up to
    # 200 words of articles published by The Diplomat, with a link back to the full article"
    # (https://thediplomat.com/syndication/). AllAfrica: "The headline can be viewed for personal use
    # or on a website, as long as proper formatting is maintained and material is correctly
    # attributed. Any web site displaying the headlines must include a link to AllAfrica home page"
    # (https://allafrica.com/misc/tools/rss.html). Headlines and links only cross; no article text.
    ("thediplomat", "The Diplomat", "https://thediplomat.com/feed/"),
    ("allafrica", "AllAfrica", "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf"),
)

FEED_OUTLET_NAMES = tuple(slug for slug, _, _ in FEEDS)


def rows_for(slug: str, url: str) -> list[dict[str, Any]]:
    """Every item in one feed as a store row. The outlet's own <category> labels are kept as given."""
    rows = []
    for item in feeds.fetch_items(slug, url):
        if not item["link"] and not item["guid"]:
            continue
        row = feeds.base_row(item, url)
        row["categories"] = {
            **row["categories"],
            "rss": [category["name"] for category in item["categories"]],
        }
        rows.append(row)
    return rows


class FeedOutlet:
    """A source the shared scrape loop can run: one section, one page, no article pages.

    A class and not a SimpleNamespace: the scrape loop keys its work by source, so a source must
    be hashable.
    """

    def __init__(self, slug: str, name: str, url: str) -> None:
        self.NAME = slug
        self.DISPLAY_NAME = name
        self.SECTIONS = {"feed": url}
        self.url = url

    def list_page(self, section: str, page: int, size: int) -> list[dict[str, Any]]:
        # A feed has no pages. Page 0 is the whole feed; later pages are empty without a request.
        return rows_for(self.NAME, self.url) if page == 0 else []

    def __repr__(self) -> str:
        return f"FeedOutlet({self.NAME!r})"


OUTLETS = tuple(FeedOutlet(slug, name, url) for slug, name, url in FEEDS)
