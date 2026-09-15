"""Stage: scrape. Runs the existing parsers and stores what they return, instead of JSON Lines.

Section 7.5 of docs/ARCHITECTURE.md. This stage fixes the two defects CLAUDE.md lists for a hosted
job: every run used to overwrite the last one, and nothing persisted between runs, so each run
walked every section back to where its listing ends.

The scrape loop itself is unchanged. This module only supplies a sink, which answers two questions
for each row (is it already known for this section, does its article page still need fetching) and
stores the row. main.py passes no sink, so the one-shot CLI keeps writing JSON Lines exactly as it
did.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

from scraper import SOURCES
from scraper.common import scrape

from . import identity
from .identity import now_utc
from .settings import Settings, SettingsError, load
from .store import STATUS_SCRAPED, Store

# Fields copied straight from a row onto the story, once mapped through identity.
EMPTY_VALUES: tuple[Any, ...] = (None, "", [], {})


class StoreSink:
    """The sink scraper.common.scrape writes through. One instance is shared by the outlet threads.

    Every method runs in the outlet's own thread. Store hands each thread its own SQLite
    connection, and WAL plus a 30 second busy timeout lets five of them write at once.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = threading.Lock()
        self.rows_seen: Counter[str] = Counter()
        self.new_stories: Counter[str] = Counter()
        self.updated_stories: Counter[str] = Counter()
        self.new_sections: Counter[str] = Counter()
        # URLs whose article page this run decided to fetch, so a page that gives nothing is not
        # fetched again on every later run.
        self._text_attempted: set[str] = set()

    # Questions the scrape loop asks ---------------------------------------------------

    def known(self, row: dict[str, Any]) -> bool:
        """Is this row already stored for its section? A page of these ends the section."""
        outlet = row["outlet"]
        story_id = self.store.find_story(outlet, identity.aliases(outlet, row))
        if story_id is None:
            return False
        return self.store.has_section(story_id, row["section"])

    def needs_text(self, row: dict[str, Any]) -> bool:
        """Fetch the article page for a new story, or when the listing's updated time changed.

        A story whose page was already fetched and gave nothing is not fetched again: text_fetched_at
        records the attempt, so a missing text does not mean one request an hour for ever.
        """
        outlet = row["outlet"]
        story_id = self.store.find_story(outlet, identity.aliases(outlet, row))
        stored = self.store.story(story_id) if story_id else None
        if stored is None:
            wanted = True
        else:
            listed_update = identity.utc_iso(row.get("updated"))
            changed = bool(listed_update) and listed_update != stored["updated"]
            never_tried = stored["text_fetched_at"] is None and stored["text_hash"] is None
            wanted = changed or never_tried
        if wanted and row.get("url"):
            with self._lock:
                self._text_attempted.add(row["url"])
        return wanted

    # Writing -------------------------------------------------------------------------

    def save(self, row: dict[str, Any]) -> None:
        """Store one row: the story, its aliases, the section that listed it and any text."""
        outlet = row["outlet"]
        section = row["section"]
        aliases = identity.aliases(outlet, row)
        if not aliases:
            # Nothing to identify the story by, so storing it would create a new row every run.
            with self._lock:
                self.rows_seen[outlet] += 1
            return
        text = row.get("text") if isinstance(row.get("text"), str) and row.get("text").strip() else None
        text_hash = identity.text_hash(text)
        with self._lock:
            attempted = row.get("url") in self._text_attempted
        now = now_utc()

        with self.store.write() as connection:
            story_id = self.store.find_story(outlet, aliases)
            is_new = story_id is None
            if story_id is None:
                story_id = identity.story_id(outlet, aliases[0])
                connection.execute(
                    """INSERT INTO stories (
                           story_id, outlet, primary_alias, url, source_url, headline, description,
                           published, published_raw, updated, updated_raw, authors, word_count,
                           thumbnail, categories, format_flags, text_hash, text_fetched_at,
                           first_seen_at, last_seen_at, status
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        story_id,
                        outlet,
                        aliases[0],
                        identity.canonical_url(row.get("url")),
                        row.get("url"),
                        row.get("headline"),
                        row.get("description"),
                        identity.utc_iso(row.get("published")),
                        row.get("published") if isinstance(row.get("published"), str) else None,
                        identity.utc_iso(row.get("updated")),
                        row.get("updated") if isinstance(row.get("updated"), str) else None,
                        json.dumps(identity.authors_of(row), ensure_ascii=False),
                        identity.word_count_of(row),
                        row.get("thumbnail"),
                        json.dumps(_clean_categories(row.get("categories")), ensure_ascii=False),
                        json.dumps(identity.format_flags(outlet, row)),
                        text_hash,
                        now if attempted else None,
                        now,
                        now,
                        STATUS_SCRAPED,
                    ),
                )
            else:
                self._update_story(connection, story_id, outlet, row, text_hash, attempted, now)

            for alias in aliases:
                connection.execute(
                    """INSERT INTO story_aliases (outlet, alias, story_id, first_seen_at)
                       VALUES (?, ?, ?, ?) ON CONFLICT(outlet, alias) DO NOTHING""",
                    (outlet, alias, story_id, now),
                )
            new_section = is_new or not self.store.has_section(story_id, section)
            connection.execute(
                """INSERT INTO story_sections (story_id, section, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(story_id, section) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                (story_id, section, now, now),
            )
            if text and text_hash:
                connection.execute(
                    """INSERT INTO story_texts (story_id, text, text_hash, word_count, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(story_id) DO UPDATE SET
                           text = excluded.text, text_hash = excluded.text_hash,
                           word_count = excluded.word_count, updated_at = excluded.updated_at
                       WHERE story_texts.text_hash <> excluded.text_hash""",
                    (story_id, text, text_hash, identity.word_count_of(row), now),
                )

        with self._lock:
            self.rows_seen[outlet] += 1
            if is_new:
                self.new_stories[outlet] += 1
            if new_section and not is_new:
                self.new_sections[outlet] += 1

    def _update_story(
        self,
        connection: Any,
        story_id: str,
        outlet: str,
        row: dict[str, Any],
        text_hash: str | None,
        attempted: bool,
        now: str,
    ) -> None:
        """Fill in what the story is missing and notice a material change.

        An empty new value never overwrites a stored one, so a listing row cannot wipe fields an
        article page supplied. A new headline or a new text hash sends the story back to scraped, so
        the AI layer reads it again (section 7.4).
        """
        stored = self.store.story(story_id)
        if stored is None:
            return
        updates: dict[str, Any] = {"last_seen_at": now}

        simple = {
            "url": identity.canonical_url(row.get("url")),
            "source_url": row.get("url"),
            "headline": row.get("headline"),
            "description": row.get("description"),
            "published": identity.utc_iso(row.get("published")),
            "updated": identity.utc_iso(row.get("updated")),
            "thumbnail": row.get("thumbnail"),
            "word_count": identity.word_count_of(row),
        }
        for column, value in simple.items():
            if value in EMPTY_VALUES:
                continue
            if stored[column] in EMPTY_VALUES or stored[column] != value:
                updates[column] = value
        for column, source in (("published_raw", "published"), ("updated_raw", "updated")):
            raw = row.get(source)
            if isinstance(raw, str) and raw.strip() and stored[column] != raw:
                updates[column] = raw

        authors = identity.authors_of(row)
        if authors and json.loads(stored["authors"]) != authors:
            updates["authors"] = json.dumps(authors, ensure_ascii=False)

        merged_categories = {**json.loads(stored["categories"]), **_clean_categories(row.get("categories"))}
        if merged_categories != json.loads(stored["categories"]):
            updates["categories"] = json.dumps(merged_categories, ensure_ascii=False)

        # Flags are unioned. A listing row can lack the metadata that reveals a flag, and a flag
        # only ever forbids a pin, so keeping one is the safe direction.
        flags = sorted(set(json.loads(stored["format_flags"])) | set(identity.format_flags(outlet, row)))
        if flags != json.loads(stored["format_flags"]):
            updates["format_flags"] = json.dumps(flags)

        if text_hash and text_hash != stored["text_hash"]:
            updates["text_hash"] = text_hash
        if attempted:
            updates["text_fetched_at"] = now

        material_change = ("headline" in updates and stored["headline"]) or (
            "text_hash" in updates and stored["text_hash"]
        )
        if material_change and stored["status"] != STATUS_SCRAPED:
            updates["status"] = STATUS_SCRAPED

        assignments = ", ".join(f"{column} = ?" for column in updates)
        connection.execute(
            f"UPDATE stories SET {assignments} WHERE story_id = ?", (*updates.values(), story_id)
        )
        if len(updates) > 1:
            with self._lock:
                self.updated_stories[outlet] += 1


def _clean_categories(categories: Any) -> dict[str, Any]:
    """The outlet's own taxonomy as a JSON object. Nothing here is inferred; section labels only."""
    if not isinstance(categories, dict):
        return {}
    return {str(key): value for key, value in categories.items() if value not in (None, "", [], {})}


def section_ids() -> list[str]:
    return [f"{name}:{section}" for name, source in SOURCES.items() for section in source.SECTIONS]


def build_jobs(sources: list[str], sections: list[str] | None) -> list[tuple[ModuleType, str]]:
    if sections:
        jobs = []
        for section_id in dict.fromkeys(sections):
            name, section = section_id.split(":", 1)
            jobs.append((SOURCES[name], section))
        return jobs
    return [(SOURCES[name], section) for name in dict.fromkeys(sources) for section in SOURCES[name].SECTIONS]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=list(SOURCES),
        default=list(SOURCES),
        metavar="SOURCE",
        help=f"Outlets to scrape, all at the same time. Default: all. Choices: {', '.join(SOURCES)}.",
    )
    parser.add_argument(
        "--sections",
        nargs="+",
        choices=section_ids(),
        metavar="SOURCE:SECTION",
        help="Scrape only these sections, for example reuters:africa. Default: every section.",
    )
    parser.add_argument("--size", type=int, default=8, help="Articles requested per listing page.")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between requests.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help=(
            "Maximum listing pages per section; 0 relies on the store to stop each section at the "
            "first page holding no new story. Use a small number for the first run, which has an "
            "empty store and would otherwise walk every listing to its end."
        ),
    )
    parser.add_argument(
        "--include-text", dest="include_text", action="store_true", default=True,
        help="Fetch each article page when the listing does not include full text (default).",
    )
    parser.add_argument(
        "--no-include-text", dest="include_text", action="store_false",
        help="Skip article-page requests and keep only listing data.",
    )
    parser.add_argument(
        "--jsonl-dir",
        type=Path,
        help="Also write the old JSON Lines tree here. Off by default: a scheduled run stores rows only.",
    )


