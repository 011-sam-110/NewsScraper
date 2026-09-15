# NewsScraper AI layer and Provenance integration

Status: design agreed with Sam on 2026-09-15. Nothing in this document is built yet.

Audience: the Claude agent that builds and runs NewsScraper on the home host machine.

Checked against NewsScraper `e7df4bc` and Provenance `1755a29` (both 2026-09-15). Line numbers in `file:line` citations drift. If a line has moved, search for the named symbol.

## Contents

1. What we are building
2. Decisions
3. Principles
4. How it fits together
5. Ideas taken from Huginn
6. Map rule and categories
7. Home machine: the `newsfeed` package
8. Snapshot contract `provenance.newsfeed/1`
9. Provenance work
10. Quality gate
11. Failure modes
12. Security and privacy
13. Cost
14. Milestones
15. Open items

## 1. What we are building

NewsScraper scrapes five outlets into JSON Lines, one run at a time. This design adds three things:

1. **A store and a schedule.** The scraper runs hourly into SQLite and never repeats work.
2. **An AI layer on DeepSeek.** It reads every story, checks what the story reports against its text, matches the same event across outlets, and sorts it into a category.
3. **An upload to Provenance** on AWS Lightsail (provenance-online.com). Stories about an event at an identifiable place become map pins. Everything else goes into one new console widget, "World news".

Example: "Man stabbed near Westminster Bridge" from BBC and Reuters becomes one pin in Westminster, London, carrying both headlines. "Central bank holds interest rates" becomes a World news item with no pin.

## 2. Decisions

Sam made these on 2026-09-15. Changing any of them needs Sam.

| Topic | Decision |
|---|---|
| Host | A home machine. It is the only IP proven to work for Reuters. It sits behind NAT, so it pushes. |
| Transport | HTTPS push to a new HMAC-signed ingest route on provenance-online.com. |
| Scope | The AI processes every story. A story whose event happened at an identifiable place becomes a pin. Every other story goes into the World news widget. |
| AI provider | DeepSeek, through an API key. |
| Quality bar | Proven 95%: at most 3 wrong in 153 labelled pins (section 10). |
| Reach | Everywhere a Provenance layer goes: the console map, the landing-page globe, and alerts visitors can arm. |
| Repo | NewsScraper stays public. All five outlets stay. Huginn supplies ideas only; it is not installed. |

One detail changed after research. HTTPS push still stands; only where the box keeps the data changed. The first sketch had the ingest route write the snapshot to a file on the box, and Provenance forbids that:

- A test keeps every way of writing a file out of served code (`tests/unit/discovery-admin-gate.test.ts:148`), and keeps `node:fs` itself to a short allowlist (`:171`).
- The public privacy page promises "nothing this site serves writes a file" (`app/(site)/privacy/page.tsx:227`).

So the box keeps the current snapshot in process memory. The home machine pushes it again whenever the box has lost it, for example after a deploy restart (section 7.10).

## 3. Principles

- **Separate stages.** Scrape, extract, resolve, cluster, publish and health are separate commands over one SQLite database. Each resumes where it stopped. A Reuters failure blocks nothing downstream.
- **The home machine is the source of truth.** The box holds a replaceable copy of the current window and nothing else.
- **Attribute, never assert.** Provenance learned this from GDELT: 76.5% of its rows were not a real event of the coded type at the coded place, and almost all of that was bad labelling, not bad geocoding. So:
  - A pin's title is the lead outlet's headline, verbatim. No model-written text is shown anywhere.
  - Every pin carries a verbatim evidence quote, a precision and a GeoNames id.
  - Every pin says it was located by an AI model and is not a verified incident.
  - "Covered by N outlets" is never called corroboration. Outlets copy each other and the wires.
- **Precision over recall.** Any doubt sends a story to World news instead of the map. A missing pin costs little. A wrong pin costs trust.
- **Coordinates come from a gazetteer.** The model names the place and quotes the text. Code looks the place up in GeoNames. No number the model writes ever becomes a coordinate.
- **Measure before shipping.** Pins publish only after the quality gate passes for the exact configuration that produced them.

## 4. How it fits together

```
HOME MACHINE (behind NAT)

  scraper/* (existing parsers)
      |
      v
  [scrape]    hourly     -> news.sqlite3: stories, aliases, sections, texts
      |
      v
  [extract]   DeepSeek   -> extractions: event or not, category, date, place name, quote
      |
      v
  [resolve]   GeoNames   -> places: lat, lon, precision, GeoNames id
      |
      v
  [cluster]   DeepSeek   -> clusters: one per real-world event, stable ids
      |
      v
  [publish]   every minute: build snapshot, ask the box what it holds, POST if different
      |
      |   HTTPS through Cloudflare, HMAC-signed, one JSON body
      v
PROVENANCE BOX (AWS Lightsail: Cloudflare -> Caddy -> Next.js)

  /api/ingest/newsfeed     verify, validate, replace the snapshot in memory
      |
      v
  lib/newsfeed/store.ts    (one process-wide holder)
      |                               |
      v                               v
  map layer "news-events"         /api/world-news
  (console map, landing globe,    (World news widget)
   alerts)

Beside the pipeline on the home machine:

  [health]    every 15 minutes: outlet silence, backlog, spend, publish failures -> Telegram
  [eval]      labelled samples -> gate.json, which publish reads before it sends pins
```

Data flows one way, from home to box. The box never calls the home machine.

## 5. Ideas taken from Huginn

Huginn (github.com/huginn/huginn) was reviewed on 2026-09-15. It is a Ruby on Rails app and is not adopted. These ideas are:

| Huginn idea | Where it lives in Huginn | What it becomes here |
|---|---|---|
| Agents that each do one job and pass events on | Agents linked by events | One command per stage, linked by status columns in SQLite |
| An event with a payload, optional `lat`/`lng` and `expires_at` | `app/models/event.rb` | A cluster with an optional `location` and a window after which it drops out |
| Remember what was already seen | DeDuplicationAgent (`property`, `lookback`), WebsiteAgent `mode: on_change` | Story aliases in SQLite, and a section stops at the first page with nothing new |
| Is the agent working? | `expected_update_period_in_days` and `working?` | Per-outlet silence checks in the health stage |
| Record what a POST returned | PostAgent `emit_events` | The `publishes` table |
| Authenticated inbound webhook | WebhookAgent `secret` | The HMAC-signed ingest route with a timestamp window |
| Several valid secrets, a bounded window of events | DataOutputAgent `secrets` list and `events_to_show` | The `NEWSFEED_INGEST_SECRETS` comma list for rotation, and the 7-day and 72-hour windows |

## 6. Map rule and categories

### 6.1 The map rule

A cluster becomes a pin only when all of these hold:

