"""Stage: extract. One DeepSeek call per story, newest first, checked before it is accepted.

Section 7.6. The model reads a story and says whether it reports a physical happening at a place,
which category it is, when it happened, and which place, with a quote from the text. Code then
checks the answer against the article before accepting it, because the model is the least reliable
part of the pipeline and an unchecked extraction is how GDELT ended up 76.5% wrong.

The checks, in order:

1. Schema. Valid JSON in the right shape, with the category on the closed list.
2. Quote. With whitespace normalised, the quote is a verbatim substring of the article text, and
   the place name appears inside the quote.
3. Evidence. The quote shows the happening at the place, rather than merely naming it. A verbatim
   quote containing the place name can still be an interview location or a hearing that has not
   been held, and check 2 passes both. Section 13 found each at about 1 to 1.5% of pinnable
   extractions, against an error budget of 3 wrong in 153.
4. Date. The event date falls between the published time minus 3 days and plus 1 day.
5. Country veto. An outlet's own place tags can veto a place but never supply one. Not enforced
   yet: it needs GeoNames to turn a tag like "Nigeria" into a country code, which is M6. The check
   records itself as skipped rather than quietly passing.
6. Format. A story with a format flag, or with no text, is not a physical event whatever the model
   said.

A failed schema check gets exactly one retry with the error appended. Any other failure is stored
with its reason and the story goes to World news. Neither costs the story its place in the queue:
it moves to `extracted` either way, so it is not paid for again.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import prompts
from .config import EXTRACT_MAX_TOKENS, EXTRACT_TEMPERATURE, extract_config_hash
from .deepseek import (
    DEFAULT_MODEL,
    BalanceError,
    Client,
    Completion,
    FatalError,
    TransientError,
)
from .identity import normalise_text, now_utc
from .settings import Settings, SettingsError, load
from .store import STATUS_EXTRACTED, STATUS_SCRAPED, Store
from .taxonomy import (
    CATEGORY_IDS,
    EVENT_DATE_DAYS_AFTER,
    EVENT_DATE_DAYS_BEFORE,
    NOT_EVENT_REASONS,
    PLACE_KINDS,
)

STAGE = "extract"

# Where each outlet keeps its own place and topic labels, inside stories.categories. These can veto
# a place the model names, never supply one (section 6.1). The Reuters "place" field is deliberately
# absent: it is a dateline, which says where the reporter filed from.
OUTLET_TAG_FIELDS: dict[str, tuple[str, ...]] = {
    "reuters": ("topics", "keywords"),
    "bbc": ("listing_topics", "topics"),
    "guardian": ("keywords",),
    "pbs": ("tags", "topics"),
    "nyt": ("places",),
}


class BudgetReached(RuntimeError):
    """The day's model spend has hit NEWSFEED_DAILY_BUDGET_USD. Model stages stop until tomorrow."""


@dataclass
class Checked:
    """One model answer after the checks have run."""

    accepted: bool
    reason: str | None
    payload: dict[str, Any]
    checks: dict[str, str] = field(default_factory=dict)


def outlet_place_tags(outlet: str, categories: Any) -> list[str]:
    """The outlet's own place and topic labels for a story, flattened to plain strings."""
    if not isinstance(categories, dict):
        return []
    found: list[str] = []
    for name in OUTLET_TAG_FIELDS.get(outlet, ()):
        value = categories.get(name)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    found.append(entry)
                elif isinstance(entry, dict):
                    label = entry.get("name") or entry.get("title") or entry.get("short_bio")
                    if isinstance(label, str):
                        found.append(label)
    cleaned = [tag.strip() for tag in found if tag and tag.strip()]
    return list(dict.fromkeys(cleaned))


# --- The checks -----------------------------------------------------------------------


