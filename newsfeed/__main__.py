"""python -m newsfeed <stage> [options]

One command per stage, as docs/ARCHITECTURE.md section 7.1 lists them. Stages that are not built
yet exit 2 and name the milestone that builds them, so a scheduler entry written early fails loudly
instead of looking as though it did something.
"""

from __future__ import annotations

import argparse
import sys

from . import extract as extract_stage
from . import scrape as scrape_stage
from . import status as status_stage
from .settings import SettingsError

# Stage -> the milestone in docs/ARCHITECTURE.md section 14 that builds it.
PLANNED_STAGES = {
    "resolve": "M6",
    "cluster": "M7",
    "publish": "M10",
    "health": "M10",
    "geonames-build": "M6",
    "eval": "M4",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m newsfeed", description=__doc__)
    stages = parser.add_subparsers(dest="stage", metavar="STAGE", required=True)

    scrape_parser = stages.add_parser("scrape", help="Scrape the outlets into the store.")
    scrape_stage.add_arguments(scrape_parser)

    status_parser = stages.add_parser("status", help="What the store holds, and each outlet's health.")
    status_stage.add_arguments(status_parser)

    extract_parser = stages.add_parser("extract", help="Ask DeepSeek what each new story reports.")
    extract_stage.add_arguments(extract_parser)

    for name, milestone in PLANNED_STAGES.items():
        stages.add_parser(name, help=f"Not built yet: milestone {milestone}.", add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PLANNED_STAGES:
        print(
            f"The {argv[0]} stage is not built yet. It is milestone {PLANNED_STAGES[argv[0]]} in "
            "docs/ARCHITECTURE.md section 14.",
            file=sys.stderr,
        )
        return 2
    args = build_parser().parse_args(argv)
    try:
        if args.stage == "scrape":
            return scrape_stage.run(args)
        if args.stage == "status":
            return status_stage.run(args)
        if args.stage == "extract":
            return extract_stage.run(args)
    except SettingsError as error:
        print(f"Settings: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
