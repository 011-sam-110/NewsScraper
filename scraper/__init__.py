"""News sources. SOURCES maps each outlet id to its source.

The Guardian and The New York Times were removed on 2026-11-05 at Sam's request: their own words
rule out commercial use, and Provenance now has paid plans. DROPPED_OUTLETS keeps them out of every
path that could bring them back (the rail refuses to send their stored rows).
"""

from . import bbc, pbs, reuters, rss_outlets

# The outlets read by a scraper of their own.
SCRAPERS = (reuters, bbc, pbs)
SOURCES = {module.NAME: module for module in SCRAPERS}
# Provenance's RSS feeds, one outlet each. `--sources feeds` means all of them.
SOURCES.update({outlet.NAME: outlet for outlet in rss_outlets.OUTLETS})
FEED_OUTLETS = frozenset(rss_outlets.FEED_OUTLET_NAMES)
FEED_GROUP = "feeds"
DROPPED_OUTLETS = ("guardian", "nyt")
