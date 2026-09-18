"""The closed category list of section 6.2, and the other closed lists the model must choose from.

These are part of the config hash (section 10.5), so changing anything here means a new gate run
before any pin is published. Sam refines the list during M4; until then it is v1 as agreed.

Category and pin are separate decisions. The category says what a story is about. The map rule in
section 6.1 says whether it has a place and a date someone could have witnessed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: str
    covers: str
    # Whether a story in this category can ever be a pin. This never grants a pin on its own: the
    # map rule still has to pass.
    pin_possible: bool


CATEGORIES: tuple[Category, ...] = (
    Category("conflict", "Armed conflict, military strikes and operations, shelling", True),
    Category("attack_or_violent_crime", "Terror attacks, shootings, stabbings, assaults, murders", True),
    Category("crime_and_policing", "Arrests, raids, non-violent crime, police operations", True),
    Category("protest_and_unrest", "Protests, labour strikes, riots, civil unrest", True),
    Category("disaster", "Earthquakes, floods, storms, wildfires, eruptions", True),
    Category("accident", "Transport crashes, industrial accidents, collapses, explosions with no attacker", True),
    Category("health", "Outbreaks and public-health events at a place", True),
    Category("politics_and_diplomacy", "Elections, government decisions, summits, diplomacy", True),
    # NOT pinnable. Every story here reports a legal PROCESS about something that happened
    # somewhere else, at some other time. A courtroom is a place and a hearing is an event,
    # which is exactly why this needs saying: the pin would be defensible and still wrong,
    # because it claims the story is about that place. Section 3, and measured: on
    # 2026-09-18 all 8 of the store's courts_and_justice pins were court proceedings.
    Category("courts_and_justice", "Trials, verdicts, sentencing, inquiries", False),
    Category("economy_and_business", "Markets, companies, trade, jobs", True),
    Category("science_climate_tech", "Research, climate, technology, space", True),
    Category("society_culture_sport", "Culture, religion, sport, human interest", True),
    Category("opinion_analysis", "Opinion, analysis, explainers, reviews", False),
)

CATEGORY_IDS: tuple[str, ...] = tuple(category.id for category in CATEGORIES)
CATEGORIES_BY_ID: dict[str, Category] = {category.id: category for category in CATEGORIES}

# Why a story is not a physical happening at a place. Stored so the eval reports can count them.
NOT_EVENT_REASONS: tuple[str, ...] = (
    "opinion", "analysis", "roundup", "policy", "past_event", "no_place", "other",
)

# How precise the model says the named place is. The resolver maps these to GeoNames feature
# classes (section 7.7); region and country can never be pinned.
PLACE_KINDS: tuple[str, ...] = ("venue", "street", "district", "city", "region", "country")
PINNABLE_KINDS: frozenset[str] = frozenset({"venue", "street", "district", "city"})

# The date rule of section 6.1, measured from the story's published time.
EVENT_DATE_DAYS_BEFORE = 3
EVENT_DATE_DAYS_AFTER = 1


def category_lines() -> str:
    """The category list as the prompt shows it. Part of the prompt, so part of the config hash."""
    return "\n".join(f"- {category.id}: {category.covers}" for category in CATEGORIES)


def can_pin(category: str | None) -> bool:
    """May a story in this category be pinned? Section 3 and the table above.

    An unknown or missing category is pinnable. That is the deliberate direction to fail in: the
    extract schema already restricts the category to this table, so an unknown one should not
    happen, and if the table and the model ever do drift apart, a permissive default drops a few
    wrong pins where a strict one would silently stop pinning ANYTHING and look like a quiet week.
    """
    if not category:
        return True
    for entry in CATEGORIES:
        if entry.id == category:
            return entry.pin_possible
    return True


def pin_possible_map() -> dict[str, bool]:
    """The pin flags, for the cluster config hash. Changing one changes which stories pin."""
    return {entry.id: entry.pin_possible for entry in CATEGORIES}