def check_schema(payload: Any) -> str | None:
    """The shape of section 7.6, and the closed lists. Returns an error message, or None."""
    if not isinstance(payload, dict):
        return "the answer was not a json object"
    if not isinstance(payload.get("is_physical_event"), bool):
        return "is_physical_event must be true or false"
    category = payload.get("category")
    if category not in CATEGORY_IDS:
        return f"category must be one of the listed ids, not {category!r}"
    reason = payload.get("not_event_reason")
    if reason is not None and reason not in NOT_EVENT_REASONS:
        return f"not_event_reason must be null or one of the listed reasons, not {reason!r}"

    date = payload.get("event_date")
    if not isinstance(date, str) or not _parse_date(date):
        return f"event_date must be a YYYY-MM-DD date, not {date!r}"

    place = payload.get("event_place")
    if payload["is_physical_event"]:
        if not isinstance(place, dict):
            return "event_place must be an object when is_physical_event is true"
        for name in ("name", "quote", "country", "kind"):
            if not isinstance(place.get(name), str) or not place[name].strip():
                return f"event_place.{name} must be a non-empty string"
        if place["kind"] not in PLACE_KINDS:
            return f"event_place.kind must be one of the listed kinds, not {place['kind']!r}"
        country = place["country"].strip()
        if len(country) != 2 or not country.isalpha():
            return f"event_place.country must be a two-letter country code, not {country!r}"
        within = place.get("within")
        if within is not None and not isinstance(within, str):
            return "event_place.within must be a string or null"
    elif place not in (None, {}):
        return "event_place must be null when is_physical_event is false"

    for name in ("other_places", "key_entities"):
        value = payload.get(name, [])
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
            return f"{name} must be a list of strings"
    hint = payload.get("cluster_hint")
    if hint is not None and not isinstance(hint, str):
        return "cluster_hint must be a string or null"
    return None


def check_quote(payload: dict[str, Any], text: str | None) -> str | None:
    """The quote is in the article, word for word, and the place name is inside the quote.

    Whitespace is normalised on both sides first, because outlets reflow paragraphs and the model
    copies what it was shown. Nothing else is normalised: a quote that had to be corrected to match
    is not a verbatim quote.
    """
    place = payload.get("event_place") or {}
    quote = normalise_text(place.get("quote", ""))
    name = normalise_text(place.get("name", ""))
    if not quote:
        return "the quote was empty"
    if not text or not text.strip():
        return "the story has no text, so no quote can be verified"
    if quote not in normalise_text(text):
        return "the quote is not in the article text word for word"
    if name.casefold() not in quote.casefold():
        return "the place name is not inside the quote"
    return None


# A reporter being spoken to somewhere. Section 6.1: a dateline says where the reporter was, not
# where the event happened, and the same is true of an interview.
ATTRIBUTION_RE = re.compile(
    r"\b(told|tells|spoke to|speaking to|said to|talking to)\s+(the\s+)?"
    r"(Reuters|BBC|Guardian|PBS|New York Times|NYT|AP|AFP|CNN|reporters|journalists|"
    r"our correspondent)\b",
    re.IGNORECASE,
)

# The place immediately after the attribution, which is what makes the quote about the interview
# rather than about the event.
ATTRIBUTION_PLACE_RE = r"\s*,?\s*(in|at|from|near|outside)\s+"

# An event that has not happened. Section 10.4 counts planned, threatened and hypothetical as wrong.
PLANNED_RE = re.compile(
    r"\b(is|are|was|were)\s+(scheduled|due|expected|set|slated)\s+to\b"
    r"|\bwill\s+(appear|take place|be held|go on trial|face)\b"
    r"|\bplanned for\b|\bplans to\b|\bis to (appear|stand trial|be held)\b",
    re.IGNORECASE,
)


def check_evidence(payload: dict[str, Any]) -> str | None:
    """The quote shows the event happening at the place, not something else that names it.

    Section 13 found both of these by reading real output, each at about 1 to 1.5% of pinnable
    extractions, against an error budget of 3 wrong in 153. check_quote proves a quote is real and
    contains the place name. Neither of these failures breaks that, which is the point: they are
    verbatim quotes containing the place name that are still not evidence.

    1. "Yasser Salim told Reuters in Aleppo." That is where a person spoke to a reporter.
    2. "Alex Saab is scheduled to appear ... in Miami." That is a hearing that has not happened.

    The attribution test requires the place to sit immediately AFTER the attribution. A quote that
    names the place first and attributes afterwards is ordinary reporting and must survive:
    "Iranian strikes damaged aircraft on the Muwaffaq Salti Air Base in Jordan, a U.S. official told
    Reuters" is real evidence, and an attribution test that only looked for `told Reuters` anywhere
    in the quote would throw it away. Measured on 172 real pinnable extractions: the narrow test
    catches the one bad quote and keeps that one.
    """
    place = payload.get("event_place") or {}
    quote = normalise_text(place.get("quote", ""))
    name = normalise_text(place.get("name", ""))
    if not quote or not name:
        return None

    for match in ATTRIBUTION_RE.finditer(quote):
        following = quote[match.end() : match.end() + 40]
        if re.match(ATTRIBUTION_PLACE_RE + re.escape(name), following, re.IGNORECASE):
            return "the quote says where someone spoke to a reporter, not where the event happened"

    if PLANNED_RE.search(quote):
        return "the quote describes an event that is planned rather than one that happened"
    return None


