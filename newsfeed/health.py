"""Stage: health. Every 15 minutes: is news still arriving, and is it reaching the site?

WHY THIS EXISTS. From 2026-10-13 to 2026-11-05 Reuters failed 156 runs in a row, and the rail timer
was never installed, so no scraped story reached Provenance at all. Nothing said so. Each check here
is one of those silent failures made loud.

The checks, each with the threshold it alarms on:
- runs:<outlet>     3 failed runs in a row (any outlet except the dropped ones)
- silence:<outlet>  no new story for 12 hours (Reuters, BBC and PBS; a single quiet feed is normal)
- newest            no new story from any outlet for 60 minutes. Measured 2026-11-05: in the last
                    24 hours of the 26 feeds, the longest gap between two stories was 23 minutes.
- rail              no successful push for 20 minutes (the rail runs every 5)
- production        Provenance's /api/status: newest headline older than 60 minutes, no push
                    received for 20 minutes, or an empty push store. A site without the `news`
                    block (before that change is deployed) is "unknown", not a failure.

Where it reports: the agent hub, which Sam reads in Discord. It posts when a check starts failing,
when one recovers, and once a day whatever the state, so silence from this stage is itself a
signal. Results are kept in health.json in the data directory. The exit code is 1 while any check
fails, so `systemctl --user --failed` shows it too.

Every detail is redacted before it is printed, saved or posted: an outlet's last error can hold a
proxy URL.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from scraper import DROPPED_OUTLETS, SCRAPERS
from scraper.common import redact

from .settings import Settings, load
from .store import Store

if TYPE_CHECKING:  # pragma: no cover
    import argparse

FAILED_RUNS = 3
SILENCE_HOURS = 12
NEWEST_MINUTES = 60
RAIL_MINUTES = 20
PRODUCTION_NEWEST_MINUTES = 60
PRODUCTION_PUSH_MINUTES = 20
DIGEST_HOURS = 24
ERROR_CHARS = 200
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

HUB_NAME = "newsfeed-health"
HUB_TASK = "NewsScraper health checks on provenance-1, every 15 minutes"
SCRAPED_OUTLETS = tuple(module.NAME for module in SCRAPERS)

# Provenance's status route sits behind Cloudflare, which refuses the default Python agent. Same
# reason and same string as the rail (newsfeed/rail.py).
USER_AGENT = "NewsScraper-health/1 (+https://github.com/011-sam-110/NewsScraper)"


@dataclass(frozen=True)
class Check:
    """One result. `ok` is True, False, or None when the check could not tell."""

    name: str
    ok: bool | None
    detail: str


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def minutes_since(moment: datetime | None, now: datetime) -> int | None:
    return None if moment is None else int((now - moment).total_seconds() // 60)


def short(text: str | None) -> str:
    cleaned = " ".join(redact(text or "").split())
    return cleaned if len(cleaned) <= ERROR_CHARS else cleaned[: ERROR_CHARS - 3] + "..."


def store_checks(store: Store, now: datetime) -> list[Check]:
    """The checks the store can answer: failed runs, silent outlets and the newest story."""
    checks: list[Check] = []
    for row in store.query("SELECT * FROM outlet_health ORDER BY outlet"):
        outlet = row["outlet"]
        if outlet in DROPPED_OUTLETS:
            continue
        failures = row["consecutive_failures"] or 0
        if failures >= FAILED_RUNS:
            checks.append(Check(
                f"runs:{outlet}", False,
                f"{outlet}: {failures} failed runs in a row, last success "
                f"{row['last_success_at'] or 'never'}. Last error: {short(row['last_error'])}",
            ))
        else:
            checks.append(Check(f"runs:{outlet}", True, f"{outlet}: {failures} failed runs in a row"))

        if outlet in SCRAPED_OUTLETS:
            newest = parse_time(row["last_new_story_at"])
            age = minutes_since(newest, now)
            silent = age is None or age > SILENCE_HOURS * 60
            checks.append(Check(
                f"silence:{outlet}", not silent,
                f"{outlet}: no new story for {age // 60} h (since {row['last_new_story_at']})"
                if silent and age is not None else
                f"{outlet}: no new story yet" if silent else
                f"{outlet}: last new story {age // 60} h {age % 60} min ago",
            ))

    marks = ", ".join("?" for _ in DROPPED_OUTLETS)
    newest_seen = store.scalar(
        f"SELECT MAX(first_seen_at) FROM stories WHERE outlet NOT IN ({marks})", DROPPED_OUTLETS
    )
    age = minutes_since(parse_time(newest_seen), now)
    if age is None:
        checks.append(Check("newest", False, "the store holds no story"))
    elif age > NEWEST_MINUTES:
        checks.append(Check("newest", False, f"no new story from any outlet for {age} min"))
    else:
        checks.append(Check("newest", True, f"newest story first seen {age} min ago"))
    return checks


def rail_check(state: dict[str, Any], now: datetime) -> Check:
    """From rail.json, which every rail run writes (newsfeed/rail.py, record_state)."""
    age = minutes_since(parse_time(state.get("last_ok_at")), now)
    error = short(state.get("last_error"))
    if age is None:
        return Check("rail", False, "the rail has never pushed" + (f". Last error: {error}" if error else ""))
    if age > RAIL_MINUTES:
        return Check(
            "rail", False,
            f"no push to Provenance for {age} min" + (f". Last error: {error}" if error else ""),
        )
    return Check(
        "rail", True,
        f"pushed {age} min ago, sent {state.get('sent', 0)}, Provenance holds {state.get('held', '?')}",
    )


def status_url(ingest_url: str) -> str:
    parts = urlsplit(ingest_url)
    return f"{parts.scheme}://{parts.netloc}/api/status"


def read_status(url: str) -> tuple[dict[str, Any] | None, str | None]:
    """GET the status route directly, never through the proxy: this is our own site."""
    request = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return None, short(str(error))


def production_check(status: dict[str, Any] | None, error: str | None, now: datetime) -> Check:
    if status is None:
        return Check("production", False, f"could not read Provenance's /api/status: {error}")
    news = status.get("news")
    if not isinstance(news, dict):
        return Check("production", None, "Provenance's /api/status has no news block yet")

    now_ms = now.timestamp() * 1000

    def minutes(value: Any) -> int | None:
        return int((now_ms - value) // 60000) if isinstance(value, (int, float)) and value > 0 else None

    problems = []
    newest = minutes(news.get("newestTs"))
    if newest is None or newest > PRODUCTION_NEWEST_MINUTES:
        problems.append(
            "the site holds no headline" if newest is None else f"the newest headline on the site is {newest} min old"
        )
    pushed = minutes(news.get("lastPushAt"))
    if pushed is None:
        problems.append("the site has received no push since it started")
    elif pushed > PRODUCTION_PUSH_MINUTES:
        problems.append(f"the site has received no push for {pushed} min")
    if not news.get("pushedHeld"):
        problems.append("the site's push store is empty")
    if problems:
        return Check("production", False, "; ".join(problems))
    return Check(
        "production", True,
        f"newest headline {newest} min old, {news.get('held')} headlines from {news.get('outlets')} outlets, "
        f"feeds answering {news.get('feedsAnswering')}/{news.get('feedsTotal')}, last push {pushed} min ago",
    )


def decide(previous: dict[str, Any], checks: list[Check], now: datetime) -> tuple[str | None, dict[str, Any]]:
    """What to post, if anything, and the state to keep. posted_* move only when a post is made."""
    by_name = {check.name: check for check in checks}
    failing = sorted(name for name, check in by_name.items() if check.ok is False)
    before = set(previous.get("posted_failing") or [])
    new = [name for name in failing if name not in before]
    still = [name for name in failing if name in before]
    recovered = sorted(before - set(failing))
    posted_at = parse_time(previous.get("posted_at"))
    digest_due = posted_at is None or now - posted_at >= timedelta(hours=DIGEST_HOURS)

    state = {
        "checked_at": now.strftime(TIME_FORMAT),
        "checks": {name: {"ok": check.ok, "detail": check.detail} for name, check in by_name.items()},
        "posted_at": previous.get("posted_at"),
        "posted_failing": sorted(before),
    }
    if not (new or recovered or digest_due):
        return None, state

    if failing:
        lines = [f"News health on provenance-1: {len(failing)} check(s) failing."]
    else:
        lines = ["News health on provenance-1: all checks pass."]
    lines += [f"New: {by_name[name].detail}" for name in new]
    lines += [f"Still: {by_name[name].detail}" for name in still]
    lines += [f"Recovered: {name}" for name in recovered]
    if not failing:
        lines += [f"{name}: {by_name[name].detail}" for name in ("newest", "rail", "production") if name in by_name]
    state["posted_at"] = now.strftime(TIME_FORMAT)
    state["posted_failing"] = failing
    return "\n".join(lines), state


def hub_command() -> str:
    """The hub CLI. A systemd user unit's PATH does not include ~/.local/bin."""
    return shutil.which("hub") or str(Path.home() / ".local" / "bin" / "hub")


