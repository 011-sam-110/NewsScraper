"""The New York Times: the public RSS feeds only. No article pages, no full text.

Collected from each feed item: headline, one-sentence summary, link (also the id), publication
time, authors (NYT joins them in one dc:creator string; they are split here), the image with its
credit and caption, the URL section path, and NYT's own keyword taxonomy. The taxonomy comes from
category@domain http://www.nytimes.com/namespaces/keywords/<type>: des -> subjects, nyt_geo ->
places, nyt_org -> organizations, nyt_per -> people, nyt_ttl -> titles. Tags with no domain are a
mix of real tags and internal project tags, kept unchanged under "other".

Not collected, and why:
- Article text, word count and update time. Article pages answer automated requests with a
  DataDome block (HTTP 403, checked 2026-09-14), and the notice in nytimes.com/robots.txt forbids
  automated scraping, text and data mining and AI use without NYT's written permission. This
  module does not fetch article pages and does not try to get past the block.
- The official NYT APIs (Article Search, Top Stories, Times Newswire) need a user API key and
  their own terms of use. They are not used here.
- Sports: its feed returned HTTP 200 with no items on 2026-09-14, so it is not listed.
The feeds hold the latest 20 to 60 stories only and have no older pages; run the scraper often.
"""

from __future__ import annotations

from typing import Any

from . import feeds

NAME = "nyt"
FEED_ROOT = "https://rss.nytimes.com/services/xml/rss/nyt/"
# Each feed returned HTTP 200 with items on 2026-09-14.
SECTIONS = {
    "home-page": FEED_ROOT + "HomePage.xml",
    "world": FEED_ROOT + "World.xml",
    "africa": FEED_ROOT + "Africa.xml",
    "americas": FEED_ROOT + "Americas.xml",
    "asia-pacific": FEED_ROOT + "AsiaPacific.xml",
    "europe": FEED_ROOT + "Europe.xml",
    "middle-east": FEED_ROOT + "MiddleEast.xml",
    "us": FEED_ROOT + "US.xml",
    "politics": FEED_ROOT + "Politics.xml",
    "business": FEED_ROOT + "Business.xml",
    "economy": FEED_ROOT + "Economy.xml",
    "technology": FEED_ROOT + "Technology.xml",
    "science": FEED_ROOT + "Science.xml",
    "health": FEED_ROOT + "Health.xml",
    "climate": FEED_ROOT + "Climate.xml",
    "upshot": FEED_ROOT + "Upshot.xml",
    "opinion": FEED_ROOT + "Opinion.xml",
    "arts": FEED_ROOT + "Arts.xml",
    "travel": FEED_ROOT + "Travel.xml",
}
KEYWORD_DOMAIN_ROOT = "http://www.nytimes.com/namespaces/keywords/"
KEYWORD_GROUPS = {
    "des": "subjects",
    "nyt_geo": "places",
    "nyt_org": "organizations",
    "nyt_per": "people",
    "nyt_ttl": "titles",
}


def keyword_categories(item: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {group: [] for group in KEYWORD_GROUPS.values()}
    groups["other"] = []
    for category in item["categories"]:
        domain = category["domain"]
        if domain is None:
            group = "other"
        else:
            # A domain outside the known five keeps its own name, so no tag is dropped.
            key = domain.removeprefix(KEYWORD_DOMAIN_ROOT)
            group = KEYWORD_GROUPS.get(key, key)
        groups.setdefault(group, []).append(category["name"])
    return groups


def list_page(section: str, page: int, size: int) -> list[dict[str, Any]]:
    """Every item in the section's feed on page 0. A feed has no further pages."""
    if page > 0:
        return []
    feed_url = SECTIONS[section]
    rows = []
    for item in feeds.fetch_items("NYT", feed_url):
        row = feeds.base_row(item, feed_url)
        row["categories"] = {**keyword_categories(item), **row["categories"]}
        rows.append(row)
    return rows
