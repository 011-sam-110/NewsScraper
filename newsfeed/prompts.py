"""The prompts. Their exact text is part of the config hash (section 10.5).

Two rules shape how these are written:

- The system message is identical on every call and nothing that varies comes before the article,
  because DeepSeek caches a repeated prompt prefix and a cached input token costs a fraction of a
  fresh one (section 7.6).
- The model names a place and quotes the article. It never supplies a coordinate, and nothing it
  writes is ever shown. Code looks the place up in GeoNames, and a pin's title is the outlet's
  headline, verbatim (section 3).

Editing anything here changes the config hash, which withholds pins until the gate passes again.
"""

from __future__ import annotations

import json
from typing import Any

from .taxonomy import (
    CATEGORY_IDS,
    EVENT_DATE_DAYS_AFTER,
    EVENT_DATE_DAYS_BEFORE,
    NOT_EVENT_REASONS,
    PLACE_KINDS,
    category_lines,
)

EXTRACT_PROMPT_VERSION = "extract/1"

EXTRACT_SYSTEM = f"""You read one news story and return one json object. Return json only, with no
other text.

Your first job is to decide whether the story reports a physical happening at an identifiable
place: something a person standing there could have witnessed. Be strict. Most news is not this.

A physical happening: a stabbing, a shooting, a fire, a flood, an earthquake, a crash, a protest, a
raid, a strike on a building, a hearing held in a named court, a match played at a named ground.

Not a physical happening, whatever place it names:
- opinion, analysis, explainers, reviews, obituaries and profiles
- a policy, a law, a ruling, a sanction or an announcement, even when a capital city is named. A
  government announcing something in Paris is about France, not about Paris.
- market reports, company results, polls, interviews and roundups
- an event that is planned, threatened, expected, demanded or hypothetical, rather than one that
  happened
- an anniversary, a retrospective, or a verdict about an event outside the date window
- a summary of many separate events in different places

Set "is_physical_event" to false for any of those, give a "not_event_reason", and still choose a
category.

When it is a physical happening, name the single place where it happened, in "event_place":
- "name": the most precise place named in the story: a venue, a street, a district, or a city.
- "within": the larger place it sits in, usually the city or the region. Null if there is none.
- "country": the ISO 3166-1 alpha-2 code of the country it is in.
- "kind": one of {", ".join(PLACE_KINDS)}.
- "quote": a span copied from the article text, word for word, that both names the place and shows
  what happened there. It must contain "name" exactly as you wrote it. Copy it exactly, including
  punctuation. Do not correct, shorten or paraphrase it. If no single span in the text does both,
  set "is_physical_event" to false with the reason "no_place".

Never give a coordinate, a latitude or a longitude. Never use the place a reporter filed from as
the place of the event: a dateline says where the reporter was, not where the event happened.

The quote must show the happening AT the place. A quote that is verbatim and contains the place
name can still be the wrong quote. Two that are always wrong:

- Where somebody spoke to a reporter. "Yasser Salim told Reuters in Aleppo" says where an
  interview happened, not where anything happened. If the only span naming the place is an
  interview, a briefing or a dateline, set "is_physical_event" to false with the reason "no_place".
- Something that has not happened yet. "Alex Saab is scheduled to appear for a change of plea
  hearing in Miami" is a plan. So are "is due to", "is expected to", "will take place" and "plans
  to". A hearing that has not been held is not a happening, whatever the date says.

"event_date" is the date the happening took place, as YYYY-MM-DD, not the date it was reported. If
the story does not say, use the story's published date. It must fall between {EVENT_DATE_DAYS_BEFORE}
days before and {EVENT_DATE_DAYS_AFTER} day after the story's published date.

"category" must be exactly one id from this list:
{category_lines()}

"not_event_reason" must be null, or exactly one of: {", ".join(NOT_EVENT_REASONS)}.

"other_places": up to five other places the story names, as plain names. May be empty.
"key_entities": up to five organisations, agencies or named people central to the story. May be
empty. These are used to group reports of the same event, so prefer specific names.
"cluster_hint": one short line, at most 20 words, describing what happened, with any figures. It is
used only to match reports of the same event and is never shown to anyone.

Return exactly this shape:

{{"is_physical_event": true, "not_event_reason": null, "category": "attack_or_violent_crime",
"event_date": "2026-09-14", "event_place": {{"name": "Westminster", "within": "London",
"country": "GB", "kind": "district", "quote": "a man was stabbed near Westminster Bridge in central
London on Sunday"}}, "other_places": ["Manchester"], "key_entities": ["Metropolitan Police"],
"cluster_hint": "stabbing near Westminster Bridge, one man injured"}}

When "is_physical_event" is false, set "event_place" to null and "event_date" to the story's
published date.
"""


