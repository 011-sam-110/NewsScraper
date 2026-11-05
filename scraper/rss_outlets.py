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
- Every other feed whose own terms say no to a commercial site: dropped by Sam the same day (23 more,
  each quoted in scraper/__init__.py, DROPPED_OUTLETS). Five of the old feeds passed and stay.

THE TERMS RULE (Sam, 2026-11-05; the same rule as Provenance's lib/news/feedList.ts). A feed is here
only when the outlet's own terms allow a commercial site to show its headlines ("yes"), it is under the
Open Government Licence or a CC BY licence, or its terms say nothing against it ("silent"). Each row
below names its basis and quotes the page it was read on. A proxy does not change a source's terms.

Feed rows are stored with status `feed`, not `scraped`, so the paid extract stage never claims them.
Whether they go through extract and resolve is a separate decision (the no-location-pins work).
"""

from __future__ import annotations

import re
from typing import Any

from . import feeds

# (slug, name a reader sees, the publisher's own feed URL). Same URLs as Provenance's FEEDS.
FEEDS: tuple[tuple[str, str, str], ...] = (
    # Kept: their terms passed.
    # "yes": "You may use The Jerusalem Post's RSS feed in order to create any application, service or
    # website" (https://www.jpost.com/landedpages/termsofservice.aspx, "RSS Feed").
    ("jpost", "The Jerusalem Post", "https://www.jpost.com/rss/rssfeedsinternational"),
    # "yes": "Webmasters who wish to include MercoPress news updates on their sites are welcomed to do so.
    # We only require ask to preserve the link back" (https://en.mercopress.com/feeds).
    ("mercopress", "MercoPress", "https://en.mercopress.com/rss"),
    # "silent": no terms page; footer "(c) Perfil.com 2006-2026 - All rights reserved".
    ("batimes", "Buenos Aires Times", "https://www.batimes.com.ar/feed"),
    # "silent": no terms page; footer "Copyright (c) 2026 The Kyiv Independent".
    ("kyivindependent", "Kyiv Independent", "https://kyivindependent.com/news-archive/rss/"),
    # "yes": "The headline can be viewed for personal use or on a website, as long as proper formatting is
    # maintained and material is correctly attributed. Any web site displaying the headlines must include
    # a link to AllAfrica home page" (https://allafrica.com/misc/tools/rss.html).
    ("allafrica", "AllAfrica", "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf"),
    # Added on 2026-11-05.
    # "yes", Kyiv Post (https://www.kyivpost.com/pages/terms-and-conditions): "Web links to Kyiv Post material
    # are allowed provided that they contain a hyperlink to the stories and only a brief extract (not
    # more than 10 percent) of the text." The same page: "All Interfax-Ukraine news agency stories
    # cannot be reproduced or distributed in any form without written permission of Interfax-Ukraine."
    # The feed also carries AFP and partner items, which are not Kyiv Post material: THIRD_PARTY_CREDITS.
    ("kyivpost", "Kyiv Post", "https://www.kyivpost.com/feed"),
    # "cc-by", Global Voices (https://globalvoices.org/about/global-voices-attribution-policy/): "all content
    # created by Global Voices is published under a Creative Commons Attribution-Only license", which
    # lets anyone share it "for any purpose, even commercially". Provenance shows the licence link.
    ("globalvoices", "Global Voices", "https://globalvoices.org/feed/"),
    # "ogl", GOV.UK news and communications (Atom). https://www.gov.uk/help/terms-conditions: content "is
    # published under the Open Government Licence (OGL)", and OGL v3 lets anyone "exploit the
    # Information commercially and non-commercially". Government output, not journalism. The feed
    # holds only its newest 20 entries, so this box's 5-minute read is what keeps every one.
    ("govuk", "GOV.UK", "https://www.gov.uk/search/news-and-communications.atom"),
    # "yes", Ukrinform, Ukraine's national news agency (https://www.ukrinform.net/info/subscribe_conditions.html,
    # the site footer): "While citing and using any materials on the Internet, links to the website
    # ukrinform.net not lower than the first paragraph are mandatory." Sam chose to add it with that
    # link (2026-11-05); Provenance shows it under every Ukrinform headline.
    ("ukrinform", "Ukrinform", "https://www.ukrinform.net/rss/block-lastnews"),
    # "yes", Agência Brasil, the news agency of Brazil's public media company EBC, English feed
    # (https://agenciabrasil.ebc.com.br/sobre): "Reprodução autorizada para veículos de comunicação com
    # fins jornalísticos, mediante indicação da fonte." (Reproduction allowed for news media, for
    # journalism, with the source named.) Sam chose the English feed (about 4 a day) on 2026-11-05.
    # Its <description> is the whole article; Provenance shows the headline only.
    ("agenciabrasil", "Agência Brasil", "https://agenciabrasil.ebc.com.br/en/rss/ultimasnoticias/feed.xml"),
    # "yes", Courthouse News Service (https://www.courthousenews.com/terms-of-use/): "We invite you to link
    # the Site, and to make use of CNS' RSS feeds on your website, provided such link or feed does not
    # state or imply any sponsorship of your website by CNS." US courts, every state.
    ("courthousenews", "Courthouse News Service", "https://www.courthousenews.com/feed/"),
    # "silent", each page read on 2026-11-05. Sam chose mainstream newsrooms only. Provenance shows
    # these as headline, time, outlet and link only.
    # Semafor: no terms page (/terms and /terms-of-service answer 404); footer "(c) 2026 Semafor Inc.".
    ("semafor", "Semafor", "https://www.semafor.com/rss.xml"),
    # National Indigenous Times (Atom): no terms page; footer "Copyright (c) 2026 National Indigenous Times".
    # The feed's own address; /feed/ redirects to it, and Cloudflare refuses some clients at /feed/.
    ("nit", "National Indigenous Times", "https://nit.com.au/feed/articles"),
    # Pulse Tasmania: no terms page; "Pulse Tasmania is published by Pulse Media Group Pty Ltd."
    ("pulsetasmania", "Pulse Tasmania", "https://pulsetasmania.com.au/feed/"),
    # City AM: its terms page covers contributors only, and its licensing page sells reprints and says
    # nothing about headlines. Footer "Copyright 2026 City AM Limited".
    ("cityam", "City AM", "https://www.cityam.com/feed/"),
    # Nation.Cymru: no terms page and no copyright line. It runs PA Media wire stories as well as its
    # own: THIRD_PARTY_CREDITS.
    ("nationcymru", "Nation.Cymru", "https://nation.cymru/feed/"),
    # UK Defence Journal: no terms page; footer "(c) 2014-2026 UK Defence Journal, all rights reserved."
    ("ukdefencejournal", "UK Defence Journal", "https://ukdefencejournal.org.uk/feed/"),
    # Asia and Canada, added later on 2026-11-05 ("All but Daily Hive", Sam). Each is a private newsroom
    # whose terms say nothing against showing headlines elsewhere ("silent"); each page was read that
    # day. Their wire and partner items are not covered by that silence: THIRD_PARTY_CREDITS and
    # THIRD_PARTY_CATEGORIES.
    # Asia Times (https://asiatimes.com/terms-of-use-2/): only that its content "will remain the
    # exclusive property of Asia Times and its licensors", and no use of its trademarks.
    ("asiatimes", "Asia Times", "https://asiatimes.com/feed/"),
    # BusinessWorld: the terms link in its footer (https://www.bworldonline.com/news/terms-of-use/)
    # answers 404; footer "(c) 2026 BusinessWorld Publishing. All Rights Reserved."
    ("businessworld", "BusinessWorld", "https://www.bworldonline.com/feed/"),
    # The Phnom Penh Post: https://phnompenhpost.com/terms-of-use/ has a title and no text; footer
    # "(c) 2026 SR Digital Media Co., Ltd. All rights reserved."
    ("phnompenhpost", "The Phnom Penh Post", "https://phnompenhpost.com/feed/"),
    # Onlinekhabar English: https://english.onlinekhabar.com/about-us, /terms-of-use answers 404; footer
    # "(c) 2006-2026 Onlinekhabar.com, All Rights Reserved."
    ("onlinekhabar", "Onlinekhabar English", "https://english.onlinekhabar.com/feed"),
    # Newswire (Sri Lanka): https://www.newswire.lk/about-us/, /terms-of-use/ answers 404; footer
    # "(c) 2026 Wire Digital. All Rights Reserved."
    ("newswirelk", "Newswire.lk", "https://www.newswire.lk/feed/"),
    # The Island (Sri Lanka): /terms-of-use/ answers 404; footer "Copyright (c) 2020 Upali Newspapers (Pvt) Ltd."
    ("theisland", "The Island", "https://island.lk/feed/"),
    # Greater Kashmir: its pages answer a plain request with 403, so they were read through this box's
    # proxy. The footer links only Contact Us, Privacy Policy and GKSC; /terms-and-conditions and
    # /terms-of-use answer 404; "(c) Copyright All right reserved By Greater Kashmir".
    ("greaterkashmir", "Greater Kashmir", "https://www.greaterkashmir.com/rss"),
    # CHEK News: https://cheknews.ca/about-us/news-policies/, /terms-of-use/ answers 404; footer "(c) 2026 CHEK Media".
    ("cheknews", "CHEK News", "https://www.cheknews.ca/feed/"),
    # Cabin Radio: https://cabinradio.ca/about/, /terms-of-use/ answers 404; footer "(c) 2017-2026 506992 NWT
    # Ltd, operating as Cabin Radio."
    ("cabinradio", "Cabin Radio", "https://cabinradio.ca/feed/"),
)

FEED_OUTLET_NAMES = tuple(slug for slug, _, _ in FEEDS)

# Items a feed carries that are not the outlet's own, so its permission does not cover them.
# Matched against each item's dc:creator (or Atom author). Kyiv Post's feed credits AFP, Eurotopics,
# TVP World and ISW on some items (8, 3, 2 and 2 of 100 on 2026-11-05), and its terms name
# Interfax-Ukraine. AFP sells licences only.
THIRD_PARTY_CREDITS: dict[str, re.Pattern[str]] = {
    "kyivpost": re.compile(
        r"^\s*(afp|agence france[- ]presse|reuters|ap|associated press|interfax(-ukraine)?|eurotopics|"
        r"tvp world|isw|institute for the study of war|bloomberg|dpa|ukrinform|unian)\s*$",
        re.IGNORECASE,
    ),
    # "A Agência Brasil publica conteúdos de outros veículos da EBC e de agências parceiras" (the same
    # page): partner agencies' items follow their own rules. EBC's own outlets share its policy.
    "agenciabrasil": re.compile(
        r"^\s*(reuters|afp|agence france[- ]presse|ap|associated press|efe|agencia efe|lusa|ansa|xinhua|"
        r"prensa latina|dpa|bloomberg)\s*$",
        re.IGNORECASE,
    ),
    # Courthouse News runs Associated Press stories on its site (none in the feed on 2026-11-05).
    "courthousenews": re.compile(
        r"^\s*(ap|associated press|the associated press|reuters|afp|agence france[- ]presse)\s*$",
        re.IGNORECASE,
    ),
    # Nation.Cymru runs PA Media wire stories beside its own.
    "nationcymru": re.compile(
        r"^\s*(pa|pa media|press association|pa reporters?|pa political staff|pa news agency)\s*$",
        re.IGNORECASE,
    ),
    # The Phnom Penh Post credits Asia News Network items, and sponsored posts, by those names.
    "phnompenhpost": re.compile(
        r"^\s*(asia news network|ann|sponsored|xinhua|afp|reuters|ap|associated press)\s*$", re.IGNORECASE
    ),
    # Greater Kashmir credits agency items to the agency (PTI on 8 of 50 on 2026-11-05, AP stories among them).
    "greaterkashmir": re.compile(r"^\s*(pti|ians|ani|uni|kns|kno|ap|associated press|reuters|afp)\s*$", re.IGNORECASE),
    # CHEK News credits The Canadian Press stories as "<name>, The Canadian Press".
    "cheknews": re.compile(
        r"(^|,\s*)(the canadian press|canadian press|the associated press|associated press|reuters|afp)\s*$",
        re.IGNORECASE,
    ),
}

# Items a feed marks as someone else's work by a category: Asia Times tags its partners' articles
# ("The Conversation", "Common Dreams"), BusinessWorld its wire stories ("Reuters", 10 of 30).
THIRD_PARTY_CATEGORIES: dict[str, re.Pattern[str]] = {
    "asiatimes": re.compile(
        r"^\s*(the conversation|common dreams|project syndicate|pacific forum|east asia forum|afp|reuters|ap|"
        r"associated press|bloomberg|xinhua)\s*$",
        re.IGNORECASE,
    ),
    "businessworld": re.compile(
        r"^\s*(reuters|afp|agence france[- ]presse|ap|associated press|bloomberg|xinhua)\s*$", re.IGNORECASE
    ),
}

# Items a feed marks as someone else's work by a note in the summary.
THIRD_PARTY_TEXT: dict[str, re.Pattern[str]] = {
    "asiatimes": re.compile(r"originally published by|republished with permission", re.IGNORECASE),
}


def third_party(slug: str, item: dict[str, Any]) -> bool:
    """True when the item is someone else's work, which the outlet's permission does not cover."""
    credit = THIRD_PARTY_CREDITS.get(slug)
    if credit and any(credit.search(creator) for creator in item.get("creators", [])):
        return True
    category = THIRD_PARTY_CATEGORIES.get(slug)
    if category and any(category.search(c.get("name") or "") for c in item.get("categories", [])):
        return True
    text = THIRD_PARTY_TEXT.get(slug)
    return bool(text and text.search(item.get("description") or ""))


def rows_for(slug: str, url: str) -> list[dict[str, Any]]:
    """Every item in one feed as a store row. The outlet's own <category> labels are kept as given."""
    rows = []
    for item in feeds.fetch_items(slug, url):
        if not item["link"] and not item["guid"]:
            continue
        if third_party(slug, item):
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