def check_date(payload: dict[str, Any], published: str | None) -> str | None:
    """The event date sits in the window around the story's published time (section 6.1)."""
    event = _parse_date(payload.get("event_date"))
    if event is None:
        return "event_date is not a date"
    if not published:
        return None  # No published time to measure against; the date rule cannot be applied.
    when = _parse_timestamp(published)
    if when is None:
        return None
    earliest = (when - timedelta(days=EVENT_DATE_DAYS_BEFORE)).date()
    latest = (when + timedelta(days=EVENT_DATE_DAYS_AFTER)).date()
    if not earliest <= event <= latest:
        return f"event_date {event.isoformat()} is outside {earliest.isoformat()} to {latest.isoformat()}"
    return None


def check_format(payload: dict[str, Any], format_flags: list[str], text: str | None) -> str | None:
    """A flagged page or a story with no text is not a physical event, whatever the model said."""
    if format_flags:
        return f"the outlet marks this as {', '.join(format_flags)}"
    if not text or not text.strip():
        return "the story has no article text"
    return None


def _parse_date(value: Any):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_timestamp(value: str):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def apply_checks(
    payload: dict[str, Any], text: str | None, published: str | None, format_flags: list[str]
) -> Checked:
    """Run every check in order and return the answer as it should be stored.

    A failure never throws the answer away. The category is still worth keeping, so the story
    becomes a World news item with the reason recorded rather than vanishing.
    """
    checks: dict[str, str] = {"schema": "passed"}
    # The country veto needs a way to turn an outlet tag into a country code, which arrives with
    # GeoNames at M6. Saying so is better than recording a pass that never ran.
    checks["country_veto"] = "skipped: needs the GeoNames country table (M6)"

    format_failure = check_format(payload, format_flags, text)
    checks["format"] = "passed" if format_failure is None else f"failed: {format_failure}"

    if not payload.get("is_physical_event"):
        checks["quote"] = "not applicable: not a physical event"
        checks["date"] = "passed" if check_date(payload, published) is None else "failed"
        return Checked(True, None, payload, checks)

    if format_failure is not None:
        # Section 7.6 check 5: override the model rather than reject it.
        payload = dict(payload)
        payload["is_physical_event"] = False
        payload["not_event_reason"] = payload.get("not_event_reason") or "other"
        payload["event_place"] = None
        checks["quote"] = "not applicable: overridden by the format check"
        checks["date"] = "not applicable: overridden by the format check"
        return Checked(True, format_failure, payload, checks)

    quote_failure = check_quote(payload, text)
    checks["quote"] = "passed" if quote_failure is None else f"failed: {quote_failure}"
    # Only worth asking once the quote is known to be real and to name the place: this check reads
    # the quote as evidence, and a quote that failed above is not evidence of anything.
    evidence_failure = None if quote_failure else check_evidence(payload)
    checks["evidence"] = (
        "not applicable: the quote check failed"
        if quote_failure
        else ("passed" if evidence_failure is None else f"failed: {evidence_failure}")
    )
    date_failure = check_date(payload, published)
    checks["date"] = "passed" if date_failure is None else f"failed: {date_failure}"

    failure = quote_failure or evidence_failure or date_failure
    if failure is not None:
        payload = dict(payload)
        payload["is_physical_event"] = False
        payload["not_event_reason"] = payload.get("not_event_reason") or "other"
        payload["event_place"] = None
        return Checked(True, failure, payload, checks)
    return Checked(True, None, payload, checks)


# --- The stage ------------------------------------------------------------------------


@dataclass
class Outcome:
    story_id: str
    accepted: bool
    is_event: bool
    category: str | None
    reason: str | None
    cost_usd: float


def load_story(store: Store, story_id: str) -> dict[str, Any] | None:
    """Everything the prompt and the checks need for one story, in one read."""
    row = store.one(
        """SELECT s.*, t.text AS text
             FROM stories s LEFT JOIN story_texts t ON t.story_id = s.story_id
            WHERE s.story_id = ?""",
        (story_id,),
    )
    if row is None:
        return None
    story = dict(row)
    story["categories"] = json.loads(story["categories"] or "{}")
    story["format_flags"] = json.loads(story["format_flags"] or "[]")
    section = store.one(
        "SELECT section FROM story_sections WHERE story_id = ? ORDER BY first_seen_at LIMIT 1",
        (story_id,),
    )
    story["section"] = section["section"] if section else ""
    return story