1. **A physical happening.** It reports something that someone could have witnessed at that place. A stabbing, a fire, a protest, a flood, a hearing in a named court: yes. A policy announcement, a market move, a poll, an interview: no.
2. **A precise place.** Its event place resolves in GeoNames to point, district or city precision (section 7.7). Region and country are never pinned.
3. **Evidence in the text.** Its evidence quote is found verbatim in the article text, and the place name is inside the quote.
4. **A date in the window.** Its event date falls between the story's published time minus 3 days and plus 1 day, and inside the 7-day pin window.
5. **Article text.** At least one member story has text. NYT has none, so a cluster made only of NYT stories cannot be a pin.

Everything else is a World news item (72-hour window). That includes:

- opinion, analysis, explainers, obituaries, reviews and profiles;
- market reports, and roundups such as PBS News Wrap and NYT briefings;
- live blogs, galleries, podcasts, and videos without a transcript;
- country-level policy stories, even when a capital is named ("Paris announces a new tax" is about France, not Paris);
- verdicts and anniversaries about events outside the date window;
- clusters made only of NYT stories.

Two limits on what outlets tell us:

- **Outlet place tags can veto a place but never supply one.** These are Reuters N2 country codes, BBC topics, Guardian keywords, PBS tags and NYT places. If the model says Lagos and every member is tagged only with France, the story goes to World news.
- **A dateline says where the reporter filed from, not where the event happened.** That includes the Reuters `place` field, byline cities and AP datelines. None of them is ever used as the event place.

### 6.2 Categories, v1

A closed list; the model must pick exactly one. Sam refines the list during M4, and then it is frozen into the config hash (section 10.5).

| id | Covers | Pin possible? |
|---|---|---|
| `conflict` | Armed conflict, military strikes and operations, shelling | yes |
| `attack_or_violent_crime` | Terror attacks, shootings, stabbings, assaults, murders | yes |
| `crime_and_policing` | Arrests, raids, non-violent crime, police operations | yes |
| `protest_and_unrest` | Protests, labour strikes, riots, civil unrest | yes |
| `disaster` | Earthquakes, floods, storms, wildfires, eruptions | yes |
| `accident` | Transport crashes, industrial accidents, collapses, explosions with no attacker | yes |
| `health` | Outbreaks and public-health events at a place | yes |
| `politics_and_diplomacy` | Elections, government decisions, summits, diplomacy | only for a physical happening such as a summit in a city; usually World news |
| `courts_and_justice` | Trials, verdicts, sentencing, inquiries | only for a hearing at a named court inside the date window |
| `economy_and_business` | Markets, companies, trade, jobs | rarely, for example a factory closure at a named site |
| `science_climate_tech` | Research, climate, technology, space | rarely |
| `society_culture_sport` | Culture, religion, sport, human interest | yes, for an event at a venue |
| `opinion_analysis` | Opinion, analysis, explainers, reviews | never |

Category and pin are separate decisions. The category says what the story is about. The map rule says whether it has a place and date someone could have witnessed.

## 7. Home machine: the `newsfeed` package

### 7.1 Layout

Standard library only, like the rest of the repo. Reuters keeps its one dependency, Playwright.

```
newsfeed/
  __init__.py
  __main__.py       python -m newsfeed <stage> [options]
  settings.py       environment variables and .env
  store.py          SQLite connection, schema, migrations, leases
  scrape.py         runs scraper/*, writes rows into the store
  deepseek.py       the one HTTPS client for every model call
  extract.py
  geonames.py       builds and queries geonames.sqlite3
  resolve.py
  cluster.py
  snapshot.py       builds the provenance.newsfeed/1 body
  sign.py           HMAC signing
  publish.py
  health.py
  eval/             label tooling and the gate
eval/labels/        committed label files (section 10.2)
tests/
  test_newsfeed_*.py
  fixtures/newsfeed/  recorded DeepSeek responses, synthetic snapshots, HMAC vectors
```

Commands:

```
python -m newsfeed scrape [--sources reuters bbc ...]
python -m newsfeed extract [--limit N]
python -m newsfeed resolve
python -m newsfeed cluster
python -m newsfeed publish [--dry-run]
python -m newsfeed health
python -m newsfeed geonames-build
python -m newsfeed eval sample|score|gate|audit
```

`main.py` keeps working exactly as it does today.

### 7.2 Settings

Environment variables, or a `.env` file in the repo root. `.gitignore` already ignores `.env`, `.env.*`, `*.sqlite`, `*.sqlite3` and `*.db`. Never commit a key: the repo is public.

Built in M1: `settings.py` also reads the host's own settings file, `~/.config/newsfeed/newsfeed.env` (`$XDG_CONFIG_HOME` is honoured). Later sources win, so the order is that file, then the checkout's `.env`, then real environment variables. The systemd units load the same host file with `EnvironmentFile=`, so a scheduled run and a run by hand cannot drift apart, and the key never has to sit inside a public checkout.

| Variable | Used by | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | extract, cluster, verify | DeepSeek API key |
| `NEWSFEED_INGEST_URL` | publish | `https://provenance-online.com/api/ingest/newsfeed` |
| `NEWSFEED_INGEST_SECRET` | publish | The current signing secret. The same value must be in the box's `NEWSFEED_INGEST_SECRETS`. |
| `NEWSFEED_DATA_DIR` | all | Where the databases and `health.json` live. Default `%LOCALAPPDATA%\NewsScraper` on Windows, `~/.local/share/newsscraper` elsewhere. |
| `NEWSFEED_DAILY_BUDGET_USD` | extract, cluster, verify | Hard stop on model spend per UTC day. Default 3. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | health, publish | Where alerts go |
| `NEWSFEED_DEADMAN_URL` | publish | A dead-man service, pinged after each successful publish |
| `NEWS_SCRAPER_PROXY` | scrape | Existing: one proxy for every outlet |

**Never put the databases in the OneDrive checkout.** This repo lives under OneDrive on Sam's machine, and file sync corrupts SQLite write-ahead logs. `settings.py` refuses to start if `NEWSFEED_DATA_DIR` resolves inside a OneDrive, Dropbox or Google Drive folder.

### 7.3 SQLite rules

- Set `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=30000` and `PRAGMA foreign_keys=ON`.
- Connect with `isolation_level=None`, and open every write transaction with `BEGIN IMMEDIATE`. Then two stages can never deadlock while upgrading a read lock to a write lock.
- **Never hold a transaction across an HTTP request or a model call.** Claim the work and commit, then call out, then write the result in a new transaction.
- **Claimed work carries a lease** (`lease_owner`, `lease_until`). If a stage crashes, its lease expires after 10 minutes and the work can be claimed again.
- **Schema changes are numbered migrations**, recorded in `schema_version`.

### 7.4 Data model

`news.sqlite3`:

