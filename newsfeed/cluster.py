"""Cluster: decide which stories are the same event, and whether that event is a pin.

Section 7.8 of docs/ARCHITECTURE.md. Extract says what one story reports and resolve says where.
This stage is the only one that compares stories with each other, and it is where the expensive
mistake lives: two different events merged into one pin reads as a single confident fact, and
nothing downstream can tell it was ever two.

WHY EVERY COMPARISON IS AGAINST THE FOUNDER, NEVER THE LATEST MEMBER. Chaining is the failure.
Story A matches B, B matches C, and A and C are different events, so a cluster walks across a city
one plausible pair at a time and ends up somewhere nobody reported. Comparing against the founding
story only means every member has been judged the same event as one fixed story, so the cluster
cannot drift. The cost is that a genuinely matching story can be refused because it shares its
event with a later member rather than the founder, which produces two clusters for one event. Two
clusters for one event is a duplicate on the map; one cluster for two events is a lie on the map.

WHY A CANDIDATE GATE AT ALL. The same-event check is a model call, so comparing every new story
with every open cluster would cost money proportional to the square of the feed. The gate is cheap
and deliberately generous: it is allowed to admit pairs the model then rejects, and it must not
reject a pair the model would have accepted, because a rejection here is never seen again.

THE PIN RULE IS A VETO, NOT A VOTE. A cluster is a pin only when its founder passes the map rule
AND every member that resolved to a place sits within PIN_SPREAD_KM of it. One member far away
means the cluster is not one located event, whatever the others say, so it goes to World news with
a reason rather than being pinned at the majority position.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from .config import cluster_config_hash, extract_config_hash, resolve_config_hash
from .deepseek import (
    DEFAULT_MODEL,
    BalanceError,
    Client,
    FatalError,
    TransientError,
)
from .extract import BudgetReached
from .geonames import Gazetteer, build_hash
from .prompts import CLUSTER_SYSTEM, build_cluster_user
from .resolve import distance_km
from .settings import Settings, load
from .store import Store

STAGE = "cluster"

# Section 7.8 step 1: a candidate cluster's founding story must be published within this of the
# story being placed. Two days is wide enough for an event reported late by a weekly outlet and
# narrow enough that an annual commemoration does not join the original.
CLUSTER_WINDOW_HOURS = 48

# How close two resolved places have to be to make a pair worth asking the model about.
CANDIDATE_RADIUS_KM = 50.0

# Section 7.8 step 5. Deliberately the same figure as the candidate radius: a member that could
# not have made the cluster a candidate on distance cannot keep it a pin either.
PIN_SPREAD_KM = 50.0

# How much of two headlines must overlap before they are worth a model call. Low on purpose: this
# gate only has to be cheaper than the call, and a pair it wrongly drops is never reconsidered.
HEADLINE_OVERLAP = 0.34

# Words that carry no event identity. Kept short and English-only on purpose: a longer list starts
# encoding which stories we expect, and the gate is meant to be generous.
STOP_WORDS = frozenset(
    """a an and are as at be by for from has have in is it its of on or that the to was were
    with after before says said new latest live update updates""".split()
)

# Zero, like extract. The same pair asked twice must not answer differently, or a rebuild under an
# unchanged config hash would produce different clusters from the same stories.
CLUSTER_TEMPERATURE = 0.0

VERDICT_FOUNDER = "founder"
VERDICT_SAME = "same"
VERDICT_DIFFERENT = "different"
VERDICT_UNSURE = "unsure"

# Only this one joins. Section 7.8 step 2 is explicit that unsure does not.
JOINING_VERDICTS = frozenset({VERDICT_SAME})


@dataclass(frozen=True)
class StoryFacts:
    """What the cluster stage needs about one story. Assembled once, compared many times."""

    story_id: str
    outlet: str
    primary_alias: str
    headline: str
    published: str | None
    country: str | None
    entities: tuple[str, ...] = ()
    latitude: float | None = None
    longitude: float | None = None
    place_precision: str | None = None
    pinnable: bool = False
    is_physical_event: bool = False
    format_flags: tuple[str, ...] = ()
    # Shown to the model, never to a reader. cluster_hint is the one-line summary extract wrote for
    # exactly this purpose (section 6.2), which is why the article text is not carried here at all.
    cluster_hint: str | None = None
    event_date: str | None = None
    place_name: str | None = None

    @property
    def is_live_blog(self) -> bool:
        """Section 7.8 step 6. A live blog is many events in one document, so it is never one."""
        return "live" in self.format_flags

    @property
    def located(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass
class ClusterFacts:
    """An open cluster, as the candidate gate sees it."""

    cluster_id: str
    founder: StoryFacts
    members: list[StoryFacts] = field(default_factory=list)


def make_cluster_id(outlet: str, alias: str) -> str:
    """Section 7.8 step 4. Minted once from the founder and never changed.

    It is derived rather than random so that rebuilding under a new config hash gives the same
    cluster the same id, which is what lets the box keep meaning one event by one id.
    """
    digest = hashlib.sha256(f"{outlet}\n{alias}".encode("utf-8")).hexdigest()
    return f"nf_{digest[:12]}"


def headline_tokens(headline: str) -> set[str]:
    """Lower-case word tokens worth comparing, with stop words and short fragments dropped."""
    words = re.findall(r"[a-z0-9]+", (headline or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOP_WORDS}


def headline_overlap(first: str, second: str) -> float:
    """Jaccard overlap of the two token sets. Symmetric, so the order stories arrive cannot matter."""
    a, b = headline_tokens(first), headline_tokens(second)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def shared_entities(first: Iterable[str], second: Iterable[str]) -> set[str]:
    """Case-folded intersection. An entity is a name, and outlets capitalise names differently."""
    a = {e.strip().casefold() for e in first if e and e.strip()}
    b = {e.strip().casefold() for e in second if e and e.strip()}
    return a & b


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def within_window(first: str | None, second: str | None, hours: int = CLUSTER_WINDOW_HOURS) -> bool:
    """Are two publication times close enough for one event?

    A story with no usable publication time fails this rather than passing it. `tsExact` exists
    because some outlets give none, and a story whose time we had to invent must not be allowed to
    join an event on the strength of the time we invented.
    """
    a, b = _parse_time(first), _parse_time(second)
    if a is None or b is None:
        return False
    return abs(a - b) <= timedelta(hours=hours)


def candidate_reasons(story: StoryFacts, cluster: ClusterFacts) -> list[str]:
    """Why this cluster is worth a model call for this story. Empty means it is not.

    Section 7.8 step 1: the founder must be inside the window and share the country, and then any
    one of place, entity or headline is enough.
    """
    founder = cluster.founder
    if story.is_live_blog or founder.is_live_blog:
        return []
    if not story.country or story.country != founder.country:
        return []
    if not within_window(story.published, founder.published):
        return []

    reasons: list[str] = []
    if story.located and founder.located:
        gap = distance_km(story.latitude, story.longitude, founder.latitude, founder.longitude)
        if gap <= CANDIDATE_RADIUS_KM:
            reasons.append(f"place within {gap:.0f} km")
    shared = shared_entities(story.entities, founder.entities)
    if shared:
        reasons.append(f"shares {sorted(shared)[0]}")
    overlap = headline_overlap(story.headline, founder.headline)
    if overlap >= HEADLINE_OVERLAP:
        reasons.append(f"headline overlap {overlap:.2f}")
    return reasons


def pin_decision(cluster: ClusterFacts) -> tuple[bool, str | None]:
    """Section 7.8 step 5. A pin, or World news and the reason it is not a pin."""
    founder = cluster.founder
    if founder.is_live_blog:
        return False, "live_blog"
    if not founder.is_physical_event:
        return False, "founder_not_an_event"
    if not founder.pinnable or not founder.located:
        return False, "founder_place_not_pinnable"

    for member in cluster.members:
        if member.story_id == founder.story_id or not member.located:
            continue
        gap = distance_km(member.latitude, member.longitude, founder.latitude, founder.longitude)
        if gap > PIN_SPREAD_KM:
            return False, "members_far_apart"
    return True, None


def parse_verdict(payload: dict[str, Any]) -> str:
    """The model's answer, reduced to one of three words. Anything unrecognised is `unsure`.

    Unrecognised must not become `same`. A malformed answer is not evidence that two stories are
    the same event, and this is the one place where being wrong merges two events into one pin.
    """
    raw = str(payload.get("verdict", "")).strip().lower()
    return raw if raw in (VERDICT_SAME, VERDICT_DIFFERENT, VERDICT_UNSURE) else VERDICT_UNSURE


@dataclass(frozen=True)
class Placement:
    """Where one story landed, and why. `founded` means it started a cluster of its own."""

    cluster_id: str
    verdict: str
    founded: bool
    compared_to: str | None = None
    considered: int = 0


def place_story(
    story: StoryFacts,
    clusters: Sequence[ClusterFacts],
    judge: Callable[[StoryFacts, StoryFacts], str],
) -> Placement:
    """Put one story in exactly one cluster, asking `judge` only about candidates.

    `judge` is passed (story, founder) and never (story, member). That is the whole anti-chaining
    rule, and keeping it in the signature means a future caller cannot quietly pass the latest
    member instead.

    Candidates are tried in order and the first `same` wins. Trying them all and picking a best
    would need a score the model does not give, and would cost a call per candidate on every story
    rather than stopping at the first match.
    """
    considered = 0
    if not story.is_live_blog:
        for cluster in clusters:
            if not candidate_reasons(story, cluster):
                continue
            considered += 1
            verdict = judge(story, cluster.founder)
            if verdict in JOINING_VERDICTS:
                return Placement(cluster.cluster_id, verdict, False, cluster.founder.story_id, considered)

    return Placement(
        make_cluster_id(story.outlet, story.primary_alias),
        VERDICT_FOUNDER,
        True,
        None,
        considered,
    )


def story_facts(row: dict[str, Any]) -> StoryFacts:
    """One joined row from the store, as the comparison sees it."""
    return StoryFacts(
        story_id=row["story_id"],
        outlet=row["outlet"],
        primary_alias=row["primary_alias"],
        headline=row["headline"] or "",
        published=row["published"],
        country=row["place_country"],
        entities=tuple(_json_list(row["key_entities"])),
        latitude=row["latitude"],
        longitude=row["longitude"],
        place_precision=row["place_precision"],
        pinnable=bool(row["pinnable"]),
        is_physical_event=bool(row["is_physical_event"]),
        format_flags=tuple(_json_list(row["format_flags"])),
        cluster_hint=row["cluster_hint"],
        event_date=row["event_date"],
        place_name=row["place_name"],
    )


def _json_list(raw: Any) -> list[str]:
    """A json array column, or an empty list. A malformed column must not stop the stage."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


