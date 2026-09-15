# NewsScraper

Scrapes world and regional news from five outlets into JSON Lines. This file is the handoff for the agent that runs the scraper on its host machine.

## Goal of the project

1. Scrape the outlets on a schedule.
2. An AI step (DeepSeek) reads each new story, decides if it reports an event at an identifiable place, groups the same event across outlets and assigns a category.
3. Push the result to Provenance (repo `011-sam-110/Provenance`). Located events become map pins. Everything else goes into a "World news" widget.

Today only step 1 exists, and only as a one-shot CLI. Steps 2 and 3 are designed in `docs/ARCHITECTURE.md`, agreed with Sam on 2026-09-15, and not built. Build from that document, one milestone at a time (section 14).

## Run

Python 3.13 (3.10 or later should work). Standard library only, except Reuters.

```
pip install -r requirements.txt
python main.py --list-sections
python main.py                                  # every section of every outlet
python main.py --sources bbc pbs --max-pages 3
python main.py --sections reuters:africa -o news
python -m unittest discover -s tests            # 136 tests, offline fixtures
```

Output: `<output-dir>/<outlet>/<section>.jsonl`, one story a line. The exit code is 1 if any outlet failed.

## Layout

- `main.py`: CLI only.
- `scraper/common.py`: HTTP, RSS parsing, `empty_row()` and the one scrape loop (`scrape`, one thread per outlet).
- `scraper/<outlet>.py`: `NAME`, `SECTIONS`, `list_page(section, page, size)`, optional `article_details(url)` and `close()`.
- `scraper/feeds.py`: helpers for feed-only outlets (NYT).
- Register a new outlet in `scraper/__init__.py`.
- `tests/fixtures/<outlet>/`: saved real responses. There is no `tests/__init__.py`, so filter tests with `-k`.
- `docs/ARCHITECTURE.md`: the design for the AI layer and the Provenance upload.

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
2. On Linux with no display, run under a virtual display: `xvfb-run -a python main.py ...`.
3. Prove Reuters works on this machine before anything else:
   `python main.py --sections reuters:africa --max-pages 1 -o /tmp/probe`
   Pass = rows saved and exit code 0. A 401/403 means DataDome refused this machine or network. A VPN or rotating proxy can cause it. `NEWS_SCRAPER_PROXY` sets a proxy for every outlet.
4. Only a home IP is proven to work (2026-09-14). If Reuters fails here, report it to Sam. Run the other four outlets without it (`--sources bbc guardian pbs nyt`).
5. Keep pipeline state out of synced folders. The SQLite databases go in `NEWSFEED_DATA_DIR` (default `%LOCALAPPDATA%\NewsScraper` on Windows). This checkout lives under OneDrive, and sync corrupts SQLite write-ahead logs.

## Known gaps before this can run on a schedule

These are real defects for a hosted job. Fix them before adding a scheduler.

1. **Every run overwrites the last run.** `scrape_source` opens each section file with `"w"`.
2. **Nothing persists between runs.** The seen set lives only while the process runs. With `--max-pages 0` each run walks every section back to where the listing ends (up to 500 pages for PBS).
3. **No health check.** An outlet can stop sending new stories and nothing fails.

The fixes are designed in `docs/ARCHITECTURE.md`. The SQLite store and the early section stop (sections 7.4 and 7.5, milestone M1) close gaps 1 and 2. The health stage (section 7.11, milestone M10) closes gap 3.

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
