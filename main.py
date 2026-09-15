"""Scrape news from several outlets' world and regional sections at the same time.

Each outlet writes to its own folder: OUTPUT_DIR/<outlet>/<section>.jsonl (one JSON object a line).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import ModuleType

from scraper import SOURCES
from scraper.common import scrape


def section_ids() -> list[str]:
    return [f"{name}:{section}" for name, source in SOURCES.items() for section in source.SECTIONS]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("news"),
        help="Folder for the output; each outlet gets a subfolder. Default: news.",
    )
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
        help=(
            "Scrape only these sections, for example reuters:africa. An outlet's sections run in this order. "
            "Default: every section of every chosen source. Run --list-sections to see them."
        ),
    )
    parser.add_argument("--list-sections", action="store_true", help="Print every SOURCE:SECTION and exit.")
    parser.add_argument("--size", type=int, default=8, help="Articles requested per listing page, where the outlet allows it.")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between requests.")
    parser.add_argument(
        "--include-text",
        dest="include_text",
        action="store_true",
        default=True,
        help="Fetch each article page when the listing does not include full text (default).",
    )
    parser.add_argument(
        "--no-include-text",
        dest="include_text",
        action="store_false",
        help="Skip article-page requests and keep only listing data, so categories hold only listing labels.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum listing pages per section; 0 keeps fetching until the outlet returns no new articles.",
    )
    return parser.parse_args(argv)


def build_jobs(args: argparse.Namespace) -> list[tuple[ModuleType, str]]:
    if args.sections:
        jobs = []
        for section_id in dict.fromkeys(args.sections):
            name, section = section_id.split(":", 1)
            jobs.append((SOURCES[name], section))
        return jobs
    return [(SOURCES[name], section) for name in dict.fromkeys(args.sources) for section in SOURCES[name].SECTIONS]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Piped output on Windows is cp1252, which cannot encode many headlines. A bad character
    # in a progress line must not stop an outlet.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    if args.list_sections:
        print("\n".join(section_ids()))
        return 0
    if args.size < 1 or args.delay < 0 or args.max_pages < 0:
        print("--size must be positive; --delay and --max-pages cannot be negative.", file=sys.stderr)
        return 2
    totals, errors = scrape(
        args.output_dir, build_jobs(args), args.size, args.delay, args.max_pages, args.include_text
    )
    for name, total in totals.items():
        print(f"{name}: saved {total} articles to {args.output_dir / name}", file=sys.stderr)
    for name, message in errors.items():
        print(f"Error ({name}): {message}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
