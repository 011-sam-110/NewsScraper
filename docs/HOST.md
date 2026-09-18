# Running NewsScraper on the host machine

The host is a Fedora 44 desktop. This file is what an operator or the next agent needs to run,
check and repair the scheduled pipeline. The design is `ARCHITECTURE.md`; the project rules are
`../CLAUDE.md`.

## What is built

| Stage | State |
|---|---|
| `scrape` | Built (milestone M1). Stores rows in SQLite, hourly, on systemd timers. |
| `status` | Built. Not a pipeline stage: it is how an operator checks the schedule before the health stage exists. |
| `extract`, `resolve`, `cluster`, `publish`, `health`, `geonames-build`, `eval` | Not built. Each exits 2 and names its milestone. |

`main.py` is untouched and still writes JSON Lines exactly as it always did.

## Layout on this machine

| Thing | Path |
|---|---|
| Checkout | `~/NewsScraper` |
| Virtual environment | `~/NewsScraper/.venv` |
| Store and `health.json` | `~/.local/share/newsscraper` |
| Settings | `~/.config/newsfeed/newsfeed.env` (mode 600) |
| Timers | `~/.config/systemd/user/newsfeed-*` |

Settings are read from the host file, then a `.env` in the checkout, then real environment
variables, with later sources winning. The systemd units load the same host file with
`EnvironmentFile=`, so a scheduled run and a run by hand see the same values. Keep keys in the host
file and never in the checkout: this repo is public.

The data directory is deliberately outside the checkout. `newsfeed/settings.py` refuses a
`NEWSFEED_DATA_DIR` inside OneDrive, Dropbox or Google Drive, because file sync corrupts a SQLite
write-ahead log.

## First install

```
# xdpyinfo lets the Reuters wrapper prove the display answers before spending a request.
sudo dnf install -y google-chrome-stable xorg-x11-utils
git clone https://github.com/011-sam-110/NewsScraper.git ~/NewsScraper
cd ~/NewsScraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

Google Chrome must be the real browser. DataDome answers Playwright's bundled Chromium, Chrome for
Testing, curl and curl_cffi with 401, so `playwright install chromium` is not a substitute. Do not
change the user agent. A bare `curl https://www.reuters.com/` returning 401 on this machine is
normal and proves nothing is wrong.

### The Reuters probe (milestone M0)

```
# Two sections, not one. Under a bad setup the FIRST section still passes.
deploy/reuters-display.sh \
  .venv/bin/python main.py --sections reuters:africa reuters:americas --max-pages 1 -o /tmp/probe
```

A pass is rows saved with exit code 0. A 401 or 403 means DataDome refused this machine or network;
check no VPN or proxy is on, then tell Sam rather than trying to work around it.

Run on this host on 2026-09-15 with Google Chrome 153.0.8010.36 under `xvfb-run`: 8 rows, all 8 with
article text, exit 0. **That pass was misleading and cost three days of Reuters.** It probed one
section, and under Xvfb the first section of a session succeeds before DataDome refuses the rest:
23 of the next 24 hourly runs failed on every section they tried. A one-section probe cannot tell
a working setup from a broken one here. Probe at least two sections.

Re-run on the real display on 2026-09-18: all 15 sections, 296 rows, 137 new stories, 0 failures.

### Reuters and DataDome

**Reuters needs the seat's real display. Xvfb does not work, and this is measured.** DataDome
scores the WebGL renderer. Same public IP, minutes apart on 2026-09-18:

| How Chrome was started | Renderer it reports | Reuters |
|---|---|---|
| real display `:0` | `ANGLE (Mesa, zink Vulkan 1.4(NVIDIA GeForce GTX 1650 (NVK TU117)), OpenGL 4.6)` | 200 |
| `xvfb-run` | `ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)` | 401 |
| `xvfb-run --use-gl=egl` | the same SwiftShader string | no better |
| `xvfb-run --use-angle=vulkan` | no WebGL at all, a worse signal still | no better |
| under systemd, Wayland backend | `ANGLE (Mesa, zink ... GTX 1650 ...), OpenGL ES 3.2` | 401 after one page |

`navigator.webdriver` was false in all of them, so the renderer is the signal and the IP is not
blocked.

**The fifth row is the one that cost a day, and it was found on 2026-09-18 after the fix above was
already believed to work.** Every scheduled Reuters run between 01:25 and 09:21 took exactly 8 rows,
one page of one section, and answered 401 to everything after it. The same command run by hand,
minutes after one of those failures, took 296 rows with 0 failures. The box, the IP, the display,
the GPU and `navigator.webdriver` were identical.

