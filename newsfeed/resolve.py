"""Resolve: turn the place a model named into a coordinate GeoNames vouches for, or into nothing.

Section 7.7 of docs/ARCHITECTURE.md. Every rule here exists to make one failure impossible: a pin
that is confidently in the wrong place. Provenance already carries that lesson from GDELT, where
76.5 per cent of rows were not the coded event at the coded place, and almost all of it was bad
labelling rather than bad geocoding.

THE THREE RULES THAT DO THE WORK.

1. A coordinate always comes from a matched GeoNames row. There is no path in this module that
   invents, averages or interpolates one. A place that does not match is unresolved, and an
   unresolved place makes the story World news rather than a pin.

2. When two candidates are both plausible and far apart, the answer is not the bigger one. It is
   the containing place. Westminster is a district of London and a city in Colorado; answering
   London is right, answering either Westminster on a coin toss is the failure this whole stage
   exists to prevent. Climbing loses precision, which is visible and honest; guessing loses
   correctness, which is not.

3. Precision is read off the matched feature, never off the kind the model asked for. A model that
   calls a region a city cannot promote a region to a pin, because the pin test reads the
   GeoNames feature code and region is not pinnable.

WHY DISTANCE AND NOT ADMIN CODES. Section 7.7 is explicit that GeoNames admin codes are too uneven
across countries to contain a place by code alone. Some countries put a city in ADM2, others in
ADM3, and a few leave the column empty. Distance from the resolved container is uneven in a
different and more forgiving way: it is wrong only at the edges, and the edges are tuned here as
CITY_RADIUS_KM and REGION_RADIUS_KM rather than hidden in a join.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import resolve_config_hash
from .geonames import Gazetteer, Place, build_hash
from .settings import Settings, load
from .store import Store

# How close a candidate must be to its resolved container. Section 7.7 names both figures. A city
# is a point with a radius; a region is an area whose centre can be far from its edge, so it gets
# the wider gate. Inside the country is the whole gate when the container is a country, and that
# is already enforced by the country restriction on every lookup.
CITY_RADIUS_KM = 30.0
REGION_RADIUS_KM = 150.0

# Two candidates further apart than this, ranking about equally, are ambiguous. Nearer than this
# and picking either one puts the pin in the same place on any map a reader would look at.
AMBIGUITY_DISTANCE_KM = 25.0

# What "about equally" means. Same fit, and the runner-up is not dwarfed. Paris, France beats
# Paris, Texas on population by three orders of magnitude and is not ambiguous with it; two
# villages of the same name and similar size are.
AMBIGUITY_POPULATION_RATIO = 0.5

# How specific each precision is. Used to score how well a matched feature fits the kind the model
# asked for: the closer in this order, the better the fit.
PRECISION_ORDER = {"point": 0, "district": 1, "city": 2, "region": 3, "country": 4}

# The precision each extracted kind is asking for. `street` is here because the model emits it and
# GeoNames has no road class we keep, so a street reliably fails to match and climbs to its city,
# which is the behaviour section 7.7 asks for in its fourth golden test.
KIND_PRECISION = {
    "venue": "point",
    "street": "point",
    "district": "district",
    "city": "city",
    "region": "region",
    "country": "country",
}


@dataclass(frozen=True)
class Resolution:
    """What resolve decided, including why, so a wrong pin can be explained after the fact."""

    place: Place | None
    precision: str | None
    reason: str
    climbed: bool = False

    @property
    def resolved(self) -> bool:
        return self.place is not None

    @property
    def pinnable(self) -> bool:
        return self.place is not None and self.place.pinnable


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Plain haversine: the gates here are tens of kilometres, not metres."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def fit(kind: str | None, place: Place) -> int:
    """How well a matched feature fits the kind asked for. Higher is better; 0 is the best fit.

    Negative, so that sorting descending puts the best fit first alongside population. A feature
    with no precision at all scores below every feature that has one, because it can never be
    pinned and should never outrank something that can.
    """
    precision = place.precision
    if precision is None:
        return -99
    wanted = KIND_PRECISION.get((kind or "").strip().lower())
    if wanted is None:
        return 0
    return -abs(PRECISION_ORDER[precision] - PRECISION_ORDER[wanted])


def _ranked(candidates: list[Place], kind: str | None) -> list[Place]:
    """Best fit first, ties broken by population, then by id so the order can never wobble."""
    return sorted(candidates, key=lambda p: (fit(kind, p), p.population, -p.geonames_id), reverse=True)


def _ambiguous(first: Place, second: Place, kind: str | None) -> bool:
    """Section 7.7 rule 5: about equal, and far enough apart that the difference shows on a map."""
    if fit(kind, first) != fit(kind, second):
        return False
    if distance_km(first.latitude, first.longitude, second.latitude, second.longitude) <= AMBIGUITY_DISTANCE_KM:
        return False
    if first.population > 0:
        return second.population >= first.population * AMBIGUITY_POPULATION_RATIO
    # Neither has a population, so there is nothing to tell them apart with.
    return second.population == 0


def _inside(candidate: Place, container: Place) -> bool:
    """Is a candidate inside its container?

    DISTANCE ALONE IS WRONG FOR A LARGE REGION, AND THIS IS MEASURED. Section 7.7 says to keep
    candidates within 150 km of a region. Texas is about 1,200 km across, and Paris, Texas sits
    roughly 440 km from the point GeoNames gives as the Texas centroid, so the radius rule throws
    away the right answer in the very example the section uses as its second golden test. Any
    radius wide enough for Texas is meaningless for a small region.

    So containment is read from the admin code when the container IS an admin area, and from
    distance otherwise. The section warns that admin codes are too uneven ACROSS COUNTRIES to rely
    on, and that warning is about which level a city sits at, not about whether a US row carries
    TX. Inside one country, against one named container, the code is exact. Distance stays as the
    fallback for the case the code cannot answer: either side missing the column.
    """
    precision = container.precision
    if precision == "country":
        return True  # The country restriction on the lookup already did this gate.

    if container.feature_code == "ADM1" and container.admin1:
        if candidate.admin1:
            return candidate.admin1 == container.admin1
    elif container.feature_code == "ADM2" and container.admin2:
        if candidate.admin2 and candidate.admin1:
            return candidate.admin2 == container.admin2 and candidate.admin1 == container.admin1

    radius = REGION_RADIUS_KM if precision in ("region", "country") else CITY_RADIUS_KM
    return distance_km(candidate.latitude, candidate.longitude, container.latitude, container.longitude) <= radius


def resolve_place(
    gazetteer: Gazetteer,
    name: str | None,
    within: str | None = None,
    country: str | None = None,
    kind: str | None = None,
    _climbing: bool = False,
) -> Resolution:
    """Resolve one extracted place to a GeoNames row, or explain why it could not be."""
    if not (name or "").strip():
        return Resolution(None, None, "no place name")
    if not (country or "").strip():
        # Section 7.7 step 1 restricts candidates to the country. Without one, the search is the
        # whole world and every common name is ambiguous, so this is refused rather than guessed.
        return Resolution(None, None, "no country to search in")

    container: Place | None = None
    if (within or "").strip():
        inner = resolve_place(gazetteer, within, None, country, None, _climbing=True)
        container = inner.place

    candidates = gazetteer.candidates(name, country)
    if container is not None:
        near = [c for c in candidates if _inside(c, container)]
        # Keeping the far ones when nothing is near would answer with a place the story did not
        # mean. Dropping to the container is the honest answer, and _climbing stops that recursing.
        candidates = near

    if not candidates:
        if container is not None and not _climbing:
            return Resolution(
                container,
                container.precision,
                f"{name} did not match inside {within}; climbed to it",
                climbed=True,
            )
        return Resolution(None, None, f"{name} matched nothing in {country}")

    ranked = _ranked(candidates, kind)
    best = ranked[0]

    if len(ranked) > 1 and _ambiguous(best, ranked[1], kind):
        if container is not None and not _climbing:
            return Resolution(
                container,
                container.precision,
                f"{name} is ambiguous in {country}; climbed to {within}",
                climbed=True,
            )
        return Resolution(None, None, f"{name} is ambiguous in {country} and has nothing to climb to")

    return Resolution(best, best.precision, f"matched {best.name} ({best.feature_code})")


# --------------------------------------------------------------------------------------------
# The stage. Everything above is pure and testable without a store or a gazetteer on disk.
# --------------------------------------------------------------------------------------------


def add_arguments(parser: "argparse.ArgumentParser") -> None:
    parser.add_argument("--limit", type=int, default=500, help="Most extractions to resolve in one run.")
    parser.add_argument("--story", help="Resolve one story by id, whatever it already holds.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Say what each place would resolve to, and write nothing.",
    )


def run(args: "argparse.Namespace", settings: "Settings | None" = None) -> int:
    settings = settings or load()
    settings.ensure_data_dir()

    try:
        gazetteer = Gazetteer.open(settings.geonames_db)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2

    config_hash = resolve_config_hash(build_hash(gazetteer.connection))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with Store(settings.news_db) as store:
        store.migrate()
        # Only accepted physical events with a place carry a coordinate worth resolving. Anything
        # else is World news already and asking the gazetteer about it would cost time and answer
        # nothing.
        sql = (
            "SELECT e.story_id, e.config_hash AS extract_hash, e.place_name, e.place_within,"
            " e.place_country, e.place_kind"
            " FROM extractions e"
            " WHERE e.accepted = 1 AND e.is_physical_event = 1"
            " AND e.place_name IS NOT NULL AND e.place_name <> ''"
        )
        values: list[Any] = []
        if args.story:
            sql += " AND e.story_id = ?"
            values.append(args.story)
        else:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM resolutions r"
                " WHERE r.story_id = e.story_id AND r.config_hash = ?)"
            )
            values.append(config_hash)
        sql += " ORDER BY e.created_at DESC LIMIT ?"
        values.append(max(1, args.limit))

        rows = store.query(sql, values)
        counts = {"resolved": 0, "pinnable": 0, "climbed": 0, "unresolved": 0}

        for row in rows:
            result = resolve_place(
                gazetteer,
                row["place_name"],
                row["place_within"],
                row["place_country"],
                row["place_kind"],
            )
            counts["resolved" if result.resolved else "unresolved"] += 1
            if result.pinnable:
                counts["pinnable"] += 1
            if result.climbed:
                counts["climbed"] += 1

            if args.dry_run:
                where = f"{row['place_name']} in {row['place_within'] or row['place_country']}"
                print(f"{row['story_id']}  {where}  ->  {result.reason}")
                continue

            place = result.place
            with store.write() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["story_id"],
                        config_hash,
                        row["extract_hash"],
                        1 if result.resolved else 0,
                        place.geonames_id if place else None,
                        place.name if place else None,
                        place.latitude if place else None,
                        place.longitude if place else None,
                        place.feature_code if place else None,
                        result.precision,
                        1 if result.pinnable else 0,
                        1 if result.climbed else 0,
                        result.reason,
                        now,
                    ),
                )

    gazetteer.close()
    print(
        f"resolve: {len(rows)} places, {counts['resolved']} resolved "
        f"({counts['pinnable']} pinnable, {counts['climbed']} climbed), "
        f"{counts['unresolved']} unresolved  [config {config_hash}]"
    )
    return 0
