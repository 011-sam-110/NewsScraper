"""Stage: verify. A fresh DeepSeek call checks every headline pin, and a critic settles any flag.

    python -m newsfeed verify --dry-run      # how many pins wait for a check, no model call
    python -m newsfeed verify                # check them (calls 2 and 3), then headline-places sends

Docs: docs/ARCHITECTURE.md section 7.9. Sam asked for it on 5 Nov 2026: "a second verification layer (a
fresh deepseek agent) that checks every claim. If it flags something differently, ANOTHER critic should
check it again."

WHERE IT SITS. After extract (call 1) and resolve, before headline-places. It judges the claim WITH the
GeoNames place, which is how it can see that "Villarreal" landed on a hamlet in Badajoz. Placed before
resolve, it could not.

THE TWO CALLS.
- Call 2, VERIFY (prompts.VERIFY_SYSTEM). A fresh call reads the story text and the first answer's
  claims (the happening, the event place, the map place, the precision, the category). It never sees the
  first call's reasoning, and it is told the claim is sometimes wrong. It answers agree, disagree or
  unsure on each rule, and names the categories it accepts. Measured on 57 pins: a blind second reading
  repeated the same four mistakes; this judge stopped three of them.
- Call 3, ADJUDICATE, the critic (prompts.ADJUDICATE_SYSTEM). Only when call 2 flags a point. It reads the
  text, the claim and the objections, and decides: A (the pin stands), B (the objection stands) or
  unclear. At most one critic call per story (CRITIC_CALLS_MAX). There is no fourth call.

THE DECISION (Sam's go on the plan's recommendations, 5 Nov 2026).

  | Call 2               | Call 3      | Outcome            | Pin sent |
  |----------------------|-------------|--------------------|----------|
  | no flag              | not asked   | verified           | yes      |
  | a flag               | A           | kept_after_review  | yes      |
  | a flag               | B           | overruled          | no       |
  | a flag               | unclear     | split_dropped      | no       |
  | no usable answer     |             | not_run            | no       |

A flag is any rule not answered "agree" (unsure is a flag), or a category answer in which NO accepted
category can pin. Two categories that both fit are not an error: code decides what can pin.

FAILS CLOSED. Verification only removes pins; it never adds one. No budget, a 402, an error, or two bad
answers from one call (the answer and its one retry) mean no pin on this run, and the story waits for the
next run. A story whose calls answer badly on MAX_BAD_RUNS runs is given up, so it cannot cost money
every ten minutes. In a DeepSeek outage the map gets no new headline pins.

NEVER ASKED TWICE. One `verifications` row per story and (extract, resolve, verify) hash, holding the
claim, both answers, the flags and the outcome. A final outcome is never asked again: in thinking mode
DeepSeek ignores temperature, so a second asking could answer differently, and the first answer is the
one recorded. The row also keeps the hash of the exact message the verifier read; when the story text or
the claim changes, the old verdict no longer sends a pin and the story is checked again.

Every call writes its cost row in `llm_calls` (stages `verify` and `adjudicate`), so the shared daily
budget (NEWSFEED_DAILY_BUDGET_USD) sees it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import headline_places, prompts, taxonomy
from .config import (
    DEFAULT_MODEL,
    VERIFY_MAX_TOKENS,
    VERIFY_TEMPERATURE,
    extract_config_hash,
    gazetteer_resolve_hash,
    placement_config_hash,
    verify_config_hash,
)
from .deepseek import BalanceError, Client, FatalError, TransientError
from .extract import BudgetReached, feed_text
from .headline_places import Candidate
from .identity import now_utc
from .settings import Settings, SettingsError, load
from .store import Store

STAGE = "verify"
CRITIC_STAGE = "adjudicate"

VERIFIED = "verified"
KEPT = "kept_after_review"
OVERRULED = "overruled"
SPLIT = "split_dropped"
NOT_RUN = "not_run"
OUTCOMES = (VERIFIED, KEPT, OVERRULED, SPLIT, NOT_RUN)
PASSING = frozenset({VERIFIED, KEPT})
FINAL = frozenset({VERIFIED, KEPT, OVERRULED, SPLIT})

# The rules call 2 answers on, in the order the prompt names them. The category is the fifth, read apart.
RULE_POINTS = ("happened", "event_place", "map_place", "precision")
# The critic's decision to the outcome. "unclear" is a split, and a split is no pin.
DECISIONS = {"A": KEPT, "B": OVERRULED, "unclear": SPLIT}
CRITIC_CALLS_MAX = 1
# Runs that end in bad answers before a story is given up. A bad answer is rare (on 2026-11-05, 528
# stories took one retry between them and none failed twice), so two runs is enough to ride out a blip
# and few enough to cap the cost.
MAX_BAD_RUNS = 2
DEFAULT_LIMIT = 30
INPUT_HASH_LENGTH = 16


@dataclass(frozen=True)
class Hashes:
    extract: str
    resolve: str
    verify: str
    placement: str


def current_hashes(connection: Any, model: str = DEFAULT_MODEL) -> Hashes:
    """The four hashes of this configuration, the gazetteer's own build and names rule included."""
    extract = extract_config_hash(model)
    resolve = gazetteer_resolve_hash(connection)
    verify = verify_config_hash(model)
    return Hashes(extract, resolve, verify, placement_config_hash(extract, resolve, verify))