# The columns every candidate and every subject needs. Kept in one place because the two queries
# must return the same shape for story_facts() to read either one.
STORY_COLUMNS = """
    s.story_id, s.outlet, s.primary_alias, s.headline, s.published, s.format_flags,
    e.is_physical_event, e.place_country, e.key_entities, e.cluster_hint, e.event_date, e.place_name,
    r.latitude, r.longitude, r.place_precision, r.pinnable
"""

PENDING_SQL = f"""
SELECT {STORY_COLUMNS}
FROM stories s
JOIN extractions e ON e.story_id = s.story_id AND e.config_hash = ?
LEFT JOIN resolutions r ON r.story_id = s.story_id AND r.config_hash = ?
WHERE e.accepted = 1
  AND s.published IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM cluster_members m
      WHERE m.story_id = s.story_id AND m.config_hash = ?
  )
ORDER BY s.published ASC, s.story_id ASC
LIMIT ?
"""

# Candidates are open clusters in the same country whose FOUNDER is inside the window. The window
# is applied in SQL as well as in candidate_reasons() so that a box with months of stories does not
# load every cluster it ever made to reject them one at a time in Python.
CANDIDATE_SQL = f"""
SELECT c.cluster_id, {STORY_COLUMNS}
FROM clusters c
JOIN stories s ON s.story_id = c.founder_story
JOIN extractions e ON e.story_id = s.story_id AND e.config_hash = ?
LEFT JOIN resolutions r ON r.story_id = s.story_id AND r.config_hash = ?
WHERE c.config_hash = ?
  AND c.open = 1
  AND c.country IS ?
  AND c.founder_published >= ?
  AND c.founder_published <= ?
ORDER BY c.founder_published ASC
"""