| Table | Key | Holds |
|---|---|---|
| `stories` | `story_id` | Outlet, canonical URL, headline, description, published and updated (UTC), authors, word count, thumbnail, outlet categories (JSON), format flags, `text_hash`, `first_seen_at`, `last_seen_at`, `status` |
| `story_aliases` | (`outlet`, `alias`) | Every id a row carries, each pointing at one `story_id` |
| `story_sections` | (`story_id`, `section`) | Every section that listed the story, with first and last seen times |
| `story_texts` | `story_id` | Article text. Kept apart so the main table stays small. |
| `extractions` | (`story_id`, `config_hash`) | The model's JSON, which code checks passed or failed, tokens, cost, model string |
| `places` | (`story_id`, `config_hash`) | GeoNames id, name, lat, lon, precision and parent chain. Or `unresolved`, with a reason. |
| `clusters` | `cluster_id` | Founding story, category, first and last reported times, pin or World news, and the reason if it is not a pin |
| `cluster_members` | (`cluster_id`, `story_id`) | Membership, with the same-event verdict that admitted the story |
| `outlet_health` | `outlet` | Last run, last new story, last error, consecutive failures |
| `llm_calls` | `call_id` | Stage, model, prompt and completion tokens, cache hit and miss tokens, cost, latency, outcome |
| `publishes` | `publish_id` | Snapshot id, item and pin counts, bytes, HTTP status, server `Date` header, duration |
| `alerts` | `alert_key` | When each alert was last sent, so it is not repeated inside its quiet period |
| `schema_version` | `version` | Applied migrations |

**Story identity.** A row joins the story that any of its aliases already points to. Otherwise it starts a new story. The aliases for each outlet, checked against the current scrapers:

| Outlet | Aliases |
|---|---|
| Reuters | Row `id`, which is the article `_id` (`scraper/reuters.py:461`); canonical URL |
| BBC | Row `id`, the content URN, for example `urn:bbc:optimo:asset:cx2kz4v4e6xo` (`scraper/bbc.py:61`); canonical URL |
| Guardian | `short_url` when present (`scraper/guardian.py:269`, `:472`, `:500`); row `id`; canonical URL |
| PBS | Row `id`, the URL path (`scraper/pbs.py:203`); `post_id` from the article page when it was fetched (`scraper/pbs.py:364`) |
| NYT | `guid` (`scraper/feeds.py:87`); canonical URL |

Canonical URL: scheme and host lower-cased, query and fragment removed, trailing slash removed.

**Status.** A story's `status` only moves forward: `scraped` -> `extracted` -> `resolved` or `no_place` -> `clustered`. There is one exception. A material change, meaning a new headline or text hash, sends the story back to `scraped`. The last accepted extraction stays in use until the new one passes.

**GeoNames database.** `geonames.sqlite3` is built by `python -m newsfeed geonames-build` (section 7.7). Every other stage only reads it.

### 7.5 Stage: scrape (hourly)

- **An optional store sink.** `newsfeed/scrape.py` runs the existing `scraper.common.scrape` loop and passes a store sink into `scrape_source` (`scraper/common.py:226`). With no sink the loop behaves exactly as it does today, so `main.py` and the 136 existing tests do not change.
- **What the sink does.** It answers two questions: is this row already known for this section, and does it need its article page fetched? A page is fetched for a new story, or when the listing's `updated` time changed. Then the sink saves the row.
- **Stopping early.** A section stops at the first listing page where every row is already known for that section. Today each run walks back to where the listing ends, which is up to 500 pages for PBS. This fixes known gaps 1 and 2 in `CLAUDE.md`. Scheduled runs write only to the store, not to JSON Lines.
- **UTC times.** Times are stored as UTC ISO 8601. `rss_date` passes values it cannot parse through unchanged (`scraper/common.py:105`). A value that still does not parse is stored as null, and the raw value is kept.
- **Format flags.** These come from outlet metadata, never from the model:
  - `/live/` in the URL
  - Reuters N2 codes ANLINS, MKTREP, EXPLN and FBOX
  - Guardian tone and design fields
  - NYT URL paths such as `/briefing/` and `/opinion/`
  - PBS broadcast pages

  A flagged story still gets a category, but it cannot be a pin.
- **Health.** Each outlet run updates `outlet_health`.

### 7.6 Stage: extract

One DeepSeek call per story, newest stories first. The request is raw HTTPS through `urllib`, with no SDK:

```
POST https://api.deepseek.com/chat/completions
Authorization: Bearer <DEEPSEEK_API_KEY>

{
  "model": "deepseek-flash",
  "thinking": {"type": "disabled"},
  "response_format": {"type": "json_object"},
  "temperature": 0,
  "max_tokens": 800,
  "messages": [
    {"role": "system", "content": "<fixed instructions, the category list, one worked example; mentions json>"},
    {"role": "user", "content": "<outlet, section, headline, published time, outlet tags, article text>"}
  ]
}
```

- **Caching.** The system message is identical on every call, and nothing that varies comes before the article. DeepSeek caches repeated prompt prefixes automatically, and cached input tokens cost much less. Each call logs `usage.prompt_cache_hit_tokens` and `usage.prompt_cache_miss_tokens`.
- **JSON mode.** It needs the word "json" in the prompt, and it can return empty content. Empty content is retried (table below).
- **Model id.** When M5 starts, confirm the id with `GET https://api.deepseek.com/models`, and record the `model` string each response returns. Both go into the config hash.

Output schema, with an example:

```json
{
  "is_physical_event": true,
  "not_event_reason": null,
  "category": "attack_or_violent_crime",
  "event_date": "2026-09-14",
  "event_place": {
    "name": "Westminster",
    "within": "London",
    "country": "GB",
    "kind": "district",
    "quote": "a man was stabbed near Westminster Bridge in central London on Sunday"
  },
  "other_places": ["Manchester"],
  "key_entities": ["Metropolitan Police"],
  "cluster_hint": "stabbing near Westminster Bridge, one man injured"
}
```

Allowed values:
- `not_event_reason`: `opinion`, `analysis`, `roundup`, `policy`, `past_event`, `no_place` or `other`.
- `kind`: `venue`, `street`, `district`, `city`, `region` or `country`.

Code checks before an extraction is accepted:

1. The JSON matches the schema, and the category is on the list.
2. After whitespace is normalised, `quote` is a verbatim substring of the article text, and `name` appears inside `quote`.
3. `event_date` passes the date rule in section 6.1, measured from the story's published time.
4. `country` agrees with the outlet's place tags, when the outlet has any (the veto in section 6.1).
5. A story with a format flag, or with no text, is set to `is_physical_event: false`, whatever the model said.

**Failed checks.** A failed schema check gets exactly one retry, with the error appended to the prompt. Any other failure is stored with its reason, and the story goes to World news.

Every model call goes through `newsfeed/deepseek.py`, which follows these rules:

| Response | Action |
|---|---|
| 200 with valid content | Accept |
| 200 with empty content | Back off and retry, up to 3 times |
| `finish_reason: length` | Retry once with `max_tokens` doubled |
| 429, 500, 503, timeout, connection error | Exponential backoff with jitter, up to 5 tries, then leave the story for the next run |
| 402, insufficient balance | Stop every model stage, send an alert, exit non-zero |
| 400, 401 or 422 | Stop and alert: the request or the key is wrong |

None of these retries uses up the one schema retry.