def post_to_hub(text: str) -> bool:
    """Join (idempotent) and post. True only when the hub took the post."""
    hub = hub_command()
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    try:
        subprocess.run(
            [hub, "join", "--as", HUB_NAME, "--name", HUB_NAME, "--project", "provenance", "--task", HUB_TASK],
            env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
        posted = subprocess.run(
            [hub, "post", "--as", HUB_NAME, "-"], input=text,
            env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"health: could not run the hub CLI: {short(str(error))}", file=sys.stderr)
        return False
    if posted.returncode != 0:
        print(f"health: the hub refused the post: {short(posted.stderr)}", file=sys.stderr)
        return False
    return True


def read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def add_arguments(parser: "argparse.ArgumentParser") -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print the checks and the post; post and save nothing.")
    parser.add_argument("--no-production", action="store_true", help="Skip the check of Provenance's /api/status.")


def run(
    args: "argparse.Namespace",
    settings: Settings | None = None,
    *,
    post: Callable[[str], bool] = post_to_hub,
    read_status: Callable[[str], tuple[dict[str, Any] | None, str | None]] = read_status,
    now: datetime | None = None,
) -> int:
    settings = settings or load()
    now = now or datetime.now(timezone.utc)
    if not settings.news_db.exists():
        print(f"health: no store at {settings.news_db}")
        return 1

    with Store(settings.news_db) as store:
        checks = store_checks(store, now)
    checks.append(rail_check(read_json(settings.data_dir / "rail.json"), now))
    if settings.ingest_url and not args.no_production:
        status, error = read_status(status_url(settings.ingest_url))
        checks.append(production_check(status, error, now))

    for check in checks:
        mark = {True: "ok  ", False: "FAIL", None: "?   "}[check.ok]
        print(f"{mark} {check.name:22} {check.detail}")

    message, state = decide(read_json(settings.health_file), checks, now)
    if message:
        print("\n" + message)
    if args.dry_run:
        print("\n(dry run: nothing posted, health.json not written)")
    else:
        if message and not post(message):
            previous = read_json(settings.health_file)
            state["posted_at"] = previous.get("posted_at")
            state["posted_failing"] = previous.get("posted_failing") or []
        write_json(settings.health_file, state)
    return 1 if any(check.ok is False for check in checks) else 0