def as_prompt_story(facts: StoryFacts) -> dict[str, Any]:
    """What the model is shown about one story. Never the article text: see build_cluster_user."""
    return {
        "headline": facts.headline,
        "published": facts.published,
        "event_date": facts.event_date,
        "place": facts.place_name,
        "cluster_hint": facts.cluster_hint,
        "key_entities": list(facts.entities),
    }


class ModelJudge:
    """Asks DeepSeek whether two reports are the same happening, once per pair.

    Every answer is recorded in `llm_calls` whatever it was, including a refusal to parse, so the
    cost of this stage is visible in the same place as every other stage rather than only in the
    verdicts it produced.

    A transient failure raises rather than returning `different`. Returning a verdict on a call that
    did not happen would quietly split a real cluster and leave nothing to find later.
    """

    def __init__(self, store: Store, client: Client, config_hash: str, budget_usd: float):
        self.store = store
        self.client = client
        self.config_hash = config_hash
        self.budget_usd = budget_usd
        self.calls = 0
        self.cost_usd = 0.0

    def __call__(self, subject: StoryFacts, founder: StoryFacts) -> str:
        if self.store.spend_today() >= self.budget_usd:
            raise BudgetReached(f"the day's budget of ${self.budget_usd:.2f} is used up")

        user = build_cluster_user(as_prompt_story(founder), as_prompt_story(subject))
        completion = self.client.complete(
            CLUSTER_SYSTEM, user, temperature=CLUSTER_TEMPERATURE, json_object=True
        )
        self.calls += 1
        self.cost_usd += completion.cost_usd

        try:
            payload = json.loads(completion.content)
        except (TypeError, ValueError):
            payload = {}
        verdict = parse_verdict(payload if isinstance(payload, dict) else {})
        # A truncated answer is unparsable json and lands on `unsure`, which does not join. Saying
        # so in the outcome keeps a run of them readable in llm_calls as a max_tokens problem
        # rather than as the model being indecisive.
        note = " (answer cut off at max_tokens)" if completion.truncated else ""

        self.store.record_call(
            STAGE,
            completion.model,
            f"{verdict}: {subject.story_id} vs {founder.story_id}{note}",
            usage=completion.usage,
            cost_usd=completion.cost_usd,
            story_id=subject.story_id,
            config_hash=self.config_hash,
            latency_ms=completion.latency_ms,
            attempts=completion.attempts,
        )
        return verdict


