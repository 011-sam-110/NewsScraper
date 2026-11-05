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
- Trinidad Express and HumAngle, which passed the rule in the wide hunt of the same day: both answered
  HTTP 429 (HumAngle's body is a bot check), so they wait. Diálogo Américas too: from 12:44 UTC its
  feed answered with Cloudflare's "Just a moment..." check.

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
    # The wide hunt, added later on 2026-11-05 (the owner: "yes to all"). Government press offices, public
    # broadcasters, state agencies and newsrooms, region by region; Provenance shows each as headline,
    # time, outlet and link only. Trinidad Express, HumAngle and Diálogo Américas are not here (see above).
    # United States: federal press offices, VOA and Stars and Stripes
    # public domain: https://www.dvidshub.net/about/copyright: "Government employees as part of their official duties are
    # not eligible for copyright protection in the United States." and "Commercial Use Authorized." News
    # from US military units in every theatre.
    ("dvids", "DVIDS", "https://www.dvidshub.net/rss/news"),
    # public domain: https://www.nasa.gov/nasa-brand-center/images-and-media/: "News outlets, schools, and text-book authors
    # may use NASA content without needing explicit permission, subject to compliance with these guidelines."
    ("nasa", "NASA", "https://www.nasa.gov/news-release/feed/"),
    # public domain: https://www.state.gov/copyright-information/: "Unless a copyright is indicated, information on State
    # Department websites is in the public domain and may be copied and distributed without permission."
    ("statedept", "U.S. State Department", "https://www.state.gov/rss-feed/press-releases/feed/"),
    # public domain: The department's own policy, on DVIDS (https://www.dvidshub.net/about/copyright): "Government employees as
    # part of their official duties are not eligible for copyright protection in the United States." The
    # war.gov notice answered 403. The feed holds 10 items.
    ("dodnews", "Department of War News", "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10"),
    # public domain: The same department and policy as dodnews: contracts and official statements. The feed holds 10 items.
    ("dodreleases", "Department of War Releases", "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=10"),
    # public domain: https://www.justice.gov/legalpolicies: "Unless otherwise indicated, information on Department of Justice
    # websites is in the public domain and may be copied and distributed without permission."
    ("justice", "U.S. Department of Justice", "https://www.justice.gov/news/rss?type=press_release&m=1"),
    # silent (US federal work): No copyright statement found: https://www.noaa.gov/disclaimer covers external links only. US federal works
    # have no US copyright, but no NOAA page says so, so it is "silent".
    ("noaa", "NOAA", "https://www.noaa.gov/rss.xml"),
    # public domain: https://www.voanews.com/p/5338.html: "All text, audio and video material produced exclusively by the Voice
    # of America is in the public domain." The same page: VOA holds licences from AFP, AP and Reuters, whose
    # material "may not be copied, published or redistributed": an item that names one of them is dropped.
    ("voa", "Voice of America", "https://www.voanews.com/api/"),
    # silent: No reuse terms: https://www.stripes.com/help/disclaimer covers its authorization and adverts only, and the
    # "Reprint Permissions" page shows no text. A story credited to a wire is dropped.
    ("stripes", "Stars and Stripes", "https://subscribe.stripes.com/rss/top-news.xml"),
    # Canada
    # yes (Statistics Canada Open Licence): https://www.statcan.gc.ca/en/terms-conditions: "This licence allows you to use Statistics Canada information
    # without restrictions on sharing and redistribution, for commercial and non-commercial purposes." The licence
    # (https://www.statcan.gc.ca/en/reference/licence) asks for its notice, which the card prints. Data releases
    # of The Daily, not news; the Atom title is XHTML.
    ("statcan", "Statistics Canada", "https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom"),
    # yes: https://www.bankofcanada.ca/terms/: "the Bank permits you to freely use, copy, distribute and transmit its
    # website content under the following terms", the first of which is "You must attribute the Bank of Canada as
    # the source". The same page bars its logo and wordmark, so its row has no favicon. Rate decisions.
    ("bankofcanada", "Bank of Canada", "https://www.bankofcanada.ca/feed/?utility=news"),
    # Latin America and the Caribbean
    # public domain, credit the source: https://www2.camara.leg.br/termo-de-uso-e-politica-de-privacidade: "Todas as informações publicadas no Portal
    # da Câmara são de domínio público, gratuitas e podem ser reproduzidas desde que citada a fonte "Câmara dos
    # Deputados"". (Public domain; name the source.) The outlet name is that source. Portuguese.
    ("agenciacamara", "Câmara dos Deputados", "https://www.camara.leg.br/noticias/rss/ultimas-noticias"),
    # yes, credit Agência Senado: https://www12.senado.leg.br/noticias/politica-de-uso: "A reprodução de matérias e fotografias é livre, desde
    # que não haja descaracterização de conteúdo e mediante a citação da Agência Senado e do autor." (Free to
    # reproduce, unaltered, naming Agência Senado and the author; the feed names no author.) Its items carry no
    # <link>: the address is the <guid>, which parseRss reads. Portuguese.
    ("agenciasenado", "Agência Senado", "https://www12.senado.leg.br/noticias/rss.xml"),
    # silent: No terms page found (https://jis.gov.jm/); footer "© 2026 The Jamaica Information Service. All Rights
    # Reserved". Government press.
    ("jis", "Jamaica Information Service", "https://jis.gov.jm/feed/"),
    # silent: No terms page: /terms, /terms-of-use and /terms-and-conditions answer 404, and
    # https://mexiconewsdaily.com/mexico-news-daily-privacy-policy/ covers personal data only.
    ("mexiconewsdaily", "Mexico News Daily", "https://mexiconewsdaily.com/feed/"),
    # silent: No terms page: /terms, /terms-of-use and /terms-and-conditions answer 404 (https://dominicantoday.com/). It
    # runs EFE stories, marked "(EFE)" in the text: dropped.
    ("dominicantoday", "Dominican Today", "https://dominicantoday.com/feed/"),
    # silent: No terms page: /terms redirects to an article; footer "© 2026 The Tico Times". Items credited "AFP" or
    # "Sponsored Post" are not its own: dropped.
    ("ticotimes", "The Tico Times", "https://ticotimes.net/feed"),
    # silent: No terms page; footer "Department of Public Information, Guyana. All rights reserved." Government press.
    ("dpiguyana", "DPI Guyana", "https://dpi.gov.gy/feed/"),
    # silent: No terms page; https://jamaica-gleaner.com/about-us footer "© 2026 The Gleaner Company (Media) Limited. A
    # member of the RJRGLEANER Communications Group. All Rights Reserved."
    ("gleaner", "The Gleaner", "https://jamaica-gleaner.com/feed/rss.xml"),
    # silent: No terms page; https://latinamericareports.com/about/ footer "2018 Espacio Media Incubator, All Rights
    # Reserved". Articles "originally published" elsewhere and "re-published with permission" are dropped.
    ("latamreports", "Latin America Reports", "https://latinamericareports.com/feed/"),
    # Europe
    # silent: The English footer has no terms link, only "© 2026. All rights reserved"; https://apie.lrt.lt/en has no
    # terms page either. Lithuania's public broadcaster: the Baltic, defence, the Belarus border.
    ("lrt", "LRT", "https://www.lrt.lt/en/news-in-english?rss"),
    # silent: No terms page: https://www.themoscowtimes.com/page/terms-of-use answers "We couldn't find your page."; footer
    # "© The Moscow Times, all rights reserved." Its RSS page only invites readers to add the feeds to "your RSS reader".
    ("moscowtimes", "The Moscow Times", "https://www.themoscowtimes.com/rss/news"),
    # yes (EU reuse policy): https://www.europarl.europa.eu/legal-notice/en: "the reuse (reproduction or use) of textual data ... is
    # authorised, for personal use or for further non-commercial or commercial dissemination, provided that the
    # entire item is reproduced and the source is acknowledged", and a partial one "must also cite the URL link
    # of the complete item". Each headline links its item.
    ("europarl", "European Parliament", "https://www.europarl.europa.eu/rss/doc/press-releases/en.xml"),
    # yes: https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html: "users of this website may make
    # free use of the information obtained directly from it subject to the following conditions: When such
    # information is distributed or reproduced, it must appear accurately and the ECB must be cited as the source."
    ("ecb", "European Central Bank", "https://www.ecb.europa.eu/rss/press.html"),
    # Middle East
    # yes: https://en.globes.co.il/en/article.aspx?did=850820: "You may use the feeds on a web page as long as you do not
    # post the full text of Globes articles, and as long as you provide attribution to the Globes website and
    # retain the link to the original article." Its pictures are often credited to Reuters.
    ("globes", "Globes", "https://en.globes.co.il/webservice/rss/rssfeeder.asmx/FeederNode?iID=1725"),
    # silent: No terms page: the footer links a Privacy Policy only, and says "Copyright 2026. All Rights Reserved. Doha News".
    ("dohanews", "Doha News", "https://dohanews.co/feed/"),
    # Africa
    # yes, credit SAnews: https://www.sanews.gov.za/content/user-policy: "Media are welcome to utilise all stories, pictures and other
    # material on this site ... at no cost, provided SAnews is credited." The card prints the credit.
    ("sanews", "SAnews", "https://www.sanews.gov.za/rss.xml"),
    # yes (brief quote with credit and link): https://www.namibian.com.na/terms-of-use/: "you may quote briefly from them with credit and a link", and "You
    # may not republish whole articles, scrape the site". A headline with its outlet and link is that brief quote.
    ("namibian", "The Namibian", "https://www.namibian.com.na/feed/"),
    # silent: No terms page: /terms-of-use/ and /terms-and-conditions/ answer 404, and https://www.vanguardngr.com/about/
    # has no reuse line. It runs AFP stories ("an AFP reporter said") and News Agency of Nigeria copy: an
    # item that names either is dropped.
    ("vanguard", "Vanguard", "https://www.vanguardngr.com/feed/"),
    # silent: No terms page: /terms/, /terms-and-conditions/, /terms-of-use/ and /copyright/ answer 404; footer "©
    # Mail & Guardian" (https://mg.co.za/about-us/). Its paid "partner content" (6 of 50 items that day) is dropped
    # by its address.
    ("mailguardian", "Mail & Guardian", "https://mg.co.za/rss/"),
    # silent: https://dailytrust.com/terms-of-use/ covers reader posts only and says nothing about reuse of its stories.
    # Sponsored posts (a category) and News Agency of Nigeria copy ("(NAN)") are dropped.
    ("dailytrust", "Daily Trust", "https://dailytrust.com/feed/"),
    # silent: No terms page (/terms-of-use/ answers 404); https://www.channelstv.com/aboutus/ footer "© Channels
    # Incorporated Limited. All rights reserved." It runs AFP stories: an item that names AFP, another wire or
    # NAN is dropped.
    ("channelstv", "Channels Television", "https://www.channelstv.com/feed/"),
    # Asia
    # yes: https://www.taiwantoday.tw/Disclaimer: "Articles produced by Taiwan Today can be republished on the condition
    # the source and link are cited." The same page asks permission for other stories, so only items that end
    # with its own "Write to Taiwan Today" line are kept. Dates carry no time ("2026/11/05").
    ("taiwantoday", "Taiwan Today", "https://api.taiwantoday.tw/en/rss.php?unit=2,6,10,15,18"),
    # silent: No terms page and no KOGL mark on https://www.korea.net/Others/Subscribe-to-Koreanet/RSS-Service or on an
    # article; the feed says "Copyright © by KOCIS. All rights reserved." Yonhap copy and photos are dropped.
    ("koreanet", "Korea.net", "https://www.korea.net/koreanet/rss/news/2"),
    # silent: No terms page: https://www.app.com.pk/about-us/ ends "Associated Press of Pakistan ©2026 - All Rights
    # Reserved." Pakistan's state news agency, typed "State media". The feed holds 10 items, about 1.6 h of
    # them: the NewsScraper host reads it every 5 minutes and keeps every item.
    ("app", "Associated Press of Pakistan", "https://www.app.com.pk/feed/"),
    # silent: No terms page; footer "Copyright © 2026. The Rising Nepal". Published by the Gorkhapatra Corporation, "an
    # undertaking of the Government of Nepal": typed "State media". The feed holds 10 items, about 4 h of them:
    # the NewsScraper host reads it every 5 minutes.
    ("risingnepal", "The Rising Nepal", "https://risingnepaldaily.com/rss"),
    # silent: No terms page; footer "© 2024 BBSCL. All rights reserved." Bhutan's public broadcaster.
    ("bbsbhutan", "Bhutan Broadcasting Service", "https://www.bbs.bt/feed/"),
    # silent: No terms page; https://en.tatoli.tl/about-us/ footer "© 2026 TATOLI. All rights reserved." Timor-Leste's
    # news agency, a public institute.
    ("tatoli", "Tatoli", "https://en.tatoli.tl/feed/"),
    # silent (not fully read): No terms page found: the footer lists "Normative documents" and "About the Agency" with no reuse line. The
    # national news agency of Uzbekistan, typed "State media". The owner added it on 2026-11-05.
    ("uza", "UzA", "https://uza.uz/en/rss"),
    # Oceania
    # CC BY 4.0: https://mypolice.qld.gov.au/ footer: a "Creative Commons Attribution 4.0 International" badge beside "© The
    # State of Queensland 2026 (Queensland Police Service)". https://www.qld.gov.au/legal/copyright/: "Unless
    # otherwise noted, all copyright material ... is licensed under a Creative Commons Attribution 4.0
    # International licence". The card links the licence.
    ("qldpolice", "Queensland Police Service", "https://mypolice.qld.gov.au/feed/"),
    # silent: No terms page; footer "© ALL RIGHTS RESERVED FRONT PAGE PTE LIMITED". Its feed also carries the PACNEWS
    # bulletins of the Pacific Islands News Association, credited "PINA": dropped.
    ("islandsbusiness", "Islands Business", "https://islandsbusiness.com/feed/"),
    # yes: https://www.beehive.govt.nz/copyright: "You can reproduce (including through translation) this content free of
    # charge without further permission, as long as you: reproduce the material accurately; acknowledge the source
    # of the material."
    ("beehive", "New Zealand Government", "https://www.beehive.govt.nz/rss.xml"),
    # CC BY 4.0: https://www.pm.gov.au/copyright: "With the exception of the Coat of Arms and all photos and graphics, this website
    # is licensed under a Creative Commons Attribution 4.0 International Licence." No favicon (the Coat of Arms).
    # Dates carry no time ("Tuesday 3 November 2026").
    ("pmaustralia", "Prime Minister of Australia", "https://www.pm.gov.au/rss.xml"),
    # CC BY 4.0: https://www.rba.gov.au/copyright/: "most other material published on this website is provided under a Creative
    # Commons Attribution 4.0 International License". Rate decisions; the feed holds its newest release.
    ("rba", "Reserve Bank of Australia", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml"),
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
    # The wide hunt (2026-11-05): VOA's licensed wire copy, Stars and Stripes' wire stories, The Tico
    # Times' AFP and sponsored posts, Islands Business's PACNEWS bulletins (credited "PINA").
    "voa": re.compile(r"\((associated press|ap|reuters|afp|agence france[- ]presse)\)|^\s*(associated press|ap|reuters|afp|agence france[- ]presse)\s*$", re.IGNORECASE),
    "stripes": re.compile(r"(^|,\s*|\s+and\s+)(the associated press|associated press|ap|reuters|afp|agence france[- ]presse|bloomberg|the washington post|the new york times|tribune news service)\s*$", re.IGNORECASE),
    "ticotimes": re.compile(r"^\s*(afp|agence france[- ]presse|efe|reuters|ap|associated press|sponsored post|sponsored)\s*$", re.IGNORECASE),
    "islandsbusiness": re.compile(r"^\s*(pina|pacnews|afp|reuters|rnz)\s*$", re.IGNORECASE),
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
    # Daily Trust tags a sponsored post "Sponsored Post".
    "dailytrust": re.compile(r"^\s*sponsored( post)?\s*$", re.IGNORECASE),
}

# Items a feed marks as someone else's work by a note in the summary or the full text (content:encoded,
# read for this check and never stored). Channels TV and Vanguard name AFP only in the full text.
# Taiwan Today asks permission for stories that are not its own, so its rule keeps only the items that
# end with its own "Write to Taiwan Today" line: the pattern matches when that line is missing.
THIRD_PARTY_TEXT: dict[str, re.Pattern[str]] = {
    "asiatimes": re.compile(r"originally published by|republished with permission", re.IGNORECASE),
    "voa": re.compile(r"associated press|\breuters\b|agence france[- ]presse|\bafp\b", re.IGNORECASE),
    "dominicantoday": re.compile(r"\b(efe|afp|reuters)\b|associated press", re.IGNORECASE),
    "latamreports": re.compile(r"originally published|re-published with permission|republished with permission", re.IGNORECASE),
    "vanguard": re.compile(r"\bafp\b|\breuters\b|associated press|\(nan\)|news agency of nigeria", re.IGNORECASE),
    "dailytrust": re.compile(r"\(nan\)|news agency of nigeria|\bafp\b|\breuters\b|associated press", re.IGNORECASE),
    "channelstv": re.compile(r"\bafp\b|\breuters\b|associated press|\(nan\)|news agency of nigeria", re.IGNORECASE),
    "taiwantoday": re.compile(r"^(?![\s\S]*write to taiwan today)", re.IGNORECASE),
    "koreanet": re.compile(r"\byonhap\b|\bnewsis\b|\bnews1\b|\bafp\b|\breuters\b|associated press", re.IGNORECASE),
}

# Items a feed marks as someone else's work by their address: Mail & Guardian's paid "partner content".
THIRD_PARTY_LINKS: dict[str, re.Pattern[str]] = {
    "mailguardian": re.compile(r"\/partner-content\/", re.IGNORECASE),
}


def third_party(slug: str, item: dict[str, Any]) -> bool:
    """True when the item is someone else's work, which the outlet's permission does not cover."""
    credit = THIRD_PARTY_CREDITS.get(slug)
    if credit and any(credit.search(creator) for creator in item.get("creators", [])):
        return True
    category = THIRD_PARTY_CATEGORIES.get(slug)
    if category and any(category.search(c.get("name") or "") for c in item.get("categories", [])):
        return True
    link = THIRD_PARTY_LINKS.get(slug)
    if link and link.search(item.get("link") or ""):
        return True
    text = THIRD_PARTY_TEXT.get(slug)
    words = " ".join(part for part in (item.get("description"), item.get("content")) if part)
    return bool(text and text.search(words))


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