# --- Reading the answers -------------------------------------------------------------------------


def _load(content: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse_verify_answer(content: Any) -> dict[str, Any] | None:
    """Call 2's answer, or None when it is not the pin shape asked for. A missing rule is never read as
    agree: an answer without all four is a bad answer."""
    answer = _load(content)
    if answer is None or answer.get("kind") != "pin":
        return None
    for point in RULE_POINTS:
        part = answer.get(point)
        if not isinstance(part, dict) or not isinstance(part.get("verdict"), str):
            return None
    category = answer.get("category")
    if category is not None:
        if not isinstance(category, dict):
            return None
        acceptable = category.get("acceptable")
        if acceptable is not None and not isinstance(acceptable, list):
            return None
    return answer


def parse_critic_answer(content: Any) -> dict[str, Any] | None:
    """Call 3's answer with its decision as A, B or unclear, or None when it has no such decision."""
    answer = _load(content)
    if answer is None or not isinstance(answer.get("decision"), str):
        return None
    decision = {"a": "A", "b": "B", "unclear": "unclear"}.get(answer["decision"].strip().lower())
    if decision is None:
        return None
    return {**answer, "decision": decision}


def disputed(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """The points call 2 flagged. Any flag sends the story to the critic."""
    points: list[dict[str, Any]] = []
    for key in RULE_POINTS:
        part = answer.get(key) or {}
        if str(part.get("verdict") or "").strip().lower() != "agree":
            points.append({"point": key, "verdict": part.get("verdict"), "reason": part.get("reason"),
                           "correct_place": part.get("correct_place")})
    category = answer.get("category") or {}
    acceptable = [entry for entry in (category.get("acceptable") or []) if isinstance(entry, str)]
    # Round 1 dropped two right pins over society against politics. Flag the category only when no
    # category the verifier accepts can pin.
    if acceptable and not any(taxonomy.can_pin(entry) for entry in acceptable):
        points.append({"point": "category", "verdict": category.get("verdict"), "reason": category.get("reason"),
                       "acceptable": acceptable})
    return points


def decide(points: list[dict[str, Any]], critic: dict[str, Any] | None) -> str:
    """The decision table in the module docstring."""
    if not points:
        return VERIFIED
    if critic is None:
        return NOT_RUN
    return DECISIONS[critic["decision"]]


# --- The message ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """What call 2 reads for one candidate, and the hash that ties a verdict to it."""

    candidate: Candidate
    story: str
    claim: str
    message: str
    input_hash: str


def prepare(candidate: Candidate) -> Prepared:
    row = candidate.row
    story = prompts.verify_story_block(row.get("outlet"), row.get("published"), row.get("headline"), feed_text(row))
    named = {"name": row.get("place_name"), "within": row.get("place_within"), "country": row.get("place_country"),
             "kind": row.get("place_kind"), "quote": row.get("place_quote")}
    build = prompts.verify_fallback_claim if candidate.place.get("fallback") else prompts.verify_pin_claim
    claim = build(row.get("category"), named, candidate.place)
    message = prompts.build_verify_user(story, claim)
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:INPUT_HASH_LENGTH]
    return Prepared(candidate, story, claim, message, digest)


# --- The calls -----------------------------------------------------------------------------------


@dataclass
class Result:
    outcome: str = NOT_RUN
    answer: dict[str, Any] | None = None
    points: list[dict[str, Any]] = field(default_factory=list)
    critic: dict[str, Any] | None = None
    reason: str | None = None
    cost_usd: float = 0.0
    bad_answer: bool = False


class Stopped(Exception):
    """The stage must stop: no budget, a 402, or DeepSeek failing. `partial` is what this story got."""

    def __init__(self, message: str, partial: Result, exit_code: int) -> None:
        super().__init__(message)
        self.partial = partial
        self.exit_code = exit_code


class Checker:
    """Calls 2 and 3 for one story at a time. Every call is recorded in llm_calls, whatever it answered."""

    def __init__(self, store: Store, client: Any, budget_usd: float, verify_hash: str) -> None:
        self.store = store
        self.client = client
        self.budget_usd = budget_usd
        self.verify_hash = verify_hash
        self.calls = 0
        self.cost_usd = 0.0

    def ask(self, system: str, user: str, parse: Callable[[Any], dict[str, Any] | None], stage: str,
            story_id: str) -> tuple[dict[str, Any] | None, float]:
        """One call and its one retry. Returns the parsed answer, or None after two bad answers, and the
        cost. Raises BudgetReached before a call the budget does not cover."""
        cost = 0.0
        message = user
        for _ in range(2):
            if self.store.spend_today() >= self.budget_usd:
                raise BudgetReached(f"the day's budget of ${self.budget_usd:.2f} is used up")
            try:
                completion = self.client.complete(system, message, max_tokens=VERIFY_MAX_TOKENS,
                                                  temperature=VERIFY_TEMPERATURE)
            except (TransientError, FatalError) as error:
                self.store.record_call(stage, self.client.model, f"error: {type(error).__name__}",
                                       story_id=story_id, config_hash=self.verify_hash)
                raise
            self.calls += 1
            self.cost_usd += completion.cost_usd
            cost += completion.cost_usd
            parsed = parse(completion.content)
            note = " (answer cut off at max_tokens)" if completion.truncated else ""
            self.store.record_call(
                stage, completion.model, ("ok" if parsed is not None else "bad answer") + note,
                usage=completion.usage, cost_usd=completion.cost_usd, story_id=story_id,
                config_hash=self.verify_hash, latency_ms=completion.latency_ms, attempts=completion.attempts,
            )
            if parsed is not None:
                return parsed, cost
            message = user + prompts.VERIFY_RETRY_SUFFIX
        return None, cost

    def check(self, prepared: Prepared) -> Result:
        story_id = prepared.candidate.story_id
        result = Result()
        try:
            answer, cost = self.ask(prompts.VERIFY_SYSTEM, prepared.message, parse_verify_answer, STAGE, story_id)
            result.cost_usd += cost
            if answer is None:
                result.reason, result.bad_answer = "the verifier answered badly twice", True
                return result
            result.answer = answer
            result.points = disputed(answer)
            if not result.points:
                result.outcome = VERIFIED
                return result
            # The critic: at most one call per story (CRITIC_CALLS_MAX). Whatever it decides is final.
            critic_user = prompts.build_adjudicate_user(prepared.story, prepared.claim, result.points)
            critic, cost = self.ask(prompts.ADJUDICATE_SYSTEM, critic_user, parse_critic_answer, CRITIC_STAGE,
                                    story_id)
            result.cost_usd += cost
            if critic is None:
                result.reason, result.bad_answer = "the critic answered badly twice", True
                return result
            result.critic = critic
            result.outcome = decide(result.points, critic)
            result.reason = critic.get("reason")
            return result
        except BudgetReached as error:
            result.reason = f"no budget: {error}"
            raise Stopped(f"Stopped: {error}. Model stages resume at the next UTC day.", result, 0) from None
        except BalanceError as error:
            result.reason = f"402: {error}"
            raise Stopped(f"Stopped: {error}", result, 1) from None
        except TransientError as error:
            result.reason = f"DeepSeek failed: {error}"
            raise Stopped(f"Stopped: {error} The rest is left for the next run.", result, 0) from None
        except FatalError as error:
            result.reason = f"DeepSeek refused: {error}"
            raise Stopped(f"Stopped: {error} This is a bug in the request or the key, not a blip.", result, 1) from None


# --- The table -----------------------------------------------------------------------------------


def stored(store: Store, hashes: Hashes) -> dict[str, Any]:
    """Every verification row under these hashes, by story."""
    rows = store.query(
        "SELECT * FROM verifications WHERE extract_hash = ? AND resolve_hash = ? AND verify_hash = ?",
        (hashes.extract, hashes.resolve, hashes.verify),
    )
    return {row["story_id"]: row for row in rows}


def needs_check(row: Any, input_hash: str) -> bool:
    if row is None or row["input_hash"] != input_hash:
        return True
    if row["outcome"] in FINAL:
        return False
    return (row["attempts"] or 0) < MAX_BAD_RUNS


def record(store: Store, prepared: Prepared, hashes: Hashes, result: Result, previous: Any) -> None:
    now = now_utc()
    same_input = previous is not None and previous["input_hash"] == prepared.input_hash
    attempts = (previous["attempts"] or 0) if same_input else 0
    if result.bad_answer:
        attempts += 1
    place = prepared.candidate.place
    with store.write() as connection:
        connection.execute(
            """INSERT INTO verifications (story_id, extract_hash, resolve_hash, verify_hash, input_hash, geonames_id,
                                          place_precision, fallback, claim, verify_answer, flags, critic_answer,
                                          outcome, reason, attempts, cost_usd, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(story_id, extract_hash, resolve_hash, verify_hash) DO UPDATE SET
                   input_hash = excluded.input_hash, geonames_id = excluded.geonames_id,
                   place_precision = excluded.place_precision, fallback = excluded.fallback,
                   claim = excluded.claim, verify_answer = excluded.verify_answer, flags = excluded.flags,
                   critic_answer = excluded.critic_answer, outcome = excluded.outcome, reason = excluded.reason,
                   attempts = excluded.attempts, cost_usd = verifications.cost_usd + excluded.cost_usd,
                   updated_at = excluded.updated_at""",
            (
                prepared.candidate.story_id, hashes.extract, hashes.resolve, hashes.verify, prepared.input_hash,
                place.get("id"), place.get("precision"), 1 if place.get("fallback") else 0, prepared.claim,
                json.dumps(result.answer, ensure_ascii=False) if result.answer is not None else None,
                json.dumps(result.points, ensure_ascii=False),
                json.dumps(result.critic, ensure_ascii=False) if result.critic is not None else None,
                result.outcome, result.reason, attempts, result.cost_usd, now, now,
            ),
        )


def gate(store: Store, hashes: Hashes) -> Callable[[Candidate], bool]:
    """For headline-places: does this candidate pin pass? Only a verified or kept pin, under these
    hashes, on the same GeoNames place, for the same message. Anything else, a missing row included,
    is no pin."""
    rows = stored(store, hashes)

    def passes(candidate: Candidate) -> bool:
        row = rows.get(candidate.story_id)
        if row is None or row["outcome"] not in PASSING or row["geonames_id"] != candidate.place.get("id"):
            return False
        return row["input_hash"] == prepare(candidate).input_hash

    return passes


def outcome_counts(store: Store, days: int = 7) -> dict[str, dict[str, int]]:
    """Outcomes per UTC day for the last `days` days, by the day each verdict was last written. For
    `status` and health.json."""
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    counts: dict[str, dict[str, int]] = {}
    for row in store.query(
        """SELECT substr(updated_at, 1, 10) AS day, outcome, COUNT(*) AS n FROM verifications
            WHERE substr(updated_at, 1, 10) >= ? GROUP BY day, outcome ORDER BY day, outcome""",
        (since,),
    ):
        counts.setdefault(row["day"], {})[row["outcome"]] = row["n"]
    return counts


# --- The stage -----------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hours", type=int, default=headline_places.WINDOW_HOURS,
                        help="How far back to look for pins, the same window headline-places sends.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Most pins to check in one run.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="DeepSeek model id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Say how many pins wait for a check. Call nothing, write nothing.")


def run(args: argparse.Namespace, settings: Settings | None = None, client: Any = None) -> int:
    """Check the pins that wait. `client` is for tests, which must not call a paid API."""
    from .geonames import Gazetteer, names_rule_warning

    settings = settings or load()
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        gazetteer = Gazetteer.open(settings.geonames_db)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2

    exit_code = 0
    try:
        hashes = current_hashes(gazetteer.connection, args.model)
        warning = names_rule_warning(gazetteer.connection)
        if warning:
            print(warning, file=sys.stderr)
        with Store(settings.news_db) as store:
            rows = headline_places.placed_rows(store, hashes.extract, since)
            found = headline_places.candidates(rows, headline_places.gazetteer_placer(gazetteer))
            existing = stored(store, hashes)
            waiting: list[tuple[Prepared, Any]] = []
            for candidate in found:
                prepared = prepare(candidate)
                row = existing.get(candidate.story_id)
                if needs_check(row, prepared.input_hash):
                    waiting.append((prepared, row))
            todo = waiting[: max(0, args.limit)]
            print(
                f"verify {hashes.verify}  placement {hashes.placement}  {len(found)} pins in {args.hours} h, "
                f"{len(found) - len(waiting)} settled, {len(waiting)} waiting, {len(todo)} to check now  "
                f"spent today ${store.spend_today():.4f} of ${settings.daily_budget_usd:.2f}",
                file=sys.stderr,
            )
            if args.dry_run:
                return 0
            if not todo:
                print("Nothing to check.", file=sys.stderr)
                return 0

            if client is None:
                client = Client(settings.require("deepseek_api_key"), model=args.model)
            checker = Checker(store, client, settings.daily_budget_usd, hashes.verify)
            tally: Counter[str] = Counter()
            stopped: str | None = None
            for prepared, row in todo:
                try:
                    result = checker.check(prepared)
                except Stopped as stop:
                    record(store, prepared, hashes, stop.partial, row)
                    tally[NOT_RUN] += 1
                    stopped, exit_code = str(stop), stop.exit_code
                    break
                record(store, prepared, hashes, result, row)
                tally[result.outcome] += 1

            summary = ", ".join(f"{tally[outcome]} {outcome}" for outcome in OUTCOMES if tally[outcome])
            print(
                f"checked {sum(tally.values())}: {summary or 'none'}. {checker.calls} model calls, "
                f"${checker.cost_usd:.4f}",
                file=sys.stderr,
            )
            if stopped:
                print(stopped, file=sys.stderr)
    finally:
        gazetteer.close()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m newsfeed verify", description=__doc__)
    add_arguments(parser)
    try:
        return run(parser.parse_args(argv))
    except SettingsError as error:
        print(f"Settings: {error}", file=sys.stderr)
        return 2
