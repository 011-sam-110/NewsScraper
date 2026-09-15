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
| Settings | `~/.config/newsfeed/newsfeed.env` |
| Timers | `~/.config/systemd/user/newsfeed-*` |

The data directory is deliberately outside the checkout. `newsfeed/settings.py` refuses a
`NEWSFEED_DATA_DIR` inside OneDrive, Dropbox or Google Drive, because file sync corrupts a SQLite
write-ahead log.

## First install

```
sudo dnf install -y google-chrome-stable xorg-x11-server-Xvfb
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
xvfb-run -a --server-args="-screen 0 1920x1080x24" \
  .venv/bin/python main.py --sections reuters:africa --max-pages 1 -o /tmp/probe
```

A pass is rows saved with exit code 0. A 401 or 403 means DataDome refused this machine or network;
check no VPN or proxy is on, then tell Sam rather than trying to work around it.

Run on this host on 2026-09-15 with Google Chrome 153.0.8010.36 under `xvfb-run`: 8 rows, all 8 with
article text, exit 0. Reuters works here, so all five outlets are on the schedule.

### Getting root on this host

There is no passwordless sudo, and `sudo` cannot prompt from a non-interactive shell. The desktop
runs KDE with a polkit agent, so `pkexec` raises a password dialog on the logged-in session:

```
pkexec /usr/bin/dnf install -y <packages>
```

Seed the store with a bounded run, because an empty store has nothing to stop a section early and
`--max-pages 0` would walk every listing to its end (up to 500 pages for PBS):

```
.venv/bin/python -m newsfeed scrape --sources bbc guardian pbs nyt --max-pages 2
.venv/bin/python -m newsfeed status
```

Then install the timers:

```
deploy/install-systemd.sh
sudo loginctl enable-linger $USER    # so the timers survive a logout
```

## The schedule

| Timer | When | What |
|---|---|---|
| `newsfeed-scrape.timer` | hourly, on the hour | BBC, the Guardian, PBS, NYT |
| `newsfeed-scrape-reuters.timer` | hourly, at 20 past | Reuters, under `xvfb-run` |

Reuters runs on its own timer and under a virtual display because it drives a visible Google Chrome
window. `xvfb-run` gives that window a display of its own, so the run does not need anyone logged
in and does not put a browser window in front of whoever is.

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
| An outlet reports `<section>: HTTP 503` and the run exits 1 | A transient outlet error. The other sections of that outlet still ran, and the next hourly run picks up what this one missed. Only worry if the same section fails for several hours. |
| One outlet goes quiet, the rest are fine | That outlet's parser broke. Save the new response into `tests/fixtures/<outlet>/`, write a test against it, then fix the parser. |
| `database is locked` | Should not happen: WAL is on with a 30 second busy timeout. Check nothing has copied the store into a synced folder. |
| The store looks wrong and you want to start again | Stop the timers, delete `~/.local/share/newsscraper/news.sqlite3*`, then seed again. Story ids are derived from outlet ids, so a rebuild gives the same ids for the same stories. |
| A timer did not run | `systemctl --user list-timers 'newsfeed-*' --all`, then `journalctl --user -u <unit>`. If the account had logged out, `loginctl enable-linger` was not set. |

## What M1 left for later milestones

- No health check, so an outlet can go quiet and nothing alerts (M10). `status` is the manual check.
- A section's transient error is not retried inside the run. It is isolated, so the outlet's other sections still run, and the next hourly run collects what it missed.
- Text is stored for BBC, the Guardian, PBS and Reuters. NYT sends no text, by design.
- Nothing reads the store yet. `extract` (M5) is the next stage that does.