def build_user(
    outlet: str,
    section: str,
    headline: str | None,
    published: str | None,
    outlet_tags: list[str],
    text: str,
    max_text_characters: int = 12000,
) -> str:
    """The per-story message. Everything that varies is here, after the cached system prompt.

    The text is trimmed from the end, never the start: the lead paragraphs carry the place and the
    happening, and a quote has to be found in what the model was shown.
    """
    trimmed = text.strip()
    if len(trimmed) > max_text_characters:
        trimmed = trimmed[:max_text_characters].rsplit(" ", 1)[0]
    tags = ", ".join(outlet_tags[:20]) if outlet_tags else "none"
    return (
        f"Outlet: {outlet}\n"
        f"Section: {section}\n"
        f"Published: {published or 'unknown'}\n"
        f"Outlet place tags: {tags}\n"
        f"Headline: {headline or 'untitled'}\n"
        f"\nArticle text:\n{trimmed}\n"
    )


def retry_suffix(error: str) -> str:
    """Appended for the one schema retry (section 7.6). Says what was wrong, nothing more."""
    return (
        "\n\nYour previous answer was rejected: "
        f"{error}\n"
        "Return the same json shape again, corrected. Remember that the quote must be copied from "
        "the article text word for word and must contain the place name."
    )


CLUSTER_PROMPT_VERSION = "cluster/1"

CLUSTER_SYSTEM = """You compare two news reports and decide whether they report the SAME single
happening. Return json only, with no other text.

The same happening means one event: one fire, one crash, one raid, one match. Same place, same
time, same thing.

These are NOT the same happening, however similar they read:
- two separate crimes of the same type in the same city on the same day
- an event and a later, separate consequence of it: an arrest, a funeral, a court hearing, an
  inquiry, a report published about it
- a follow-up that adds a new, different incident to a running story
- one incident and a roundup that covers several incidents including it
- the same type of event in two places, however close

Answer "different" whenever you are weighing two plausible readings and one of them is "different".
Being wrong in the direction of "same" merges two real events into one, and nothing downstream can
tell that it was ever two. Being wrong in the direction of "different" only shows the event twice.

Use "unsure" when the two reports are too thin to tell apart, not as a midpoint between the two.
"unsure" is treated as "different", so it costs you nothing to use it honestly.

A shared place, a shared name, or a shared date is not evidence on its own. The question is whether
a person at the scene would have witnessed one thing or two.

Return exactly this shape:

{"verdict": "same", "reason": "both report the fire at the Dagenham tower block on 26 August"}

"verdict" is exactly one of: same, different, unsure.
"reason" is one short line, at most 20 words. It is stored, never shown to a reader.
"""


def build_cluster_user(first: dict[str, Any], second: dict[str, Any]) -> str:
    """The per-pair message. Section 7.8 step 2.

    Deliberately NOT the article text. The extract stage already reduced each story to a headline,
    a one-line summary, a place and the names in it, and that is what tells two events apart. Full
    text here would multiply the cost of the one stage that runs a call per candidate pair, and
    would bury the four fields that carry the answer.
    """

    def block(label: str, story: dict[str, Any]) -> str:
        entities = ", ".join(story.get("key_entities") or []) or "none"
        return (
            f"{label}:\n"
            f"  Headline: {story.get('headline') or 'untitled'}\n"
            f"  Published: {story.get('published') or 'unknown'}\n"
            f"  Event date: {story.get('event_date') or 'unknown'}\n"
            f"  Place: {story.get('place') or 'unknown'}\n"
            f"  What happened: {story.get('cluster_hint') or 'not summarised'}\n"
            f"  Names in the story: {entities}\n"
        )

    return f"{block('Report A', first)}\n{block('Report B', second)}"