The difference was one environment variable. Chrome chooses its ozone backend from the environment:
with `WAYLAND_DISPLAY` set it talks Wayland, and without it, it uses `DISPLAY` and XWayland. Both
draw on the real GPU, and they do not report the same renderer string. A login shell has no
`WAYLAND_DISPLAY`; the systemd user manager has `WAYLAND_DISPLAY=wayland-0` and
`XDG_SESSION_TYPE=wayland`. So the command worked every time a person ran it and failed every time
the timer ran it, which is the worst shape a fault can have.

`deploy/reuters-display.sh` now unsets `WAYLAND_DISPLAY` and sets `XDG_SESSION_TYPE=x11`, so both
paths start the same browser. Proved by running the same WebGL probe under `systemd-run --user`
with and without the variable: `OpenGL ES 3.2` with it, `OpenGL 4.6` without it, matching the shell
byte for byte down to the window position.

**How to tell this has come back.** A Reuters run that reports exactly 8 rows and fails every
section after the first is this fault, not a rate limit. A rate limit refuses the first section too.
 `deploy/reuters-display.sh` finds the display, proves it answers before spending a
Reuters request on it, and exits 3 with a readable message when there is none.

**The cost, stated plainly: Reuters now needs a logged-in graphical session on this box.** The
other four outlets need no display and are unaffected. If the seat is logged out, the Reuters
timer fails loudly rather than quietly collecting 401s.

DataDome does also rate-limit the Chrome session. On 2026-09-15 a run that pulled 312 rows in
eleven minutes was answered 401 on its last two sections, and a light request minutes later
worked. That is why Reuters is scheduled slower and shallower than the other four, and it is
worth doing on its own. It was not the cause of the outage above, and making it gentler did not
fix anything.

What follows from that:

- Reuters is scheduled slower and shallower than the other four (`--max-pages 3 --delay 3`). In the
  steady state every section stops on page 1 anyway.
- Never backfill Reuters. An uncapped run on a section that is behind walks its whole archive, which
  is what provoked the 401.
- A 401 is not automatically a ban. Wait a few minutes and try one section with
  `--max-pages 1 --no-include-text`. If that works, the machine is fine. If it keeps failing across
  runs an hour apart, that is the case CLAUDE.md says to report to Sam.
- A Reuters run holds a Chrome process: about 1 GB at peak. Do not run two at once.

### Getting root on this host

There is no passwordless sudo, and `sudo` cannot prompt from a non-interactive shell. The desktop
runs KDE with a polkit agent, so `pkexec` raises a password dialog on the logged-in session:

```
pkexec /usr/bin/dnf install -y <packages>
```

Seed the store with a bounded run, because an empty store has nothing to stop a section early:

```
.venv/bin/python -m newsfeed scrape --sources bbc guardian pbs nyt --max-pages 2
deploy/reuters-display.sh .venv/bin/python -m newsfeed scrape --sources reuters --max-pages 2
.venv/bin/python -m newsfeed status
```

Seed depth and the timers' `--max-pages 10` are separate numbers, and the seed one does not matter
much. A section seeded two pages deep simply fills in up to ten pages on the next scheduled run,
then settles at one or two.

Then install the timers:

```
deploy/install-systemd.sh
sudo loginctl enable-linger $USER    # so the timers survive a logout
```

## The schedule

| Timer | When | What |
|---|---|---|
| `newsfeed-scrape.timer` | hourly, on the hour | BBC, the Guardian, PBS, NYT |
| `newsfeed-scrape-reuters.timer` | every three hours, at 20 past | Reuters, on the seat's real display |
| `newsfeed-pipeline.timer` | hourly, at half past | extract, resolve, cluster |
| `newsfeed-rail.timer` | hourly, at quarter to | push the stories to Provenance |

The order inside the hour is the point: scrape, then read what was scraped, then push what was
read. A story pushed before the pipeline has seen it still reaches the World news feed, because
that feed needs a headline and a link and nothing else, but it carries no event and so cannot
reach the map until a later push.

Reuters is the exception to hourly, and not because of this order. DataDome rate-limits a session,
and a 15-section run an hour after another 15-section run was refused on every section from the
first one (measured 2026-09-18, same machine, same IP, real display). Two runs is an estimate of
where that limit sits, not a measurement of it.

