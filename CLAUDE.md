# NewsScraper

Scrapes world and regional news from three scraped outlets and 70 RSS feeds. This file is the handoff for the agent that runs the scraper on its host machine.

## Goal of the project

1. Scrape the outlets on a schedule.
2. An AI step (DeepSeek) reads each new story, decides if it reports an event at an identifiable place, groups the same event across outlets and assigns a category.
3. Push the result to Provenance (repo `011-sam-110/Provenance`). Located events become map pins. Everything else goes into a "World news" widget.

Step 1 runs on a schedule. Step 2 is part built: extract (M5) and resolve (M6) exist, cluster
and the quality gate do not. Step 3 is not built. The design is `docs/ARCHITECTURE.md`, agreed
with Sam on 2026-09-15. Build from that document, one milestone at a time (section 14).

## Run

Python 3.13 (3.10 or later should work). Standard library only, except Reuters.

```
pip install -r requirements.txt
python main.py --list-sections
python main.py                                  # every section of every outlet
python main.py --sources bbc pbs --max-pages 3
python main.py --sections reuters:africa -o news
python -m unittest discover -s tests            # 768 tests, offline fixtures
```

Output: `<output-dir>/<outlet>/<section>.jsonl`, one story a line. The exit code is 1 if any outlet failed.

## Layout

- `main.py`: CLI only.
- `scraper/common.py`: HTTP, RSS parsing, `empty_row()` and the one scrape loop (`scrape`, one thread per outlet).
- `scraper/<outlet>.py`: `NAME`, `SECTIONS`, `list_page(section, page, size)`, optional `article_details(url)` and `close()`.
- `scraper/feeds.py`: helpers for feed-only outlets.
- `scraper/rss_outlets.py`: the 70 RSS feeds, one outlet each. The slugs are Provenance's FEEDS ids.
- Register a new outlet in `scraper/__init__.py`.
- `tests/fixtures/<outlet>/`: saved real responses. There is no `tests/__init__.py`, so filter tests with `-k`.
- `newsfeed/`: the hosted pipeline (store, stages). `main.py` does not use it.
- `deploy/`: systemd units, the settings example and the installer for the host.
- `docs/ARCHITECTURE.md`: the design for the AI layer and the Provenance upload.
- `docs/HOST.md`: the runbook for the machine that runs the schedule.
- `sources/incident/`: the Ukraine GeoConfirmed parser, optional DeepSeek interpretation, Supabase writer and CLI.
- `scripts/set-ukraine-incident-env.sh`: hidden-prompt environment setup with a fake-value mode and a current-user-only Windows ACL.

## Ukraine incident worker (DEV only)

Run `python -m sources incident-ukraine [--fixture PATH] [--write] [--no-deepseek]`. The worker keeps dated Ukraine VID/UAV/PIC rows with source coordinates, builds `provenance.incident/1`, and preserves claims and receipts. Structured parsing needs no model key. `DEEPSEEK_KEY_UKRAINE` enables the optional interpretation, which cannot supply coordinates or final prose.

The environment script writes `~/.config/provenance-incident/ukraine.env`. It uses `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SECRET_KEY` and `DEEPSEEK_KEY_UKRAINE`. Never paste those values into a chat, a command line, source control or a log.

A real `--write` needs Sam's separate approval and the dev schema. Production schema stays unapplied. Run the 16 incident tests with `python -m unittest discover -s tests -k incident`.

## Outlets

| Outlet | Method | Text |
|---|---|---|
| Reuters | Section API and article pages, fetched from inside a real Chrome tab (DataDome) | yes |
| BBC | `web-cdn.api.bbci.co.uk` content collections + `__NEXT_DATA__` | yes |
| PBS | Section and tag pages, article HTML, video transcripts | yes |
| 70 RSS feeds | The same feeds Provenance reads (`scraper/rss_outlets.py`), every 5 minutes, status `feed` | no |

In scraped rows, categories come from each outlet's own taxonomy. Do not infer them there. The AI category (`docs/ARCHITECTURE.md` section 6.2) is a separate field in the pipeline. A story listed in several sections gets one row per section (intentional: "the more data the better, prune later").