def open_clusters(store: Store, subject: StoryFacts, extract_hash: str, resolve_hash: str,
                  config_hash: str) -> list[ClusterFacts]:
    """The clusters worth testing against this story, oldest founder first."""
    published = _parse_time(subject.published)
    if published is None:
        return []
    window = timedelta(hours=CLUSTER_WINDOW_HOURS)
    rows = store.query(
        CANDIDATE_SQL,
        (
            extract_hash,
            resolve_hash,
            config_hash,
            subject.country,
            (published - window).isoformat(),
            (published + window).isoformat(),
        ),
    )
    return [ClusterFacts(row["cluster_id"], story_facts(row), []) for row in rows]


def members_of(store: Store, cluster_id: str, extract_hash: str, resolve_hash: str,
               config_hash: str) -> list[StoryFacts]:
    """Every story already in a cluster. Needed only for the pin spread check."""
    rows = store.query(
        f"""
        SELECT {STORY_COLUMNS}
        FROM cluster_members m
        JOIN stories s ON s.story_id = m.story_id
        JOIN extractions e ON e.story_id = s.story_id AND e.config_hash = ?
        LEFT JOIN resolutions r ON r.story_id = s.story_id AND r.config_hash = ?
        WHERE m.cluster_id = ? AND m.config_hash = ?
        """,
        (extract_hash, resolve_hash, cluster_id, config_hash),
    )
    return [story_facts(row) for row in rows]