The four browser-free outlets run with `--max-pages 10`. Reuters runs with `--max-pages 3 --delay 3`,
because it is the one outlet that can be refused: see "Reuters and DataDome" below.

The page cap is a backstop, not the normal stop. A section normally stops
at the first listing page where the store already holds every story, which is one or two pages once
the store is caught up. The cap matters only when the store is behind: an uncapped run there walks
a section's whole archive, which for `reuters:ukraine-russia-war` is hundreds of pages and tens of
thousands of requests from the one IP DataDome trusts. Ten pages is far more than an hour of news
for any section.

Reuters runs on its own timer and on the seat's real display, because it drives a real Google
Chrome window and DataDome refuses the software renderer a virtual display gives it. The unit
sets `NEWS_SCRAPER_CHROME_ARGS=--window-position=-32000,-32000` so the hourly window does not
appear in front of whoever is at the keyboard.

Both timers use `Persistent=true`, so a run missed while the machine was off or asleep is caught up
once. systemd will not start a second instance of a service that is still running, and
`TimeoutStartSec=45min` kills a run that has hung rather than letting it overlap the next hour.

**The machine must not sleep.** A sleeping host looks exactly like an outage of every outlet.

## Checking on it

```
.venv/bin/python -m newsfeed status            # what the store holds, per outlet
.venv/bin/python -m newsfeed status --json     # the same, for a script
systemctl --user list-timers 'newsfeed-*'
journalctl --user -u newsfeed-scrape.service -n 50
systemctl --user start newsfeed-scrape.service # run one now, off the schedule
```

`status` prints each outlet's last new story, its consecutive failure count and its last error.
Until the health stage is built (M10), nothing alerts: this command is the check.

A scheduled run logs one line per listing page and a summary per outlet, not one line per stored
row. A run stores over a thousand rows an hour, and logging each one would bury the useful lines.
Add `--verbose` when debugging a parser to get them back.

## Why a scheduled run is short

A section stops at the first listing page where the store already holds every story
(`ARCHITECTURE.md` section 7.5). So the steady state is one or two pages a section, not the whole
listing. Two things follow:

- A run that suddenly takes far longer usually means story ids changed, so nothing matches the
  store. Compare a listing row's id with `story_aliases` before assuming the outlet changed.
- `--max-pages 0` is safe on a seeded store and slow on an empty one.

## Repair

| Symptom | What to do |
|---|---|
| Reuters fails with 401 or 403 | DataDome refused this machine or network. Check Chrome is installed and that no VPN or proxy is on. If it persists, tell Sam and run the other four with `--sources bbc guardian pbs nyt`. |
| Every Reuters section fails with HTTP 404 at once | Reuters has redeployed and the Arc deployment id in the API URL has gone stale. The scraper reads the current id from the live page and retries once, so this should heal itself. If it does not, check the page still carries a `?d=<digits>` asset URL and update `DEFAULT_DEPLOYMENT` in `scraper/reuters.py`. |
| An outlet reports `<section>: HTTP 503` and the run exits 1 | A transient outlet error. The other sections of that outlet still ran, and the next hourly run picks up what this one missed. Only worry if the same section fails for several hours. |
| One outlet goes quiet, the rest are fine | That outlet's parser broke. Save the new response into `tests/fixtures/<outlet>/`, write a test against it, then fix the parser. |
| `database is locked` | Should not happen: WAL is on with a 30 second busy timeout. Check nothing has copied the store into a synced folder. |
| The store looks wrong and you want to start again | Stop the timers, delete `~/.local/share/newsscraper/news.sqlite3*`, then seed again. Story ids are derived from outlet ids, so a rebuild gives the same ids for the same stories. |
| A timer did not run | `systemctl --user list-timers 'newsfeed-*' --all`, then `journalctl --user -u <unit>`. If the account had logged out, `loginctl enable-linger` was not set. |

## What M1 left for later milestones

- No health check, so an outlet can go quiet and nothing alerts (M10). `status` is the manual check.
- A section's transient error is not retried inside the run. It is isolated, so the outlet's other sections still run, and the next hourly run collects what it missed.
- There is no history backfill. The pipeline watches for new stories, and `--max-pages 10` deliberately stops a run walking an archive. If Sam ever wants history, it is a separate one-off job, run slowly and not on the schedule.
- Text is stored for BBC, the Guardian, PBS and Reuters. NYT sends no text, by design.
- Nothing reads the store yet. `extract` (M5) is the next stage that does.