def ask_model(client: Client, story: dict[str, Any], tags: list[str]) -> tuple[Completion, dict[str, Any], str | None, bool]:
    """One call, plus the one schema retry section 7.6 allows.

    Returns the completion, the parsed payload, a schema error that survived the retry, and whether
    the retry was used. The retry is spent only on a schema failure, never on a transport failure.
    """
    user = prompts.build_user(
        outlet=story["outlet"],
        section=story["section"],
        headline=story["headline"],
        published=story["published"],
        outlet_tags=tags,
        text=story["text"] or "",
    )
    completion = client.complete(
        prompts.EXTRACT_SYSTEM, user, max_tokens=EXTRACT_MAX_TOKENS, temperature=EXTRACT_TEMPERATURE
    )
    payload, error = _parse(completion)
    if error is None:
        return completion, payload, None, False

    retry = client.complete(
        prompts.EXTRACT_SYSTEM,
        user + prompts.retry_suffix(error),
        max_tokens=EXTRACT_MAX_TOKENS,
        temperature=EXTRACT_TEMPERATURE,
    )
    payload, error = _parse(retry)
    return retry, payload, error, True


def _parse(completion: Completion) -> tuple[dict[str, Any], str | None]:
    try:
        payload = json.loads(completion.content)
    except json.JSONDecodeError as error:
        hint = " The answer was cut off at max_tokens." if completion.truncated else ""
        return {}, f"the answer was not valid json ({error.msg}).{hint}"
    schema_error = check_schema(payload)
    return (payload if isinstance(payload, dict) else {}), schema_error


def store_extraction(
    store: Store,
    story: dict[str, Any],
    config_hash: str,
    checked: Checked,
    completion: Completion,
    tags: list[str],
    schema_retried: bool,
) -> None:
    """Write the extraction and move the story on, in one transaction."""
    payload = checked.payload
    place = payload.get("event_place") or {}
    with store.write() as connection:
        connection.execute(
            """INSERT INTO extractions (
                   story_id, config_hash, text_hash, accepted, failure_reason, is_physical_event,
                   not_event_reason, category, event_date, place_name, place_within, place_country,
                   place_kind, place_quote, other_places, key_entities, cluster_hint, raw, model,
                   schema_retried, outlet_tags, checks, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(story_id, config_hash) DO UPDATE SET
                   text_hash = excluded.text_hash, accepted = excluded.accepted,
                   failure_reason = excluded.failure_reason,
                   is_physical_event = excluded.is_physical_event,
                   not_event_reason = excluded.not_event_reason, category = excluded.category,
                   event_date = excluded.event_date, place_name = excluded.place_name,
                   place_within = excluded.place_within, place_country = excluded.place_country,
                   place_kind = excluded.place_kind, place_quote = excluded.place_quote,
                   other_places = excluded.other_places, key_entities = excluded.key_entities,
                   cluster_hint = excluded.cluster_hint, raw = excluded.raw, model = excluded.model,
                   schema_retried = excluded.schema_retried, outlet_tags = excluded.outlet_tags,
                   checks = excluded.checks, created_at = excluded.created_at""",
            (
                story["story_id"], config_hash, story["text_hash"],
                1 if checked.accepted else 0, checked.reason,
                1 if payload.get("is_physical_event") else 0,
                payload.get("not_event_reason"), payload.get("category"), payload.get("event_date"),
                place.get("name"), place.get("within"), place.get("country"), place.get("kind"),
                place.get("quote"),
                json.dumps(payload.get("other_places") or [], ensure_ascii=False),
                json.dumps(payload.get("key_entities") or [], ensure_ascii=False),
                payload.get("cluster_hint"),
                completion.content, completion.model, 1 if schema_retried else 0,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(checked.checks, ensure_ascii=False),
                now_utc(),
            ),
        )
        connection.execute(
            "UPDATE stories SET status = ?, lease_owner = NULL, lease_until = NULL WHERE story_id = ?",
            (STATUS_EXTRACTED, story["story_id"]),
        )