def commit_placement(store: Store, subject: StoryFacts, placed: Placement, config_hash: str,
                     extract_hash: str, resolve_hash: str) -> tuple[bool, str | None]:
    """Write the membership, create the cluster if this story founded it, and re-decide the pin.

    The pin decision is recomputed on every join rather than only at creation, because a member
    that lands far away has to be able to take a pin away again. Section 7.8 step 5 is a veto.
    """
    now = datetime.now(timezone.utc).isoformat()
    with store.write() as connection:
        if placed.founded:
            connection.execute(
                """
                INSERT OR IGNORE INTO clusters
                    (cluster_id, config_hash, founder_story, country, founder_published,
                     latitude, longitude, place_precision, is_pin, not_pin_reason, open, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 1, ?)
                """,
                (
                    placed.cluster_id, config_hash, subject.story_id, subject.country,
                    subject.published, subject.latitude, subject.longitude,
                    subject.place_precision, now,
                ),
            )
        connection.execute(
            """
            INSERT OR REPLACE INTO cluster_members
                (story_id, config_hash, cluster_id, verdict, compared_to, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (subject.story_id, config_hash, placed.cluster_id, placed.verdict,
             placed.compared_to, now),
        )

    founder = subject if placed.founded else _founder_of(store, placed.cluster_id, config_hash,
                                                         extract_hash, resolve_hash)
    members = members_of(store, placed.cluster_id, extract_hash, resolve_hash, config_hash)
    is_pin, reason = pin_decision(ClusterFacts(placed.cluster_id, founder, members))
    with store.write() as connection:
        connection.execute(
            "UPDATE clusters SET is_pin = ?, not_pin_reason = ? WHERE cluster_id = ? AND config_hash = ?",
            (1 if is_pin else 0, reason, placed.cluster_id, config_hash),
        )
    return is_pin, reason


def _founder_of(store: Store, cluster_id: str, config_hash: str, extract_hash: str,
                resolve_hash: str) -> StoryFacts:
    row = store.one(
        f"""
        SELECT {STORY_COLUMNS}
        FROM clusters c
        JOIN stories s ON s.story_id = c.founder_story
        JOIN extractions e ON e.story_id = s.story_id AND e.config_hash = ?
        LEFT JOIN resolutions r ON r.story_id = s.story_id AND r.config_hash = ?
        WHERE c.cluster_id = ? AND c.config_hash = ?
        """,
        (extract_hash, resolve_hash, cluster_id, config_hash),
    )
    if row is None:
        raise RuntimeError(f"cluster {cluster_id} has no founder row")
    return story_facts(row)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=200, help="Most stories to place in one run.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="DeepSeek model id.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Say how many stories are waiting and how many pairs the gate would offer the model, "
             "without calling it.",
    )
    parser.add_argument(
        "--allow-unresolved", action="store_true",
        help="Cluster even though resolve has not placed every physical event. The unplaced ones "
             "become World news and stay that way, so this is for a store that will never be "
             "resolved, not for getting past the check.",
    )


# Physical events that resolve should have placed, but has not. Cluster reads the coordinate that
# resolve wrote and never computes one, so running out of order does not fail: it quietly produces
# clusters that could not use place as a candidate reason, and pins nothing, under a config hash
# that says the run is current. The hash covers the resolve CONFIG, not whether resolve ever ran.
UNRESOLVED_SQL = """
SELECT COUNT(*)
FROM extractions e
WHERE e.config_hash = ?
  AND e.accepted = 1
  AND e.is_physical_event = 1
  AND e.place_name IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM resolutions r
      WHERE r.story_id = e.story_id AND r.config_hash = ?
  )
