"""Stage: drop-outlets. Forget the stored rows of the outlets Sam dropped (scraper.DROPPED_OUTLETS).

Run once, by hand, after the outlets leave the schedule. A dry run by default:

    python -m newsfeed drop-outlets          # what would go
    python -m newsfeed drop-outlets --yes    # delete it

Deleting a story removes its aliases, sections, text, extraction, resolution and cluster rows (the
foreign keys cascade). Two things are handled here, because a cascade alone would get them wrong:

- A cluster founded by a dropped story is deleted with it, but a story from another outlet that
  joined that cluster would keep a member row pointing at nothing, and the cluster stage would never
  look at it again. Those member rows are deleted too, so the next cluster run places them afresh.
- llm_calls is kept. It is the record of what was paid, and has no foreign key to the story.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from scraper import DROPPED_OUTLETS

from .settings import Settings, load
from .store import Store

if TYPE_CHECKING:  # pragma: no cover
    import argparse

ORPHANED_MEMBERS = """
SELECT m.story_id, m.config_hash
FROM cluster_members m
JOIN clusters c ON c.cluster_id = m.cluster_id AND c.config_hash = m.config_hash
JOIN stories founder ON founder.story_id = c.founder_story
JOIN stories member ON member.story_id = m.story_id
WHERE founder.outlet IN ({marks}) AND member.outlet NOT IN ({marks})
"""


def _checked(outlets: Sequence[str]) -> tuple[str, ...]:
    refused = [outlet for outlet in outlets if outlet not in DROPPED_OUTLETS]
    if refused:
        raise ValueError(f"not a dropped outlet: {', '.join(refused)}")
    return tuple(outlets)


def plan(store: Store, outlets: Sequence[str] = DROPPED_OUTLETS) -> dict[str, Any]:
    outlets = _checked(outlets)
    marks = ", ".join("?" for _ in outlets)
    return {
        "outlets": list(outlets),
        "stories": store.scalar(f"SELECT COUNT(*) FROM stories WHERE outlet IN ({marks})", outlets),
        "clusters": store.scalar(
            f"SELECT COUNT(*) FROM clusters c JOIN stories s ON s.story_id = c.founder_story"
            f" WHERE s.outlet IN ({marks})", outlets,
        ),
        "other_members": len(store.query(ORPHANED_MEMBERS.format(marks=marks), outlets * 2)),
    }


def apply(store: Store, outlets: Sequence[str] = DROPPED_OUTLETS) -> dict[str, Any]:
    """Delete in one transaction, and return what plan() reported before it."""
    report = plan(store, outlets)
    outlets = tuple(report["outlets"])
    marks = ", ".join("?" for _ in outlets)
    with store.write() as connection:
        orphans = connection.execute(ORPHANED_MEMBERS.format(marks=marks), outlets * 2).fetchall()
        connection.executemany(
            "DELETE FROM cluster_members WHERE story_id = ? AND config_hash = ?",
            [(row[0], row[1]) for row in orphans],
        )
        connection.execute(f"DELETE FROM stories WHERE outlet IN ({marks})", outlets)
        connection.execute(f"DELETE FROM outlet_health WHERE outlet IN ({marks})", outlets)
    return report


def add_arguments(parser: "argparse.ArgumentParser") -> None:
    parser.add_argument("--yes", action="store_true", help="Delete. Without it, only say what would go.")


def run(args: "argparse.Namespace", settings: Settings | None = None) -> int:
    settings = settings or load()
    with Store(settings.news_db) as store:
        report = apply(store) if args.yes else plan(store)
    verb = "Deleted" if args.yes else "Would delete"
    print(
        f"{verb} {report['stories']} stories from {', '.join(report['outlets'])}, "
        f"{report['clusters']} clusters they founded, and freed {report['other_members']} stories "
        "of other outlets to cluster again."
    )
    if not args.yes:
        print("Dry run. Add --yes to delete.")
    return 0
