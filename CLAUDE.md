# NewsScraper

Scrapes world and regional news from five outlets into JSON Lines. This file is the handoff for the agent that runs the scraper on its host machine.

## Goal of the project

1. Scrape the outlets on a schedule.
2. An AI step reads each new story, decides if it reports a real event at a real place, and extracts coordinates when it can.
3. Upload the located events to Provenance (repo `011-sam-110/Provenance`).

Today only step 1 exists, and only as a one-shot CLI. Steps 2 and 3 are not built.

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

## Outlets

| Outlet | Method | Text |
|---|---|---|
| Reuters | Section API and article pages, fetched from inside a real Chrome tab (DataDome) | yes |
| BBC | `web-cdn.api.bbci.co.uk` content collections + `__NEXT_DATA__` | yes |
| Guardian | RSS + `?page=N` + `<article>.json` | yes |
| PBS | Section and tag pages, article HTML, video transcripts | yes |
| NYT | RSS feeds only | no |

Categories come from each outlet's own taxonomy. Do not infer them. A story listed in several sections gets one row per section (intentional: "the more data the better, prune later").

Rights: the Guardian's robots header forbids LLM/AI and commercial use, and NYT says automated collection needs written permission. Sam knows this and decided on 2026-09-15 to include all five outlets. Do not remove them. Do not add paywall bypass.

## Host setup: do this first

Reuters is the risk. It launches installed Google Chrome with a visible window (`scraper/reuters.py`, `channel="chrome"`, `headless=False`). DataDome returns 401 to Playwright's bundled Chromium, Chrome for Testing, curl and curl_cffi. Do not change the user agent.

1. Install Google Chrome (the real browser, not `playwright install chromium`).
2. On Linux with no display, run under a virtual display: `xvfb-run -a python main.py ...`.
3. Prove Reuters works on this machine before anything else:
   `python main.py --sections reuters:africa --max-pages 1 -o /tmp/probe`
   Pass = rows saved and exit code 0. A 401/403 means DataDome refused this machine or network. A VPN or rotating proxy can cause it. `NEWS_SCRAPER_PROXY` sets a proxy for every outlet.
4. Only a home IP is proven to work (2026-09-14). If Reuters fails here, report it to Sam. Run the other four outlets without it (`--sources bbc guardian pbs nyt`).

## Known gaps before this can run on a schedule

These are real defects for a hosted job. Fix them before adding a scheduler.

1. **Every run overwrites the last run.** `scrape_source` opens each section file with `"w"`.
2. **Nothing persists between runs.** The seen set lives only while the process runs. With `--max-pages 0` each run walks every section back to where the listing ends (up to 500 pages for PBS).
3. **No health check.** An outlet can stop sending new stories and nothing fails.

## Recommended design (ideas taken from Huginn)

Huginn (github.com/huginn/huginn) was reviewed on 2026-09-15. Do not adopt it; it is a Ruby on Rails app. Take these ideas:

- **Persistent de-duplication** (Huginn DeDuplicationAgent, WebsiteAgent `mode: on_change`): a SQLite store keyed on `(outlet, story id)`. Append new rows only. Stop a section at the first page with no story that was ever seen.
- **Health check** (Huginn `expected_update_period_in_days` and `working?`): per outlet, record the time of the last new story and the last error. Alert when an outlet has no new story in its expected period or has recent errors.
- **Events with optional coordinates** (Huginn Event: payload + optional `lat`/`lng` + `expires_at`): each story row carries a status (`scraped` -> `located` or `not_an_event` -> `uploaded`). Each stage reads rows at the status before it. A crash resumes and no work repeats.
- **Separate stages**: scrape, AI extraction and upload are separate jobs. A Reuters failure must not stop uploads. Extraction must be able to run again without scraping again.
- Schedule with the host's scheduler (systemd timer, cron or Windows Task Scheduler). Do not keep a long-running loop in Python.

## Rules for the AI extraction step

Provenance's GDELT layer taught this: 76.5% of its rows were not a real event of the coded type at the coded place, and almost all of that was bad labelling, not bad geocoding. A court verdict about a shooting was coded as a shooting.

- First decide if the story reports an event that happened at a place. Commentary, obituaries, verdicts about old events and profiles are not events. Store `not_an_event` with a reason.
- For each coordinate, store the quote from the text that supports it and its precision (point, city, region or country).
- Get coordinates from a gazetteer (for example GeoNames), not from numbers the model writes.
- Keep a hand-labelled sample and measure precision before any upload.

## Provenance side

Provenance has no endpoint that accepts events today. Its news is live RSS (`lib/news/`) and its geolocated news layer is GDELT. An ingest route or a data file that Provenance reads must be designed with Sam. Do not invent the contract. Provenance runs on AWS Lightsail at provenance-online.com.

## Conventions

- Stage explicit paths. Never `git add -A`.
- Commits are solo attribution: no AI co-author trailer.
- No en dashes or em dashes in docs or user-facing text.
- Test every parser against a saved real response in `tests/fixtures/`.