**Spend and backlog.** Every call is written to `llm_calls` with its cost. When the day's cost reaches `NEWSFEED_DAILY_BUDGET_USD`, model stages stop until the next UTC day and an alert goes out. After an outage the backlog is worked newest first, so the live window refills before old stories are processed.

**No repeated work.** Outputs are stored by (`story_id`, `config_hash`), together with the text hash. Running again with the same configuration costs nothing.

### 7.7 Stage: resolve

**Building the gazetteer** (`python -m newsfeed geonames-build`, monthly):

- Download from download.geonames.org: `allCountries.zip`, `alternateNamesV2.zip`, `admin1CodesASCII.txt`, `admin2Codes.txt` and `countryInfo.txt`. From the alternate names, keep only English and preferred names.
- Write `geonames.sqlite3` with a name index. Record each source file's SHA-256 and download date in a `build` table. Those hashes are part of the config hash.
- GeoNames data is CC BY 4.0. The Provenance layer's attribution credits GeoNames (section 9.2).

**Matching an `event_place`:**

1. Restrict candidates to the extraction's country.
2. Find candidates whose name, or an English alternate name, equals `name` after folding case and accents.
3. If `within` is given, resolve it first. Then keep only candidates close to it: within 30 km of a city, 150 km of a region, or inside the country. Distance is used because GeoNames admin codes are too uneven across countries to rely on alone.
4. Rank how well each candidate's feature code fits `kind`. `venue` fits S class. `district` fits PPLX, ADM3 and ADM4. `city` fits PPL, PPLA to PPLA4, and PPLC. Break ties by population.
5. **Ambiguity.** If the top two candidates rank about equally and are more than 25 km apart, the name is ambiguous. Climb to `within` and resolve that instead, at its own precision. If `within` is ambiguous too, the place is unresolved and the story is World news.

**Precision** comes from the matched feature:

| GeoNames feature | Precision | Pinned? |
|---|---|---|
| S class (buildings, stations, stadiums, bridges) | `point` | yes |
| PPLX (a section of a town), ADM3, ADM4 | `district` | yes |
| PPL, PPLA to PPLA4, PPLC | `city` | yes |
| ADM1, ADM2 | `region` | no |
| PCLI and other country codes | `country` | no |

**Golden tests**, which must pass before M6 ends:

- "Westminster" within "London", country GB, resolves to the London district, not to Westminster in Colorado.
- "Paris" with country FR resolves to the French capital. With country US and within "Texas", it resolves to Paris, Texas.
- "Georgia" with country GE is the country, and with country US it is the state. Neither is pinned.
- A street that has no GeoNames entry climbs to its city.

### 7.8 Stage: cluster

Every extracted story joins exactly one cluster.

1. **Find candidates.** A candidate is an open cluster that meets both conditions:
   - its founding story was published within 48 hours of this story;
   - it shares this story's country and at least one of: a resolved place within 50 km, a key entity, or a similar headline.
2. **Same-event check.** For each candidate, one `deepseek-flash` call compares this story with the cluster's **founding story only**. The model gets both stories' headline, date, place, casualty or damage figures, and named actors. It answers `same`, `different` or `unsure`, and only `same` joins. Comparing with the founder, never the latest member, stops chaining, where A matches B, B matches C, and A and C are different events.
3. **No match.** The story founds a new cluster.
4. **Ids.** A cluster id is `nf_` plus the first 12 hex characters of SHA-256 over the founding story's outlet and first alias. It is minted once and stored, and it never changes. v1 never merges two clusters, so an id on the box always means the same event.
5. **Pin decision.** A cluster is a pin when both hold:
   - its founding story passes the map rule;
   - every member with a resolved place is within 50 km of the pin.

   If members are further apart, the cluster goes to World news with the reason `members_far_apart`.
6. **Live blogs.** A live blog never joins a cluster, and no story joins one.

The test that matters: two different stabbings in London on the same day must stay two clusters (M7).

### 7.9 Stage: verify (only if the gate needs it)

This stage is added only if M8 fails on pins. A second call sees the founding story and the proposed pin, and it can only veto the pin, never create one. The call uses either `deepseek-flash` with thinking enabled or `deepseek-v4-pro`. Its settings join the config hash.

### 7.10 Stage: publish (every minute)

1. **Build.** Build the snapshot (section 8) from the clusters in the window.
   - If `gate.json` does not pass for the current pin config hash, include no located items and set `pinsWithheld: true`.
   - World news works the same way, with its own gate and `worldNewsWithheld`.
2. **Ask.** Send a signed `GET /api/ingest/newsfeed`. The answer is the `snapshotId` the box holds, or null.
3. **Send if needed.** If the box already holds the latest local snapshot, stop. Otherwise send a signed `POST` with the snapshot.
4. **Retry or stop.**
   - Retry inside the minute, with backoff: 502, 503, 521 and 522, timeouts, and any answer that is not JSON (a Cloudflare challenge page looks like this).
   - A 401 or 422 is a bug: alert and stop.
5. **Record.** Write a `publishes` row. After a 200, ping `NEWSFEED_DEADMAN_URL`.
6. **Check the clock.** Compare local time with the response's `Date` header. If they are more than 60 seconds apart, alert, because signatures are only valid for 300 seconds.

A new snapshot, with a new `snapshotId` and `generatedAt`, is built only when the content changed or 10 minutes have passed. That keeps `generatedAt` meaningful and guarantees the box hears from home at least every 10 minutes. After a box restart, the next run finds the box empty and sends the same snapshot again.

### 7.11 Stage: health (every 15 minutes)

Each check has its own quiet period, so an alert is not repeated.

| Check | Alert when |
|---|---|
| Outlet silence | No new story from an outlet within its expected period: 3 hours for Reuters, BBC and Guardian; 12 hours for PBS and NYT. Tune these after M1. |
| Outlet errors | 3 failed runs in a row, or a 401/403 from Reuters (DataDome refused the machine) |
| Backlog | The oldest story still waiting for extraction is more than 2 hours old |
| Spend | A balance error, or 80% of the daily budget used |
| Publish | No 200 from the box for 10 minutes |
| Gate | `gate.json` is more than 8 days old, or a weekly audit failed |

Alerts go to Telegram. `health.json` in the data directory holds the latest result of every check.

### 7.12 Scheduling

Use the host's scheduler, never a long-running Python loop.

**Windows Task Scheduler**
- **One task per stage:**
  - scrape: hourly
  - extract, resolve and cluster: every 10 minutes
  - publish: every minute
  - health: every 15 minutes
  - geonames-build: monthly
- **Every task:** "If the task is already running: Do not start a new instance".
- **The scrape task that includes Reuters:** set "Run only when user is logged on", because Reuters opens a visible Chrome window.
- **Power settings:** the machine must not sleep. A sleeping machine looks like an outage of every outlet and of publishing.

**Linux**
- systemd timers with `Persistent=true`.
- Run the Reuters scrape under `xvfb-run -a`.