def run(args: argparse.Namespace, settings: Settings | None = None) -> int:
    """Scrape into the store. Returns 1 if any outlet failed, as main.py does."""
    settings = settings or load()
    settings.ensure_data_dir()
    if args.size < 1 or args.delay < 0 or args.max_pages < 0:
        print("--size must be positive; --delay and --max-pages cannot be negative.", file=sys.stderr)
        return 2

    jobs = build_jobs(args.sources, args.sections)
    if not jobs:
        print("Nothing to scrape.", file=sys.stderr)
        return 2
    job_ids = [f"{source.NAME}:{section}" for source, section in jobs]
    outlets = list(dict.fromkeys(source.NAME for source, _ in jobs))

    if args.jsonl_dir:
        # A debugging run: behave exactly as main.py does and leave the store untouched, so a
        # JSON Lines run never lands in outlet_health and never looks like a scheduled run to the
        # health stage.
        totals, errors = scrape(
            args.jsonl_dir, jobs, args.size, args.delay, args.max_pages, args.include_text
        )
        for outlet, total in totals.items():
            print(f"{outlet}: saved {total} rows to {args.jsonl_dir / outlet}", file=sys.stderr)
        for outlet, message in errors.items():
            print(f"Error ({outlet}): {message}", file=sys.stderr)
        print("--jsonl-dir was given, so nothing was written to the store.", file=sys.stderr)
        return 1 if errors else 0

    with Store(settings.news_db) as store:
        run_id = store.start_run(job_ids)
        for outlet in outlets:
            store.start_outlet_run(outlet)
        sink = StoreSink(store)
        totals, errors = scrape(
            Path("news"), jobs, args.size, args.delay, args.max_pages, args.include_text, sink=sink
        )
        for outlet in outlets:
            store.finish_outlet_run(outlet, errors.get(outlet), sink.new_stories[outlet])
        store.finish_run(run_id, sum(sink.rows_seen.values()), sum(sink.new_stories.values()), errors)

        for outlet in outlets:
            print(
                f"{outlet}: {sink.rows_seen[outlet]} rows, {sink.new_stories[outlet]} new stories, "
                f"{sink.updated_stories[outlet]} updated, {sink.new_sections[outlet]} new section labels",
                file=sys.stderr,
            )
        print(
            f"store: {store.scalar('SELECT COUNT(*) FROM stories')} stories, "
            f"{store.scalar('SELECT COUNT(*) FROM story_texts')} with text, "
            f"{store.scalar('SELECT COUNT(*) FROM story_sections')} section labels "
            f"({settings.news_db})",
            file=sys.stderr,
        )
    for outlet, message in errors.items():
        print(f"Error ({outlet}): {message}", file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m newsfeed scrape", description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except SettingsError as error:
        print(f"Settings: {error}", file=sys.stderr)
        return 2
