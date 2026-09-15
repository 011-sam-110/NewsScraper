"""Stage: status. What the store holds and when each outlet last sent a new story.

Not a pipeline stage. It is the one command an operator runs to see whether the schedule is
working, before the health stage (milestone M10) starts sending alerts.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .settings import Settings, load
from .store import Store


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print one JSON object instead of a table.")


def collect(store: Store) -> dict[str, Any]:
    counts = {
        "stories": store.scalar("SELECT COUNT(*) FROM stories"),
        "stories_with_text": store.scalar("SELECT COUNT(*) FROM story_texts"),
        "section_labels": store.scalar("SELECT COUNT(*) FROM story_sections"),
        "aliases": store.scalar("SELECT COUNT(*) FROM story_aliases"),
    }
    by_status = {row["status"]: row["n"] for row in store.query(
        "SELECT status, COUNT(*) AS n FROM stories GROUP BY status ORDER BY status"
    )}
    outlets = [dict(row) for row in store.query("SELECT * FROM outlet_health ORDER BY outlet")]
    per_outlet = {row["outlet"]: row["n"] for row in store.query(
        "SELECT outlet, COUNT(*) AS n FROM stories GROUP BY outlet ORDER BY outlet"
    )}
    for outlet in outlets:
        outlet["stories"] = per_outlet.get(outlet["outlet"], 0)
    runs = [dict(row) for row in store.query(
        "SELECT * FROM scrape_runs ORDER BY run_id DESC LIMIT 5"
    )]
    flags = {row["flag"]: row["n"] for row in store.query(
        """SELECT json_each.value AS flag, COUNT(*) AS n
             FROM stories, json_each(stories.format_flags)
            GROUP BY flag ORDER BY n DESC"""
    )}
    return {
        "database": str(store.path),
        "schema_version": store.scalar("SELECT MAX(version) FROM schema_version"),
        "data_as_of": store.data_as_of(),
        "counts": counts,
        "by_status": by_status,
        "format_flags": flags,
        "outlets": outlets,
        "recent_runs": runs,
    }


def run(args: argparse.Namespace, settings: Settings | None = None) -> int:
    settings = settings or load()
    if not settings.news_db.exists():
        print(f"No store yet at {settings.news_db}. Run: python -m newsfeed scrape --max-pages 1")
        return 1
    with Store(settings.news_db) as store:
        report = collect(store)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    counts = report["counts"]
    print(f"Store        {report['database']} (schema {report['schema_version']})")
    print(f"Data as of   {report['data_as_of'] or 'no completed run yet'}")
    print(
        f"Stories      {counts['stories']} "
        f"({counts['stories_with_text']} with text, {counts['section_labels']} section labels)"
    )
    print(f"By status    {report['by_status'] or 'none'}")
    print(f"Format flags {report['format_flags'] or 'none'}")
    print()
    print(f"{'outlet':10} {'stories':>7}  {'last new story':20} {'fails':>5}  last error")
    for outlet in report["outlets"]:
        # The last error is kept after a later run succeeds, so date it: an undated message beside
        # a zero failure count reads as a live problem when it is history.
        error = outlet["last_error"] or ""
        if error:
            when = outlet["last_error_at"] or "unknown time"
            healed = "" if outlet["consecutive_failures"] else ", since recovered"
            error = f"{error[:44]} ({when}{healed})"
        print(
            f"{outlet['outlet']:10} {outlet['stories']:>7}  "
            f"{outlet['last_new_story_at'] or 'never':20} {outlet['consecutive_failures']:>5}  {error}"
        )
    print()
    for entry in report["recent_runs"]:
        errors = json.loads(entry["errors"])
        print(
            f"run {entry['run_id']:>4}  {entry['started_at']} -> {entry['finished_at'] or 'unfinished'}  "
            f"{entry['rows_seen']} rows, {entry['new_stories']} new"
            + (f", errors: {', '.join(errors)}" if errors else "")
        )
    return 0