**When each task starts**
- Scrape is scheduled from M1, so data builds up.
- Extract, resolve and cluster run on the schedule from M7, still publishing nothing.
- Publish and health go live at M10.

## 8. Snapshot contract `provenance.newsfeed/1`

Agreed with Sam on 2026-09-15. Any change needs Sam and a new schema version.

### 8.1 Body

One JSON object, UTF-8, sent with `Content-Type: application/json`.

| Field | Type | Rule |
|---|---|---|
| `schema` | string | Exactly `provenance.newsfeed/1` |
| `snapshotId` | string | `snap_` plus 16 hex characters. Changes whenever the content changes. |
| `generatedAt` | string | UTC ISO 8601 time the snapshot was built |
| `dataAsOf` | string | UTC time of the newest completed scrape run behind it |
| `pinsWithheld` | boolean | True when the gate does not currently allow pins |
| `worldNewsWithheld` | boolean | True when the gate does not currently allow World news items |
| `outlets` | array | One `{outlet, lastNewStoryAt}` per outlet. `lastNewStoryAt` is UTC, or null. |
| `items` | array | 0 to 3,000 items |

Each item:

| Field | Type | Rule |
|---|---|---|
| `id` | string | The cluster id: `nf_` plus 12 hex characters, stable for the life of the event |
| `category` | string | One of the v1 categories |
| `title` | string | The lead report's headline, verbatim, at most 300 characters |
| `firstReportedAt`, `lastReportedAt` | string | UTC ISO 8601 |
| `eventDate` | string or null | `YYYY-MM-DD` |
| `location` | object or null | Null for World news items |
| `reports` | array | 1 to 8 reports, lead report first |

`location`:

| Field | Type | Rule |
|---|---|---|
| `lat`, `lon` | number | From GeoNames, at most 5 decimal places |
| `precision` | string | `point`, `district` or `city` |
| `place` | string | Display name, for example `Westminster, London, United Kingdom`. At most 120 characters. |
| `geonamesId` | integer | The matched GeoNames feature |
| `evidence` | string | The verbatim quote, trimmed around the place name to at most 200 characters |
| `evidenceOutlet` | string | The outlet the quote came from |

Each report has these fields:

| Field | Rule |
|---|---|
| `outlet` | One of `reuters`, `bbc`, `guardian`, `pbs`, `nyt` |
| `headline` | Verbatim, at most 300 characters |
| `url` | `http` or `https` only, at most 600 characters |
| `publishedAt` | UTC |

**Never in the body:** article text, descriptions, author names, the model's reasoning, or any text a model wrote. The only quoted text is `evidence`, at most 200 characters.

**Windows.** A pin stays for 7 days from `lastReportedAt`, and a World news item for 72 hours. The home machine applies the windows. The box shows what it was sent and drops nothing on its own.

### 8.2 Example

Synthetic: the ids, coordinates, headlines and URLs are illustrative.

```json
{
  "schema": "provenance.newsfeed/1",
  "snapshotId": "snap_3f9c2a71d04b8e65",
  "generatedAt": "2026-09-15T09:31:02Z",
  "dataAsOf": "2026-09-15T09:05:40Z",
  "pinsWithheld": false,
  "worldNewsWithheld": false,
  "outlets": [
    {"outlet": "reuters", "lastNewStoryAt": "2026-09-15T08:58:11Z"},
    {"outlet": "bbc", "lastNewStoryAt": "2026-09-15T09:02:37Z"},
    {"outlet": "guardian", "lastNewStoryAt": "2026-09-15T08:47:03Z"},
    {"outlet": "pbs", "lastNewStoryAt": "2026-09-15T04:12:50Z"},
    {"outlet": "nyt", "lastNewStoryAt": "2026-09-15T09:00:00Z"}
  ],
  "items": [
    {
      "id": "nf_8a41c09e2d7b",
      "category": "attack_or_violent_crime",
      "title": "Example: man injured in stabbing near Westminster Bridge",
      "firstReportedAt": "2026-09-14T18:20:00Z",
      "lastReportedAt": "2026-09-14T21:05:00Z",
      "eventDate": "2026-09-14",
      "location": {
        "lat": 51.50086,
        "lon": -0.12190,
        "precision": "district",
        "place": "Westminster, London, United Kingdom",
        "geonamesId": 1000001,
        "evidence": "stabbed near Westminster Bridge in central London",
        "evidenceOutlet": "bbc"
      },
      "reports": [
        {
          "outlet": "bbc",
          "headline": "Example: man injured in stabbing near Westminster Bridge",
          "url": "https://www.bbc.co.uk/news/articles/example1",
          "publishedAt": "2026-09-14T18:20:00Z"
        },
        {
          "outlet": "reuters",
          "headline": "Example: one hurt in London knife attack",
          "url": "https://www.reuters.com/world/uk/example-2026-09-14/",
          "publishedAt": "2026-09-14T21:05:00Z"
        }
      ]
    },
    {
      "id": "nf_c52e7f10a9d3",
      "category": "economy_and_business",
      "title": "Example: central bank holds interest rates",
      "firstReportedAt": "2026-09-15T06:00:00Z",
      "lastReportedAt": "2026-09-15T07:10:00Z",
      "eventDate": "2026-09-15",
      "location": null,
      "reports": [
        {
          "outlet": "guardian",
          "headline": "Example: central bank holds interest rates",
          "url": "https://www.theguardian.com/business/example",
          "publishedAt": "2026-09-15T06:00:00Z"
        }
      ]
    }
  ]
}
```

Test fixtures in both repos must be synthetic like this one, because both repos are public.

### 8.3 Signing

Every request to `/api/ingest/newsfeed` carries two headers:

```
X-Newsfeed-Timestamp: 1789464662
X-Newsfeed-Signature: v1=<64 lowercase hex characters>
```

**The signature.** The signature is HMAC-SHA256, keyed with the secret, over this exact string. The lines are joined with `\n`, with no trailing newline:

```
v1
<METHOD>
/api/ingest/newsfeed
<timestamp>
<lowercase hex SHA-256 of the raw body bytes>
```

A GET signs the SHA-256 of an empty body: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Python signs with `hmac` and `hashlib`. The box verifies with `crypto.subtle.verify`, which compares in constant time.

**When the box accepts a request.** It accepts only when all three hold:

1. `NEWSFEED_INGEST_SECRETS` is set. If it is blank, the route is dormant: every request gets 404, so the route reveals nothing.
2. The timestamp is within 300 seconds of the box's clock.
3. The signature verifies against at least one secret in that comma list. Each secret is at least 32 characters.

**Rotating a secret.**
1. Add the new secret to the box's list.
2. Switch the home machine to the new secret.
3. Remove the old secret from the list.

**Test vectors.** M2 commits the same three vectors to both repos, each with the secret, timestamp, exact body bytes and expected signature:
- a GET;
- a small POST;
- a POST with non-ASCII characters in a headline.

`unittest` checks them in NewsScraper, and `vitest` checks them in Provenance.

### 8.4 Answers