def prompt_components() -> dict[str, Any]:
    """What the EXTRACT config hash records about the prompts (section 10.5).

    Extract only, and it must stay that way. `extractions` is keyed by (story_id, config_hash), so
    a key added here that describes some other stage throws away every stored extraction and pays
    for it again. The cluster prompt therefore has its own function below rather than joining this
    dict, for the same reason config.py keeps an extract hash apart from the gate hash.
    """
    return {"extract_prompt_version": EXTRACT_PROMPT_VERSION, "extract_system": EXTRACT_SYSTEM}


def cluster_prompt_components() -> dict[str, Any]:
    """What the CLUSTER config hash records about the prompts. Never mixed into the extract hash."""
    return {"cluster_prompt_version": CLUSTER_PROMPT_VERSION, "cluster_system": CLUSTER_SYSTEM}


# --- Verify (docs/ARCHITECTURE.md section 7.9) ------------------------------------------------
#
# Two fresh calls after resolve. VERIFY judges every claim of a headline pin, the GeoNames place
# included, and never sees the first call's reasoning. ADJUDICATE, the critic, runs only when VERIFY
# flags a point, sees the text and both answers, and decides once. The texts below are the round 2
# prompts measured on 5 Nov 2026 (57 pins, 200 non-pins, 14 venue fallbacks): every change from round 1
# came from a measured round 1 miss. The "no_pin" half of VERIFY_SYSTEM is for the weekly recall check
# (section 10.4); the daily stage sends only pin claims. Editing anything here moves the verify hash.

VERIFY_PROMPT_VERSION = "verify/1"

RULES = """The rules for a map pin. A story gets a pin only when ALL of these hold:
1. HAPPENED. The story reports a physical happening that took place: something a person standing
   there could have witnessed. Not a plan, a schedule, a threat, a demand, an expectation or a
   hypothesis. Not opinion, analysis, a policy, a ruling or an announcement. Not a roundup of
   several events in different places. A happening that an official, a police force or one side of
   a conflict reports counts as reported; it fails only when the story says it is doubtful, denied
   or did not take place.
   DATE. The happening is dated from 3 days before to 1 day after the published date. When the story
   gives no date for it, take the published date: a missing date is never a reason to object. Object
   on the date only when the text dates the happening outside the window ("last week", a weekday or
   a date more than 3 days before publication).
2. EVENT PLACE. The place is where the happening that makes the story news took place. Not a
   dateline (where the reporter filed from), not where somebody spoke or gave an interview, not where
   somebody travelled from or to, not the home of a person or a team. When the news is a happening
   somewhere else (a killing, a crash) and the named place is only where a later, smaller happening
   took place (a relative's departure, a press conference, a vigil), the place is wrong.
3. MAP PLACE. The map pin, a GeoNames row with its region and country, is the same real place the
   story means, not another place with the same or a similar name. Use the story's context: a club,
   a region, a nearby city, the size of the place. A hamlet with population 0 is rarely the place a
   news story means when a well-known town of that name exists. A city pin for a venue inside or at
   the edge of that city is the same place.
4. PRECISION. The pin is not more precise than the text supports. A venue pin needs the text to name
   that venue as the place. A city pin needs the text to name that city or a place inside it: when
   the text names only a country or a region, a city pin is too precise.
5. CATEGORY. Name the one or two categories that fit the story. Two categories that both fit are not
   an error.
Code applies two more rules, so do not argue them: courts_and_justice and opinion_analysis stories
never get a pin, and a region, a coast, a mountain range or a country is never a pin."""