def extract_one(store: Store, client: Client, story_id: str, config_hash: str) -> Outcome | None:
    """One story, end to end. Raises TransientError, FatalError or BalanceError upward."""
    story = load_story(store, story_id)
    if story is None:
        return None
    tags = outlet_place_tags(story["outlet"], story["categories"])

    try:
        completion, payload, schema_error, retried = ask_model(client, story, tags)
    except (TransientError, FatalError) as error:
        store.record_call(STAGE, client.model, f"error: {type(error).__name__}", story_id=story_id,
                          config_hash=config_hash)
        raise

    store.record_call(
        STAGE, completion.model, "ok", usage=completion.usage, cost_usd=completion.cost_usd,
        story_id=story_id, config_hash=config_hash, latency_ms=completion.latency_ms,
        attempts=completion.attempts,
    )

    if schema_error is not None:
        # The one retry did not fix it. Keep what came back so the failure can be read later.
        checked = Checked(False, f"schema: {schema_error}", {"is_physical_event": False},
                          {"schema": f"failed: {schema_error}"})
    else:
        checked = apply_checks(payload, story["text"], story["published"], story["format_flags"])

    store_extraction(store, story, config_hash, checked, completion, tags, retried)
    return Outcome(
        story_id=story_id,
        accepted=checked.accepted,
        is_event=bool(checked.payload.get("is_physical_event")),
        category=checked.payload.get("category"),
        reason=checked.reason,
        cost_usd=completion.cost_usd,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=200, help="Most stories to extract in one run.")
    parser.add_argument("--story", help="Extract one story by id, whatever its status. For debugging.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="DeepSeek model id.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Say what would be extracted, and what the store already holds, without calling the model.",
    )
    parser.add_argument(
        "--redo", action="store_true",
        help="Also extract stories already marked extracted that have no answer under the current "
             "config hash. This is what re-runs a backlog after a prompt change, and it costs the "
             "same as extracting those stories the first time.",
    )


def run(args: argparse.Namespace, settings: Settings | None = None) -> int:
    settings = settings or load()
    settings.ensure_data_dir()
    config_hash = extract_config_hash(args.model)

    with Store(settings.news_db) as store:
        if args.redo:
            waiting = store.scalar(
                """SELECT COUNT(*) FROM stories s
                   WHERE s.text_hash IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM extractions e
                                     WHERE e.story_id = s.story_id AND e.config_hash = ?)""",
                (config_hash,),
            )
        else:
            waiting = store.scalar("SELECT COUNT(*) FROM stories WHERE status = ?", (STATUS_SCRAPED,))
        done = store.scalar(
            "SELECT COUNT(*) FROM extractions WHERE config_hash = ?", (config_hash,)
        )
        spent = store.spend_today()
        print(
            f"config {config_hash}  model {args.model}  waiting {waiting}  "
            f"already extracted {done}  spent today ${spent:.4f} of ${settings.daily_budget_usd:.2f}",
            file=sys.stderr,
        )
        if args.dry_run:
            return 0

        key = settings.require("deepseek_api_key")
        client = Client(key, model=args.model)
        if args.story:
            claimed = [args.story]
        elif args.redo:
            claimed = store.claim_missing_extractions(config_hash, args.limit)
        else:
            claimed = store.claim_stories(STATUS_SCRAPED, args.limit)
        if not claimed:
            print("Nothing waiting.", file=sys.stderr)
            return 0

        outcomes: list[Outcome] = []
        stopped: str | None = None
        exit_code = 0
        try:
            for story_id in claimed:
                if store.spend_today() >= settings.daily_budget_usd:
                    raise BudgetReached(
                        f"the day's budget of ${settings.daily_budget_usd:.2f} is used up"
                    )
                outcome = extract_one(store, client, story_id, config_hash)
                if outcome is not None:
                    outcomes.append(outcome)
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
        finally:
            done_ids = {outcome.story_id for outcome in outcomes}
            store.release_stories([sid for sid in claimed if sid not in done_ids])

        events = sum(1 for outcome in outcomes if outcome.is_event)
        failed = [outcome for outcome in outcomes if outcome.reason]
        cost = sum(outcome.cost_usd for outcome in outcomes)
        print(
            f"extracted {len(outcomes)} of {len(claimed)} claimed: {events} physical events, "
            f"{len(outcomes) - events} World news, {len(failed)} failed a check. "
            f"Cost ${cost:.4f}"
            + (f" (${cost / len(outcomes):.6f} a story)" if outcomes else ""),
            file=sys.stderr,
        )
        if stopped:
            print(stopped, file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m newsfeed extract", description=__doc__)
    add_arguments(parser)
    try:
        return run(parser.parse_args(argv))
    except SettingsError as error:
        print(f"Settings: {error}", file=sys.stderr)
        return 2