| Request | Answer |
|---|---|
| Valid POST with a newer `generatedAt` than the stored snapshot | 200 `{"ok": true, "snapshotId": "..."}`, and the snapshot replaces the stored one |
| POST with the stored `snapshotId` again | 200. Nothing changes, so retries are safe. |
| POST with a different snapshot whose `generatedAt` is not newer | 409. Old data cannot replace new data. |
| Body over 4 MB | 413, counted while reading and before parsing |
| Missing or wrong signature, or timestamp outside 300 seconds | 401 |
| Not JSON, or fails the schema | 422, with a short reason |
| Signed GET | 200 `{"snapshotId": "..." or null, "receivedAt": "..." or null}`, sent with `Cache-Control: no-store` |
| Any request while dormant | 404 |

After a restart the store is empty and a GET answers null. The next publish run, within a minute, sends the current snapshot again.

## 9. Provenance work

Two pull requests in `011-sam-110/Provenance`, opened by the host Claude. Follow Provenance's own `CLAUDE.md` and PR process. Paths below are from the Provenance repo root.

### 9.1 PR A: the ingest route, merged dormant (M3)

New files:

| File | Job |
|---|---|
| `lib/newsfeed/contract.ts` | Types, caps and `validateSnapshot()`, covering every field and length in section 8.1. Pure and node-testable. |
| `lib/newsfeed/verify.ts` | The canonical string, the timestamp window, and `crypto.subtle.verify` over each secret |
| `lib/newsfeed/store.ts` | The process-wide holder: `{snapshot, receivedAt}` under `globalThis[Symbol.for("provenance.newsfeed.store")]`, with read, accept and test-only reset functions. It imports nothing from Node. |
| `app/api/ingest/newsfeed/route.ts` | GET and POST, as in section 8.4 |
| `app/api/world-news/route.ts` | GET: World news items from the store, newest first. `no-store` when the store is empty or the items are withheld. |
| `tests/unit/newsfeed-*.test.ts` | The contract, the signature vectors, replay, the size cap, dormant 404, and the store surviving duplicated module copies |

Why these shapes:

- **Memory, not a file.** Served code may not write files (`tests/unit/discovery-admin-gate.test.ts:148`), and the privacy page says so (`app/(site)/privacy/page.tsx:227`). The snapshot is news, not visitor data, and it lives in process memory. The page's "no database, no key-value store" stays true.
- **`Symbol.for` on `globalThis`.** The Next server build can copy a module into several chunks. `lib/signals/coverage.ts:82-88` records how a module-level WeakMap lost every record in production for exactly this reason, and `lib/signals/outcome.ts:28-30` repeats the rule.
- **No Node imports in the store.** `components/WorldMap.tsx` is a client component that imports the signal registry (`components/WorldMap.tsx:114`). The map layer in PR B imports the store, so the store must not pull Node into the browser bundle.

**POST steps inside the route.** Each step fails fast:

1. **Dormant:** if no secret is set, answer 404.
2. **Size:**
   - If `content-length` is above 4 MB, answer 413.
   - Otherwise read the stream, counting bytes, and stop at 4 MB, because a chunked body has no length.

   `app/api/feedback/route.ts:104-113` is the house precedent for the header check. This route adds the streaming count.
3. **Auth:** check the timestamp window, then the signature (401). Nothing is parsed before the signature verifies.
4. **Parse:** `JSON.parse`, then `validateSnapshot` (422).
5. **Store:** apply the replay rule (409, or the idempotent 200), then store.

**What the route does not do.**
- It reads no cookies and no identifying header: no `x-forwarded-for`, no `cf-connecting-ip`. So `tests/unit/privacy-page.test.ts:92` needs no privacy-page change.
- It exports only what `tests/unit/route-export-contract.test.ts` allows, which is HTTP methods and Next's segment config. Helpers live in `lib/newsfeed/`.

Changes to existing files, each with the test that fails until it is done:

| Change | Enforced by |
|---|---|
| Add `"api/ingest/newsfeed"` to `GATE_EXEMPT_STARTS` (`lib/gate/paths.ts:47`), so the maintenance curtain does not block the home machine | `tests/unit/gate.test.ts:91`: the matcher must match the list |
| Update the `matcher` string literal in `middleware.ts:42` to the value `gateMatcher()` derives | Same test |
| Add a collision test like `tests/unit/gate.test.ts:48`: no other directory under `app/api/ingest` starts with `newsfeed`, because exemptions are prefixes | New test |
| Rename the test at `tests/unit/gate.test.ts:38`, whose name says only the unlock endpoint is exempt | Wording |
| Add a `KEY_REQUIREMENTS` entry (`lib/sources/keyRequirements.ts:48`) with `kind: "required"`, `env: ["NEWSFEED_INGEST_SECRETS"]` and a non-layer capability id such as `world-news`. Add that id to `NON_SIGNAL_IDS`, because the layer does not exist until PR B. | `tests/unit/sources-status.test.ts:70`: every credential the code reads must be listed; `:43`: ids must be layers or declared capabilities |
| Document the variable in `deploy/env.example` (section 3, live data) and in `docs/API_KEYS.md` | Review |

Merging PR A changes nothing anyone can see. With no secret set, the route answers 404.

**Switching it on (Sam, on the box):**

1. Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Add `NEWSFEED_INGEST_SECRETS=<secret>` to `/srv/provenance/shared/.env`, the file `deploy/provenance.service:24` loads.
3. Restart the service.
4. Put the same secret in the home machine's `NEWSFEED_INGEST_SECRET`.

### 9.2 PR B: the map layer and the World news widget (M9)

Open this only after the gate passes (M8).

#### Map layer: `lib/signals/news-events.ts`

Register it in `lib/signals/registry.ts`, next to the GDELT import at `:48`.

**Source fields:**

| Field | Value |
|---|---|
| `id` | `"news-events"` |
| `kind` | `"event"` |
| `group` | `"Intel"`, the group the GDELT layers use (`lib/signals/gdelt.ts:878`) |
| `refreshMs` | `60 * 1000` |
| `label` | `"News events"` |
| `attribution` | "Reuters, BBC, The Guardian, PBS NewsHour and The New York Times reports, grouped and located by NewsScraper with DeepSeek and GeoNames (CC BY 4.0)" |

It imports only `lib/newsfeed/store.ts` and `lib/signals/outcome.ts`.

**Features.** There is one feature per located item:

| Feature field | Value |
|---|---|
| `id` | The item id |
| `lat`, `lon` | From `location` |
| `title` | The item's `title` |
| `ts` | `lastReportedAt` |
| `link` | The lead report's URL |

**Props** are flat strings only:

| Prop | Example |
|---|---|
| `category` | "Attack or violent crime" |
| `place` | "Westminster, London, United Kingdom" |
| `precision` | "District" |
| `evidence` | "stabbed near Westminster Bridge in central London" (BBC) |
| `outlets` | "BBC, Reuters" |
| `coverage` | "Reported by 2 of the 5 outlets watched. Not corroboration: outlets often share wire copy." |
| `coding` | "Grouped and located by an AI model from news reports. Not a verified incident." |
| `timing` | "Event date 14 Sep 2026. First reported 18:20 UTC." |

