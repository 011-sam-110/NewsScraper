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

from typing import Any

from .taxonomy import (
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


def prompt_components() -> dict[str, Any]:
    """What the config hash records about the prompts (section 10.5)."""
    return {"extract_prompt_version": EXTRACT_PROMPT_VERSION, "extract_system": EXTRACT_SYSTEM}
