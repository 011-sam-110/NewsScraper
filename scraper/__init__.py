"""News sources. SOURCES maps each outlet id to its source.

The Guardian and The New York Times were removed on 2026-11-05 at Sam's request: their own words
rule out commercial use, and Provenance now has paid plans. DROPPED_OUTLETS keeps them out of every
path that could bring them back (the rail refuses to send their stored rows). Every RSS feed whose own
terms say no followed the same day for the same reason (below).
"""

from . import bbc, pbs, reuters, rss_outlets

# The outlets read by a scraper of their own.
SCRAPERS = (reuters, bbc, pbs)
SOURCES = {module.NAME: module for module in SCRAPERS}
# Provenance's RSS feeds, one outlet each. `--sources feeds` means all of them.
SOURCES.update({outlet.NAME: outlet for outlet in rss_outlets.OUTLETS})
FEED_OUTLETS = frozenset(rss_outlets.FEED_OUTLET_NAMES)
FEED_GROUP = "feeds"
# Later the same day Sam dropped every feed whose own terms say no to a commercial site. Each line
# below was read on the outlet's own page on 2026-11-05. Provenance refuses the same slugs at its door
# (REFUSED_OUTLETS in lib/news/ingest.ts), so a row of theirs is refused at both ends.
# - npr: content feeds only on "your personal site or application, or the site or application of a
#   nonprofit organization ... and is not a news, broadcast or multimedia organization"
#   (https://www.npr.org/about-npr/179876898/terms-of-use).
# - independent: "these feeds are for your personal, non-commercial use only and any republication or
#   other use of our content will require a licence" (https://www.independent.co.uk/service/rss-feeds-775086.html).
# - cbsnews: "personal, non-commercial purposes ... and may not build a business or other enterprise
#   utilizing any of the Content" (https://legal.paramount.com/us/en/cbsi/terms-of-use).
# - abcnews: a licence "for your personal, noncommercial use only" (https://disneytermsofuse.com/english/).
# - skynews: Sky's terms, clause 2.7: "You will not copy, download, reproduce, republish, frame,
#   broadcast, transmit in any manner whatsoever, any material on any Sky Service ... unless it is
#   necessary for Your own personal non-commercial home use" (https://www.sky.com/help/articles/skycom-terms-and-conditions).
# - aljazeera: "you do so only for your own personal, non-commercial use" (never read here).
# - dw: content "may not simply be copied, uploaded to your own accounts, or redistributed" (DW's
#   copyright guidance; it has no terms page).
# - france24, rfi: "solely for personal and private use" (France Médias Monde terms).
# - euronews, africanews: "for your personal, non-commercial home use only".
# - scmp: "for your own personal and non-commercial use only" (3.2.6).
# - timesofindia: "RSS is a free service offered by TIL to individuals for personal, non-commercial use."
# - yonhap: "shall not be published, broadcast, distributed, transmitted, displayed or sold ... without
#   prior permission of Yonhap News Agency".
# - focustaiwan: CNA's RSS is for non-commercial use by individuals and non-profits.
# - ansa: "Non è consentito il loro impiego per la pubblicazione di titoli delle ultime notizie ANSA su
#   siti web o su weblog." (no ANSA headlines on websites)
# - rnz: "The RSS Feeds must not be used to aggregate content on other websites."
# - cbc: "Any use other than for private purposes must be subject to an agreement with CBC/Radio-Canada".
# - abcaustralia: "ABC Content is provided for your personal, non-commercial use only."
# - unnews: "for the User's personal, non-commercial use" (un.org terms of use; never read here).
# - premiumtimes: "for personal, non-commercial purposes only".
# - sabcnews: "only permitted to use the service for personal and non-commercial use".
# - thediplomat: "The Services shall be used only in a noncommercial manner." (its terms list RSS among
#   the Services; its syndication page says yes, so the two disagree, and the terms win).
# BBC ("For business use of our RSS feeds you'll need to get our permission, and there may be a fee to
# pay.") and PBS ("You shall not ... in any way use or exploit any Information for commercial
# purposes") say no as well, but they are NOT dropped here: their website scrapers above still run,
# and Sam has not decided on those scrapers yet.
DROPPED_OUTLETS = (
    "guardian",
    "nyt",
    "npr",
    "independent",
    "cbsnews",
    "abcnews",
    "skynews",
    "aljazeera",
    "dw",
    "france24",
    "rfi",
    "euronews",
    "africanews",
    "scmp",
    "timesofindia",
    "yonhap",
    "focustaiwan",
    "ansa",
    "rnz",
    "cbc",
    "abcaustralia",
    "unnews",
    "premiumtimes",
    "sabcnews",
    "thediplomat",
)