There are no `magnitude`, `severity`, `level` or `status` props. The map and alert rules treat props with those names as measurements.

**Outcomes** (`lib/signals/outcome.ts`):

| State | Return |
|---|---|
| No secret set | `degraded("no key")`, the house convention (for example `lib/signals/ais.ts:235`), which the landing audit shows as locked (`scripts/gen-landing-audit.mjs:68`) |
| Secret set, nothing received since the process started | `degraded("awaiting first snapshot")` |
| `pinsWithheld` is true | `degraded("withheld: quality check")` |
| `dataAsOf` older than 3 hours, or no snapshot received for 15 minutes | `degradedWith(features, "stale feed", at)` |
| Otherwise | `observed(features, at)` |

`at` is the earlier of `generatedAt` and the time the box received the snapshot. That way a freshness claim never runs ahead of the data.

#### Other changes, and the test that enforces each

| Change | Enforced by |
|---|---|
| Add a `LayerExplainer` (`lib/signals/explain.ts:42`, list at `:56`) with what a pin is, the method, and confidence `modelled`, like GDELT's `conflict` entry (`:346`). Coverage: "five English-language outlets". Limitations must include outlet bias, AI error at the measured rate, and that outlet counts are not corroboration. | `tests/unit/signals-explain.test.ts` |
| Add a provider URL in `SIGNAL_PROVIDER_URLS` (`lib/signals/sourceLink.ts:69`), pointing at this document | `tests/unit/signals-sourceLink.test.ts:80` |
| Add alert wording in `SOURCE_WORDING` (`lib/notify/wording.ts:30`), like the GDELT entries at `:34-41`: suffix "Located by an AI model from news reports, not a verified incident." and `verb: { appears: "was reported in", count: "reports in" }`. An alert carries the feature `title` (`lib/notify/sources.ts:47`) without any of the hedges shown on screen, so the suffix is what keeps it honest. | `tests/unit/notify-wording.test.ts`: add a case |
| Add a second `KEY_REQUIREMENTS` entry, with id `news-events` | `tests/unit/sources-status.test.ts` |
| Update the README layer figures (`README.md:51`, `:65`, `:66`) | `tests/unit/readme-counts.test.ts:194`, `:200` |
| Update the widget figures (`README.md:70`, `:136`, `lib/gate/page.ts:59`) | `tests/unit/gate.test.ts:314` for the gate page; review for the README |
| Regenerate `lib/marketing/coverage-audit.data.ts`: run `scripts/country-event-breakdown.mts`, then `scripts/gen-landing-audit.mjs`, as `tests/unit/landing-audit.test.ts:29-32` spells out | `tests/unit/landing-audit.test.ts:24` |

Once registered, the layer appears on the console map, in the rail and widget catalogue, and on the landing-page globe with no further edits (`README.md:143`). Visitors can arm alerts on it, as on every registered signal.

#### Widget: `lib/console/widgets/world-news.tsx`

Model it on `lib/console/widgets/headlines.tsx`.

- **Data.** It polls `/api/world-news`, the way `lib/console/widgets/headlines.tsx:34` polls `/api/news`. Each row shows the title, category, outlets and relative time, and links to the lead report.
- **Honest states.**
  - `worldNewsWithheld` is set: "withheld while the quality check runs".
  - `dataAsOf` is more than 3 hours old: "feed stale".
  - It never says "no news" when the truth is withheld or stale.
- **Registration.**
  - Import it in `lib/console/widgets/index.ts`, as at `:8`.
  - Add it to a palette group in `lib/console/paletteGroups.ts`, next to World Headlines at `:104`.
  - Add a trust card in `WIDGET_EXPLAINERS` (`lib/console/help.ts:62`), modelled on the World Headlines card at `:180`. Use confidence `modelled`, because a model does the grouping and categories, and state the outlet-count caveat.
  - Enforced by `tests/unit/widget-explainers.test.ts` and `tests/unit/palette-groups.test.ts`.
- **No model-written text.** It uses neither `lib/news/synthesis.ts` nor any other model-written summary.
- **World Headlines is untouched.** It keeps reading `/api/news` and clustering in the browser with `lib/news/cluster.ts`.

## 10. Quality gate

### 10.1 What "proven 95%" means

The gate is a statistical test, not an average. Take a labelled sample of `n` pins. The one-sided 95% Clopper-Pearson upper bound on the error rate must be at most 5%. That allows:

| Labelled pins | Wrong pins allowed |
|---|---|
| 59 | 0 |
| 93 | 1 |
| 124 | 2 |
| 153 | 3 |

This design uses 153 pins with at most 3 wrong. To pass reliably, the pipeline has to be right about 98 to 99% of the time:

| True error rate | Chance of passing at 3 or fewer wrong in 153 |
|---|---|
| 1% | 93% |
| 1.5% | 80% |
| 2% | 63% |
| 3% | 32% |

Every report also shows the Wilson 95% interval beside the raw error rate.

### 10.2 Labels

- **What a gold label records.** It describes the article, not the pipeline's output:
  - is it a physical event, yes or no;
  - 1 or 2 acceptable categories;
  - the event place, as a GeoNames id plus precision;
  - the event date.
- **Blind labelling.** The host Claude labels blind: it sees the article, never the extraction.
- **Agreement.** Sam labels 30 of the same articles independently. Agreement is reported per field, and Sam settles every disagreement.
- **No article text in label files.** The repo is public, so label files hold story aliases, text hashes and labels only, never article text or headlines. They live in `eval/labels/*.json`. Do not use JSON Lines: `.gitignore` ignores `*.jsonl`, so those files would silently never be committed.

### 10.3 Samples

- **Dev set:** 150 articles across all outlets. Use them freely for prompt and threshold work.
- **Prompt freeze:** at the end of M7, fix the config hash and record the date.
- **Held-out set:** 153 pins, one pin per cluster, drawn in proportion to the output.
  - Draw only from stories first seen at least 7 days after the freeze.
  - The pipeline runs on the schedule during those 7 days.
- **Strata:** evidence outlet, whether NYT is involved, and district or city precision.
  - Each stratum is checked on at least 20 pins.
  - Small strata are topped up with extra draws. These count for that stratum only, not toward the 153.
  - A stratum with 2 or more wrong is withheld from publishing, even if the overall gate passes.

### 10.4 What counts as correct

A pin is correct only when all five of these hold. "Unclear" counts as wrong.

1. The event happened. The article reports it as having happened, not as planned, threatened or hypothetical.
2. Its date is inside the window.
3. The place is right at the stated precision.
4. Every member story reports the same event.
5. The category is one of the acceptable ones.

The same bar of at most 3 wrong in 153 applies separately to:

- **World news items:** each counts as correct when the category is acceptable and the grouping is right.
- **Merged pairs:** 153 random pairs of stories that the pipeline put in the same cluster. A pair is wrong when the two stories are not about the same event.