"""


def estimate(store: Store, pending: Sequence[Any], extract_hash: str, resolve_hash: str,
             config_hash: str) -> str:
    """An UPPER BOUND on the model calls a real run would make, without making any.

    Counting candidates against the stored clusters alone reports zero on a cold store, which is
    both useless and reassuring in the wrong direction. So this simulates the run: each story is
    tested against the clusters that already exist AND against the ones the earlier stories in this
    batch would have opened.

    It assumes every verdict is `different`. That is the worst case and not a pessimistic guess: a
    `same` makes place_story stop at the first match, so it costs one call and opens no new cluster
    for later stories to be tested against. The real number can only come in under this one.
    """
    opened: list[ClusterFacts] = []
    pairs = 0
    for row in pending:
        subject = story_facts(row)
        stored = open_clusters(store, subject, extract_hash, resolve_hash, config_hash)
        for cluster in [*stored, *opened]:
            if candidate_reasons(subject, cluster):
                pairs += 1
        opened.append(
            ClusterFacts(make_cluster_id(subject.outlet, subject.primary_alias), subject, [subject])
        )
    return (
        f"would place {len(pending)} stories with at most {pairs} model calls. "
        "That is the worst case, where the model calls every pair different: a match stops the "
        "calls for that story and opens no cluster for the next one to be tested against."
    )


def run(args: argparse.Namespace, settings: Settings | None = None) -> int:
    settings = settings or load()
    settings.ensure_data_dir()

    extract_hash = extract_config_hash(args.model)
    try:
        gazetteer = Gazetteer.open(settings.geonames_db)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2
    # Only the hash is wanted here. Cluster never asks the gazetteer a question: resolve already
    # wrote the coordinate, and re-reading it would let the two stages disagree about one story.
    resolve_hash = resolve_config_hash(build_hash(gazetteer.connection))
    gazetteer.connection.close()
    config_hash = cluster_config_hash(resolve_hash, extract_hash, args.model)

    with Store(settings.news_db) as store:
        store.migrate()

        unresolved = store.scalar(UNRESOLVED_SQL, (extract_hash, resolve_hash))
        if unresolved and not args.allow_unresolved:
            print(
                f"{unresolved} physical events have no place under resolve config {resolve_hash}. "
                "Clustering now would pin none of them and would record that as done."
                + os.linesep
                + "  Run: python -m newsfeed resolve"
                + os.linesep
                + "Pass --allow-unresolved to cluster them as World news on purpose.",
                file=sys.stderr,
            )
            return 2

        pending = store.query(PENDING_SQL, (extract_hash, resolve_hash, config_hash, args.limit))
        placed_already = store.scalar(
            "SELECT COUNT(*) FROM cluster_members WHERE config_hash = ?", (config_hash,)
        )
        print(
            f"config {config_hash}  model {args.model}  waiting {len(pending)}  "
            f"already placed {placed_already}  "
            f"spent today ${store.spend_today():.4f} of ${settings.daily_budget_usd:.2f}",
            file=sys.stderr,
        )
        if not pending:
            print("Nothing waiting.", file=sys.stderr)
            return 0

        if args.dry_run:
            print(estimate(store, pending, extract_hash, resolve_hash, config_hash), file=sys.stderr)
            return 0

        client = Client(settings.require("deepseek_api_key"), model=args.model)
        judge = ModelJudge(store, client, config_hash, settings.daily_budget_usd)

        founded = joined = pins = 0
        stopped: str | None = None
        exit_code = 0
        try:
            for row in pending:
                subject = story_facts(row)
                clusters = open_clusters(store, subject, extract_hash, resolve_hash, config_hash)
                placed = place_story(subject, clusters, judge)
                is_pin, _ = commit_placement(
                    store, subject, placed, config_hash, extract_hash, resolve_hash
                )
                founded += int(placed.founded)
                joined += int(not placed.founded)
                pins += int(is_pin and placed.founded)
        except BudgetReached as error:
            stopped = f"Stopped: {error}. Model stages resume at the next UTC day."
        except TransientError as error:
            stopped = f"Stopped: {error} The rest is left for the next run."
        except BalanceError as error:
            stopped = f"Stopped: {error}"
            exit_code = 1
        except FatalError as error:
            stopped = f"Stopped: {error} This is a bug in the request or the key, not a blip."
            exit_code = 1

        print(
            f"placed {founded + joined} stories: {founded} founded a cluster, {joined} joined one. "
            f"{pins} of the new clusters are pins. "
            f"{judge.calls} model calls, ${judge.cost_usd:.4f}",
            file=sys.stderr,
        )
        if stopped:
            print(stopped, file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m newsfeed cluster", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
