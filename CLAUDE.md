# NewsScraper

Scrapes world and regional news from five outlets into JSON Lines. This file is the handoff for the agent that runs the scraper on its host machine.

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
python -m unittest discover -s tests            # 214 tests, offline fixtures
```

Output: `<output-dir>/<outlet>/<section>.jsonl`, one story a line. The exit code is 1 if any outlet failed.

## Layout

- `main.py`: CLI only.
- `scraper/common.py`: HTTP, RSS parsing, `empty_row()` and the one scrape loop (`scrape`, one thread per outlet).
- `scraper/<outlet>.py`: `NAME`, `SECTIONS`, `list_page(section, page, size)`, optional `article_details(url)` and `close()`.
- `scraper/feeds.py`: helpers for feed-only outlets (NYT).
- Register a new outlet in `scraper/__init__.py`.
- `tests/fixtures/<outlet>/`: saved real responses. There is no `tests/__init__.py`, so filter tests with `-k`.
- `newsfeed/`: the hosted pipeline (store, stages). `main.py` does not use it.
- `deploy/`: systemd units, the settings example and the installer for the host.
- `docs/ARCHITECTURE.md`: the design for the AI layer and the Provenance upload.
- `docs/HOST.md`: the runbook for the machine that runs the schedule.

## Outlets

| Outlet | Method | Text |
|---|---|---|
| Reuters | Section API and article pages, fetched from inside a real Chrome tab (DataDome) | yes |
| BBC | `web-cdn.api.bbci.co.uk` content collections + `__NEXT_DATA__` | yes |
| Guardian | RSS + `?page=N` + `<article>.json` | yes |
| PBS | Section and tag pages, article HTML, video transcripts | yes |
| NYT | RSS feeds only | no |

In scraped rows, categories come from each outlet's own taxonomy. Do not infer them there. The AI category (`docs/ARCHITECTURE.md` section 6.2) is a separate field in the pipeline. A story listed in several sections gets one row per section (intentional: "the more data the better, prune later").

Rights: the Guardian's robots header forbids LLM/AI and commercial use, and NYT says automated collection needs written permission. Sam knows this and decided on 2026-09-15 to include all five outlets. Do not remove them. Do not add paywall bypass.

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
   Pass = rows saved and exit code 0. A 401/403 means DataDome refused this machine or network. A VPN or rotating proxy can cause it. `NEWS_SCRAPER_PROXY` sets a proxy for every outlet.
4. Only a home IP is proven to work (2026-09-14). If Reuters fails here, report it to Sam. Run the other four outlets without it (`--sources bbc guardian pbs nyt`).
   The 2026-09-15 reading that Xvfb was proven here was wrong, and cost three days of Reuters:
   only its FIRST section succeeded, and 23 of the next 24 hourly runs failed on every section.
   On the real display the same unit fetched 296 rows and 137 new stories with 0 failures
   (2026-09-18). See `docs/HOST.md`.
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
3. **No health check.** An outlet can stop sending new stories and nothing fails. Still open: the health stage is milestone M10. Until then `python -m newsfeed status` is the manual check.

## The hosted pipeline

`newsfeed/` is the hosted pipeline, designed in `docs/ARCHITECTURE.md` and built one milestone at a time. `docs/HOST.md` is the runbook for the host machine.

```
python -m newsfeed scrape --sources bbc guardian pbs nyt   # M1, on an hourly systemd timer
python -m newsfeed status                                  # what the store holds, per outlet
python -m newsfeed extract                                 # M5, DeepSeek, not yet on a timer
python -m newsfeed geonames-build                          # M6, monthly, downloads the dumps
python -m newsfeed resolve --dry-run                       # M6, place name to coordinate
```

Built: the store and scrape (M1), extract (M5), and the gazetteer and resolve (M6). cluster (M7),
publish and health (M10) and eval (M4) exit 2 and name their milestone. The store lives in `NEWSFEED_DATA_DIR`, never in this checkout.

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