VERIFY_SYSTEM = f"""You check a claim that another reader made about one news story. Return json only,
with no other text.

The other reader is sometimes wrong. Do not trust the claim. Read the story text yourself and test
each part of the claim against it. Use only the story text, its published date, and general
geographic knowledge (which town a football club plays in, which province a town is in, which
country a city is in). Do not use other knowledge of the news.

{RULES}

The claim is one of two kinds.

KIND "pin": the other reader says the story gets a pin. Check rules 1 to 4, and give the categories
for rule 5. For rules 1 to 4 answer "agree", "disagree" or "unsure". Use "unsure" only when the text
does not let you tell. When you disagree with the event place or the map place, give the place you
think is right, or null when the text names no place that passes the rules.

Return exactly this shape for KIND "pin":

{{"kind": "pin", "happened": {{"verdict": "agree", "reason": "a fire destroyed the market on Monday"}},
"event_place": {{"verdict": "agree", "reason": "the fire was at the market", "correct_place": null}},
"map_place": {{"verdict": "agree", "reason": "the town in that province", "correct_place": null}},
"precision": {{"verdict": "agree", "reason": "the story names the town"}},
"category": {{"acceptable": ["accident"], "reason": "an accidental fire"}}}}

KIND "no_pin": the other reader says the story gets no pin, and says why. Decide whether the story
does in fact report a happening that passes rules 1 and 2, at a place the text names at venue,
street, district or city precision, in a category code allows. Answer "agree" (no pin), "disagree"
(there should be a pin) or "unsure". When you disagree, give the place and a quote copied word for
word from the story text that names the place and shows what happened there.

Return exactly this shape for KIND "no_pin":

{{"kind": "no_pin", "verdict": "agree", "reason": "a policy announcement, nothing happened at a place",
"place": null}}

or, when you disagree:

{{"kind": "no_pin", "verdict": "disagree", "reason": "a bus crash in the town on Sunday",
"place": {{"name": "Kostiantynivka", "within": "Donetsk Oblast", "country": "UA", "kind": "city",
"quote": "a drone hit a bus in Kostiantynivka on Sunday"}}}}

Every "reason" is one short line, at most 25 words.
"""

ADJUDICATE_SYSTEM = f"""You settle a disagreement between two readers of one news story. Return json only,
with no other text.

Reader A made a claim about the story. Reader B checked the claim and objected to part of it, or gave
a different answer. Either reader may be wrong, and neither is more likely to be right. An objection
is not evidence, and neither is a claim. Read the story text yourself. For each disputed point, find
the words in the text that settle it, and side with the reader those words support. When the text
supports neither, answer "unclear". Use only the story text, its published date, and general
geographic knowledge. Do not use other knowledge of the news.

{RULES}

For each disputed point give "side": "A", "B" or "unclear", and "evidence": the few words of the text
that settle it (or "" when nothing does). Then give one decision about the map:
- "A": reader A's answer about the map stands (A's pin, or A's "no pin").
- "B": reader B is right (B's "no pin", or B's different answer).
- "unclear": the text does not settle it.

Return exactly this shape:

{{"points": [{{"point": "event_place", "side": "B", "evidence": "departed Ghana for the United States",
"reason": "the airport is where the mother left from; the killing was in the United States"}}],
"decision": "B", "reason": "the pinned place is not where the reported happening took place"}}

Every "reason" is one short line, at most 25 words.
"""

# What GeoNames calls a feature, in words the verifier reads beside the map pin.
FEATURE_NAMES = {
    "PPL": "populated place", "PPLA": "seat of a first-order admin area", "PPLA2": "seat of a second-order admin area",
    "PPLA3": "seat of a third-order admin area", "PPLA4": "seat of a fourth-order admin area", "PPLC": "capital city",
    "PPLX": "section of a populated place", "PPLL": "locality", "AIRP": "airport", "ADM1": "first-order admin area",
    "ADM2": "second-order admin area", "ADM3": "third-order admin area", "ADM4": "fourth-order admin area",
}

CATEGORIES_LINE = "Categories: " + ", ".join(CATEGORY_IDS)

VERIFY_RETRY_SUFFIX = (
    "\n\nYour previous answer was not valid json in the shape asked for. Return it again, as json only."
)


def verify_story_block(outlet: str | None, published: str | None, headline: str | None, text: str) -> str:
    """The story as both verify calls see it: a feed row's headline and RSS summary, and its date."""
    return (
        f"Outlet: {outlet}\n"
        f"Published: {published or 'unknown'}\n"
        f"Headline: {headline or 'untitled'}\n"
        f"\nStory text:\n{text}\n"
    )


def _claim_head(category: str | None, place: dict[str, Any]) -> list[str]:
    return [
        "CLAIM, KIND \"pin\":",
        "- Physical happening: yes",
        f"- Category: {category}",
        f"- Event place as the reader named it: \"{place.get('name')}\" (within: {place.get('within')}, "
        f"country: {place.get('country')}, kind: {place.get('kind')})",
        f"- Evidence quote the reader gave: \"{place.get('quote')}\"",
    ]


