"""python -m newsfeed <stage> [options]

One command per stage, as docs/ARCHITECTURE.md section 7.1 lists them. Stages that are not built
yet exit 2 and name the milestone that builds them, so a scheduler entry written early fails loudly
instead of looking as though it did something.
"""

from __future__ import annotations

import argparse
import sys

from . import extract as extract_stage
from . import cluster as cluster_stage
from . import geonames as geonames_stage
from . import rail as rail_stage
from . import resolve as resolve_stage
from . import scrape as scrape_stage
from . import status as status_stage
from .settings import SettingsError

# Stage -> the milestone in docs/ARCHITECTURE.md section 14 that builds it.
PLANNED_STAGES = {
    "publish": "M10",
    "health": "M10",
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

    geonames_parser = stages.add_parser(
        "geonames-build", help="Build the local GeoNames gazetteer. Monthly."
    )
    geonames_stage.add_arguments(geonames_parser)

    resolve_parser = stages.add_parser(
        "resolve", help="Turn each extracted place into a GeoNames coordinate, or into nothing."
    )
    resolve_stage.add_arguments(resolve_parser)

    cluster_parser = stages.add_parser(
        "cluster", help="Group reports of one happening, and decide whether it is a pin."
    )
    cluster_stage.add_arguments(cluster_parser)

    rail_parser = stages.add_parser(
        "rail",
        help="Push scraped stories to Provenance's live news rail. Not the section 8 snapshot: "
             "see section 9.0.",
    )
    rail_stage.add_arguments(rail_parser)

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
        if args.stage == "geonames-build":
            return geonames_stage.run(args)
        if args.stage == "resolve":
            return resolve_stage.run(args)
        if args.stage == "cluster":
            return cluster_stage.run(args)
        if args.stage == "rail":
            return rail_stage.run(args)
    except SettingsError as error:
        print(f"Settings: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