Rights: Sam dropped the Guardian and NYT on 2026-11-05 (the Guardian's robots header forbids LLM/AI and commercial use; NYT says automated collection needs written permission). Their stored rows are never pushed (`DROPPED_OUTLETS`). The same day Sam dropped every feed whose own terms say no to a commercial site (23 more, each quoted in `scraper/__init__.py`), and the feeds left follow one terms rule (`scraper/rss_outlets.py`). BBC and PBS say no as well, but their website scrapers still run until Sam decides on them. Feed rows cross as headline and link only. A proxy does not change a source's terms. Never bypass a paywall, a login or a subscription wall, and never use a CAPTCHA-solving service.

## Proxy

`NEWS_PROXY_URL` is a rotating residential gateway, set only by `deploy/set-news-key.sh` (hidden prompt, checked before it is written). Every outlet is read directly first; a block (401, 403, 429, 451, 503, a timeout or a refused connection) is tried ONCE more through the proxy (`scraper/common.py`, `fetch`). The production site's own feed reads stay direct, and no proxy secret goes onto the production box. Reuters' Chrome uses `NEWS_SCRAPER_REUTERS_PROXY` (off, retry or always). A signed-in Chrome profile (`NEWS_SCRAPER_CHROME_PROFILE`) stays off the proxy by default, because a session that jumps between exit IPs looks stolen. Never read or copy that profile's cookies or passwords.

Every log line and error that can hold the proxy URL goes through `redact()`, and errors are raised without the chained cause that holds the URL. `tests/test_proxy.py` fails if the password can reach a log, an exception or the store.

## Host setup: do this first

Reuters is the risk. It launches installed Google Chrome with a visible window (`scraper/reuters.py`, `channel="chrome"`, `headless=False`). DataDome returns 401 to Playwright's bundled Chromium, Chrome for Testing, curl and curl_cffi. Do not change the user agent.

1. Install Google Chrome (the real browser, not `playwright install chromium`).
2. On Linux, Reuters needs the seat's REAL display. `xvfb-run` does not work: with no GPU,
   Chrome reports the SwiftShader renderer and DataDome answers every section with 401.
   Measured on the Fedora host on 2026-09-18, same public IP, minutes apart: real display 200,
   xvfb-run 401, `xvfb-run --use-gl=egl` still SwiftShader, `--use-angle=vulkan` no WebGL at
   all. `deploy/reuters-display.sh` finds the display and refuses to run without one.
3. Prove Reuters works on this machine before anything else:
   `python main.py --sections reuters:africa --max-pages 1 -o /tmp/probe`
   Pass = rows saved and exit code 0. A 401/403 means DataDome refused this machine or network. A VPN can cause it. With `NEWS_PROXY_URL` set, a refused Chrome session is retried once through the proxy.
4. Only a home IP is proven to work (2026-09-14). If Reuters fails here, report it to Sam. Run the others without it (`--sources bbc pbs` and `--sources feeds`).
   The 2026-09-15 reading that Xvfb was proven here was wrong, and cost three days of Reuters:
   only its FIRST section succeeded, and 23 of the next 24 hourly runs failed on every section.
   On the real display the same unit fetched 296 rows and 137 new stories with 0 failures
   (2026-09-18). That was true of a run started BY HAND, and it did not hold for one scheduled
   run: all four between 01:25 and 09:21 took exactly 8 rows and answered 401 to everything
   after the first page. The cause was `WAYLAND_DISPLAY`, which the systemd user manager sets
   and a login shell does not, so Chrome took the Wayland backend and reported a different WebGL
   renderer. `deploy/reuters-display.sh` now forces the X11 path. A run that reports exactly 8
   rows and fails every section after the first is this fault, not a rate limit: a rate limit
   refuses the first section too. See `docs/HOST.md`.
   DataDome does also rate-limit a session, so Reuters stays scheduled slower and shallower
   than the other four and is never backfilled. That is worth doing on its own, but it was not
   the cause of the outage and making it gentler did not fix anything.
   The rate limit is real and was measured on 2026-09-18, same machine, same IP, real display:
   a 15-section 3-page run at 01:24 took 296 rows with 0 failures, and the identical run an
   hour later answered 401 on all 15 sections from the first one. The timer is therefore every
   three hours, not hourly (`deploy/systemd/newsfeed-scrape-reuters.timer`). Two runs is an
   estimate, not a measured limit.
5. Keep pipeline state out of synced folders. The SQLite databases go in `NEWSFEED_DATA_DIR` (default `%LOCALAPPDATA%\NewsScraper` on Windows). This checkout lives under OneDrive, and sync corrupts SQLite write-ahead logs.

## Known gaps before this can run on a schedule

1. ~~**Every run overwrites the last run.**~~ Closed by M1. A scheduled run writes to the store, not to JSON Lines. `main.py` still writes JSON Lines, and still with `"w"`, which is what a one-shot CLI should do.
2. ~~**Nothing persists between runs.**~~ Closed by M1. Stories, aliases and section labels live in SQLite, and a section stops at the first listing page holding no new story.
3. ~~**No health check.**~~ Closed on 2026-11-05: `python -m newsfeed health` runs every 15 minutes and posts to the agent hub when a check fails or recovers (`newsfeed/health.py`).

## The hosted pipeline

`newsfeed/` is the hosted pipeline, designed in `docs/ARCHITECTURE.md` and built one milestone at a time. `docs/HOST.md` is the runbook for the host machine.

```
python -m newsfeed scrape --sources bbc pbs               # M1, on an hourly systemd timer
python -m newsfeed scrape --sources feeds                 # the 70 RSS feeds, every 5 minutes
python -m newsfeed status                                  # what the store holds, per outlet
python -m newsfeed extract                                 # M5, DeepSeek, not yet on a timer
python -m newsfeed geonames-build                          # M6, monthly, downloads the dumps
python -m newsfeed resolve --dry-run                       # M6, place name to coordinate
python -m newsfeed cluster                                 # M7, same event across outlets
python -m newsfeed rail --dry-run                          # push stories to Provenance's LIVE news rail
python -m newsfeed health --dry-run                        # every health check, nothing posted
python -m newsfeed drop-outlets                            # dry run: what forgetting the dropped outlets removes
python -m newsfeed extract --feeds                         # the feed rows, read from headline and RSS summary
python -m newsfeed verify --dry-run                         # how many headline pins wait for the second check
python -m newsfeed headline-places --dry-run                # the places Provenance's Headline sources would get
```

Built: the store and scrape (M1), extract (M5), the gazetteer and resolve (M6), cluster (M7), the
snapshot BUILDER half of publish (M10, `newsfeed/publish.py`) and the independent reader of the
section 8.1 contract that checks it (`newsfeed/contract.py`). Publish cannot send: it posts to
`/api/ingest/newsfeed`, which answers 404 because Provenance PR A was never written, not because it
is dormant (checked against `origin/main` on 2026-09-18, section 9.0). Pins would stay withheld
anyway until the M8 gate passes, which cannot happen before 2026-09-25, seven days after the prompt
freeze. The `rail` stage (`newsfeed/rail.py`) pushes scraped stories to Provenance's ALREADY LIVE
`POST /api/news/ingest`, which is a different contract from section 8 and does not wait for
the gate: see section 9.0. It needs `NEWSFEED_INGEST_SECRET`, the same value as the box's
`NEWS_INGEST_SECRET`. Eval (M4) exits 2 and names its milestone. Health is built. The store lives
in `NEWSFEED_DATA_DIR`, never in this checkout.

**Headline sources** (`newsfeed/headline_places.py`): `extract --feeds` reads the feed rows with the same prompt and checks, and the row keeps status `feed`, so resolve and cluster never read it and the rail sends it with no event. `verify` (`newsfeed/verify.py`, section 7.9) asks a fresh DeepSeek call to judge every pin, the GeoNames place included, and a critic call settles any flag; only `verified` and `kept_after_review` pins are sent, and it fails closed (no budget, a 402 or two bad answers: no pin that run). `headline-places` sends one signed relay snapshot (`/api/relay/headline-places`, `newsfeed/relay.py`) of each verified feed pin: its URL, the GeoNames place (point, district or city, never a climbed answer except a venue GeoNames lacks, placed on its city at area precision) and the verified quote. Courts and opinion stories never pin (`taxonomy.can_pin`). The snapshot's `configHash` is the placement hash (extract, resolve, verify and these rules). Provenance uses it only when its switch is on, after the labelled gate. The units are `deploy/systemd/newsfeed-headline-places.*`, not installed.

## Design summary

The full design is `docs/ARCHITECTURE.md`. In short:

- Huginn (github.com/huginn/huginn) was reviewed on 2026-09-15 for ideas only. Do not adopt it; it is a Ruby on Rails app. The mapping is in section 5.
- Separate stages (scrape, extract, resolve, cluster, publish, health) over one SQLite database, each resumable, each run by the host's scheduler. No long-running loop in Python.
- DeepSeek extracts and checks same-event matches. GeoNames supplies every coordinate.
- Publish pushes one HMAC-signed snapshot to Provenance over HTTPS, every minute when it changed.
- A pin needs a physical happening at point, district or city precision, a verbatim quote and an event date inside the window. Everything else is a World news item.
- No pin is published until the quality gate passes: at most 3 wrong in 153 labelled pins.

## Rules for the AI layer

Provenance's GDELT layer taught this: 76.5% of its rows were not a real event of the coded type at the coded place, and almost all of that was bad labelling, not bad geocoding. A court verdict about a shooting was coded as a shooting.

- First decide if the story reports a physical happening at a place. Commentary, obituaries, verdicts about old events, policy stories and profiles are not. They go to World news with a stored reason.
- Every pin carries a verbatim quote from the text that contains the place name, and a precision: point, district or city. Region and country are never pinned.
- Get coordinates from GeoNames, never from numbers the model writes.
- Never show or send text a model wrote. A pin's title is the lead outlet's headline, verbatim.
- An outlet count is not corroboration. A dateline is not the event place.
- Measure against the labelled gate (section 10) before any pin is published, and again after any change to the prompts, the model or the thresholds.

## Provenance side

Provenance gets a new signed ingest route and keeps the latest snapshot in process memory, because its served code may not write files. The snapshot contract `provenance.newsfeed/1` was agreed with Sam on 2026-09-15 (section 8). Do not change the contract without Sam. The Provenance changes are two pull requests, described in section 9. Provenance runs on AWS Lightsail at provenance-online.com.

## Conventions

- Stage explicit paths. Never `git add -A`.
- Commits are solo attribution: no AI co-author trailer.
- No en dashes or em dashes in docs or user-facing text.
- Test every parser against a saved real response in `tests/fixtures/`.
- The repo is public. Never commit keys, secrets, article text or real snapshots. Label files carry ids, hashes and labels only.