Recall is measured and reported, but not gated. The check is how many real, placed events among 100 World news stories were missed.

### 10.5 The config hash

The config hash is SHA-256 over all of these:
- both prompts;
- the model string the API returns;
- `temperature`, `max_tokens` and the thinking setting;
- the category list;
- the resolver's code version and thresholds;
- the GeoNames build hashes;
- the date rules.

`gate.json` records the hash that passed. Publish sends pins only when the current hash matches a passing one. Changing anything in the list means a new gate run.

When several configurations are scored on the same held-out set, the cheapest passing one wins. It must then pass again on a fresh sample, because picking the best of several flatters it.

### 10.6 Weekly audit

Every week the host Claude labels 20 live pins. If 2 or more are wrong, publish withholds pins (`pinsWithheld: true`) until a full gate run passes again.

## 11. Failure modes

| What breaks | What happens | How it is noticed |
|---|---|---|
| Reuters refuses the machine (401/403) | Reuters stops. The other four outlets carry on. | health: outlet errors and silence |
| An outlet changes its page layout | That outlet's parser returns nothing or fails | health: outlet silence. Fix against a new saved fixture. |
| The home machine sleeps or is off | Nothing scrapes or publishes. The box keeps the last snapshot, then marks it stale. | The layer degrades after 15 minutes. The dead-man service alerts. |
| DeepSeek is slow, or answers 429 or 5xx | The backlog grows and is retried later, newest first | health: backlog age |
| DeepSeek balance runs out (402) | Model stages stop | health: spend alert |
| Daily budget reached | Model stages pause until the next UTC day | health: spend alert |
| The model returns bad JSON or a quote not in the text | The story goes to World news, with the reason stored | eval reports |
| A prompt, model or threshold changes | The config hash changes, and pins are withheld until the gate passes | `pinsWithheld` on the box; health: gate |
| A deploy restarts the box | The store empties. The next publish refills it within a minute, and the layer is back within about 3 minutes. | The layer shows "awaiting first snapshot" meanwhile |
| Cloudflare challenges the publisher | Answers that are not JSON, which publish retries | health: publish failures. The fix is a Cloudflare skip rule for the exact ingest path. |
| The home clock drifts | Signatures fail with 401 | publish: clock-skew alert at 60 seconds |
| The secret leaks | Anyone can push a snapshot | Rotate it (section 8.3) |
| A weekly audit fails | Pins are withheld | health: gate |
| SQLite is locked | Writers wait up to 30 seconds, then the stage exits and the next run retries | Stage logs |
| The data directory is inside OneDrive | Sync would corrupt the database | Prevented: `settings.py` refuses the path |

## 12. Security and privacy

- **The signature comes first.** The ingest route trusts nothing before the signature verifies: it neither parses nor logs the body.
- **The size cap holds while reading.** The 4 MB cap is enforced as the body arrives, so a huge or endless body cannot exhaust memory.
- **Replays are limited.**
  - A request can only be replayed within 300 seconds.
  - A replay cannot roll the box back to older data (409).
- **Secrets are handled strictly.**
  - At least 32 characters each.
  - Never committed to either repo.
  - Rotated by overlap (section 8.3).
- **No visitor data.** This feature stores no visitor data on the box, reads no identifying header on the ingest route, and writes no file.
- **Public repos, synthetic data.** Both repos are public, so fixtures, test vectors and example snapshots are synthetic. Label files carry no article text.
- **The DeepSeek key stays home.** It exists only on the home machine.
- **Link schemes are limited.** Report URLs must be `http` or `https`, so a `javascript:` link can never reach the map or the widget.

## 13. Cost

This is an estimate until M10 measures it.

- **Volume:** a few hundred new stories a day across the five outlets.
- **Extraction:** one `deepseek-flash` call per story, with thinking off and a cached system prompt. Roughly $0.0008 a story at peak rates.
- **Clustering:** same-event checks add a smaller call for each candidate pair.

Total: roughly $15 to $30 a month, and about half that if the heavy work runs in DeepSeek's off-peak hours. A verify pass (section 7.9) would add to it.

Prices change. Check DeepSeek's pricing page when M5 starts, and put the current rates into the cost calculation for `llm_calls`.

## 14. Milestones

Each milestone ends with an exit check that must pass before the next milestone starts. Work in order, except that M2 and M3 can run alongside M4 to M6.

| # | Milestone | Exit check |
|---|---|---|
| M0 | Host setup and the Reuters probe (`CLAUDE.md`, "Host setup") | The probe saves rows with exit code 0, or Sam has decided to run without Reuters |
| M1 | SQLite store, store sink, hourly scrape on the schedule | The 136 existing tests and the new ones pass. A second run inserts 0 rows. Two writers run for 60 seconds without "database is locked". The scheduled scrape keeps adding rows for 24 hours. |
| M2 | Contract freeze | The schema and the three HMAC test vectors are committed to both repos, and pass in `unittest` and `vitest` |
| M3 | Provenance PR A | Provenance tests and build pass. A signed POST from `newsfeed/sign.py` to a local standalone build makes `/api/world-news` return the fixture ids. Replayed, stale and oversized requests are rejected. After merge, production answers 404 with no secret set. |
| M4 | Dev label set | 150 labels are schema-valid. Sam's 30 are done, and the agreement report exists. |
| M5 | Extract | Recorded-response tests cover empty content, `length`, 402, 429 and 503. At least 99% of dev-set outputs are schema-valid. Cost per story is reported. The model id is confirmed. |
| M6 | Resolve | The golden tests pass. GeoNames source hashes are recorded. |
| M7 | Cluster, then the prompt freeze | Two London stabbings on the same day stay two clusters. No chaining. Cluster ids are identical after a shuffled rerun. Extract, resolve and cluster run on the schedule without publishing. The freeze date is recorded. |
| M8 | Gate | At least 7 days after the freeze: at most 3 wrong in 153 for pins, for World news items and for merged pairs, and the strata are checked. Verify is added only if needed, and the result is then confirmed on a fresh sample. |
| M9 | Provenance PR B | Provenance tests and build pass. `/api/signals/news-events` serves pins after a push. The PR includes screenshots of a pin's dossier, the widget and the landing globe. |
| M10 | Publish, health and schedule, then live | A production push is visible. The layer is back within 3 minutes of `systemctl restart provenance`. The dead-man alert fires when publish is stopped. One week of measured volume, cost, pin counts and the first weekly audit are written back into this document. |

## 15. Open items

These are not blocking. Each is decided when its milestone arrives.

1. **Landing audit (M9).** How the audit measures a layer that is not live in production yet. Before the secret is set, the layer can only read as locked.
2. **World Headlines.** Whether World news later replaces it. World Headlines reads six English-language RSS feeds and one Telegram channel (`lib/console/help.ts:187`).
3. **Dead-man service.** Which one to use.
4. **Categories (M4).** The final category list, refined with Sam.