def verify_pin_claim(category: str | None, place: dict[str, Any], pin: dict[str, Any]) -> str:
    """The first answer's claims, with the GeoNames place it resolved to. `place` is what the reader
    named (name, within, country, kind, quote); `pin` is the map pin (name, lat, lon, precision,
    feature_code, population)."""
    feature = FEATURE_NAMES.get(pin["feature_code"], pin["feature_code"])
    return "\n".join(_claim_head(category, place) + [
        f"- Map pin: {pin.get('name')}. GeoNames {feature}, population {pin['population']}, at {pin.get('lat')}, "
        f"{pin.get('lon')}. Pin precision: {pin.get('precision')}.",
    ])


def verify_fallback_claim(category: str | None, place: dict[str, Any], pin: dict[str, Any]) -> str:
    """A pin claim for a venue GeoNames lacks, placed on the city the reader gave as "within"."""
    feature = FEATURE_NAMES.get(pin["feature_code"], pin["feature_code"])
    return "\n".join(_claim_head(category, place) + [
        f"- Map pin: {pin['name']}. The venue is not in GeoNames, so the pin is the place the reader gave as "
        f"\"within\": GeoNames {feature}, population "
        f"{pin['population']}, at {pin['lat']}, {pin['lon']}. Pin precision: area (a town or district).",
    ])


def build_verify_user(story: str, claim: str) -> str:
    return story + "\n" + claim + "\n" + CATEGORIES_LINE


def objection_text(points: list[dict[str, Any]]) -> str:
    """The verifier's flags, as the critic reads them: reader B's objections."""
    lines = ["READER B (objections to the claim):"]
    for point in points:
        extra = ""
        if point.get("correct_place"):
            extra = f" B's place: {json.dumps(point['correct_place'], ensure_ascii=False)}."
        if point.get("acceptable"):
            extra = f" B's acceptable categories: {', '.join(point['acceptable'])}."
        lines.append(f"- {point['point']}: {point.get('verdict')}. {point.get('reason') or ''}{extra}")
    return "\n".join(lines)


def build_adjudicate_user(story: str, claim: str, points: list[dict[str, Any]]) -> str:
    return story + "\nREADER A:\n" + claim + "\n\n" + objection_text(points) + "\n\n" + CATEGORIES_LINE


# One rendered example of each message, so an edit to a template moves the verify hash as surely as an
# edit to a system prompt does. Made-up values: nothing here is a real story.
_EXAMPLE_PLACE = {"name": "Example Market", "within": "Example City", "country": "GB", "kind": "venue",
                  "quote": "a fire destroyed Example Market on Monday"}
_EXAMPLE_PIN = {"name": "Example City, England, United Kingdom", "lat": 51.5, "lon": -0.1, "precision": "area",
                "feature_code": "PPL", "population": 1000}
_EXAMPLE_POINTS = [{"point": "map_place", "verdict": "disagree", "reason": "a namesake",
                    "correct_place": {"name": "Example City", "within": "Example County", "country": "GB",
                                      "kind": "city"}},
                   {"point": "category", "verdict": None, "reason": "a court story", "acceptable": ["accident"]}]


def verify_prompt_components() -> dict[str, Any]:
    """What the VERIFY config hash records about the prompts. Never mixed into the extract hash."""
    story = verify_story_block("example-outlet", "2026-11-05T09:00:00Z", "Example headline", "Example text.")
    pin_claim = verify_pin_claim("accident", _EXAMPLE_PLACE, _EXAMPLE_PIN)
    return {
        "verify_prompt_version": VERIFY_PROMPT_VERSION,
        "verify_system": VERIFY_SYSTEM,
        "adjudicate_system": ADJUDICATE_SYSTEM,
        "verify_retry_suffix": VERIFY_RETRY_SUFFIX,
        "pin_claim_example": build_verify_user(story, pin_claim),
        "fallback_claim_example": build_verify_user(story, verify_fallback_claim("accident", _EXAMPLE_PLACE, _EXAMPLE_PIN)),
        "adjudicate_example": build_adjudicate_user(story, pin_claim, _EXAMPLE_POINTS),
    }
