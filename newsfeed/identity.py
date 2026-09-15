"""Story identity, UTC times and format flags: the rules that turn a scraped row into a store row.

Sections 7.4 and 7.5 of docs/ARCHITECTURE.md. Three rules matter here:

- A row joins the story that any of its aliases already points to, so a story listed by several
  sections, or seen first in a listing and later on its article page, stays one story.
- Times are stored as UTC ISO 8601. A value that will not parse is stored as null with the raw
  value kept beside it, so a parser change can fix it later without another scrape.
- Format flags come from outlet metadata only, never from a model and never from the text. A
  flagged story still gets a category later, but it can never be a pin.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

OUTLETS = ("reuters", "bbc", "guardian", "pbs", "nyt")

# Each outlet's ids, in the order docs/ARCHITECTURE.md section 7.4 lists them. The canonical URL is
# appended to every outlet's list, so any two rows for the same story always share at least one
# alias even when the outlet leaves its own id out of a listing.
ALIAS_FIELDS: dict[str, tuple[str, ...]] = {
    "reuters": ("id",),
    "bbc": ("id",),
    "guardian": ("short_url", "id"),
    "pbs": ("id", "post_id"),
    "nyt": ("guid",),
}

# Reuters N2 subject codes that mark copy which is not a report of a happening.
REUTERS_N2_FLAGS = {
    "ANLINS": "analysis",
    "MKTREP": "market_report",
    "EXPLN": "explainer",
    "FBOX": "factbox",
}
# The Guardian's own Tone tags.
GUARDIAN_TONE_FLAGS = {
    "comment": "opinion",
    "letters": "opinion",
    "editorials": "opinion",
    "analysis": "analysis",
    "explainers": "explainer",
    "reviews": "review",
    "obituaries": "obituary",
    "matchreports": "sport_report",
    "minutebyminute": "live",
    "timelines": "roundup",
    "podcast": "podcast",
    "advertisement features": "advertisement",
}
# The Guardian's page design, which says what kind of page it is.
GUARDIAN_DESIGN_FLAGS = {
    "liveblogdesign": "live",
    "deadblogdesign": "live",
    "videodesign": "video",
    "audiodesign": "audio",
    "gallerydesign": "gallery",
    "picturedesign": "gallery",
    "interactivedesign": "interactive",
    "fullpageinteractivedesign": "interactive",
    "commentdesign": "opinion",
    "letterdesign": "opinion",
    "editorialdesign": "opinion",
    "obituarydesign": "obituary",
    "reviewdesign": "review",
    "explainerdesign": "explainer",
    "timelinedesign": "roundup",
    "profiledesign": "profile",
    "quizdesign": "quiz",
    "newslettersignupdesign": "newsletter",
    "crosswordesign": "quiz",
}
# NYT URL paths that say what a story is. NYT sends no text, so these are all we have.
NYT_PATH_FLAGS = {
    "/briefing/": "roundup",
    "/opinion/": "opinion",
    "/interactive/": "interactive",
    "/video/": "video",
    "/slideshow/": "gallery",
    "/podcasts/": "podcast",
    "/crosswords/": "quiz",
    "/games/": "quiz",
    "/reviews/": "review",
    "/recipes/": "other_format",
}
# A BBC listing item type other than "article" is not a written report.
BBC_TYPE_FLAGS = {
    "video": "video",
    "audio": "audio",
    "liveexperience": "live",
    "photogallery": "gallery",
    "gallery": "gallery",
    "podcast": "podcast",
}

DEFAULT_PORTS = {"http": "80", "https": "443"}
_WHITESPACE = re.compile(r"\s+")


def canonical_url(url: str | None) -> str | None:
    """Scheme and host lower-cased, query and fragment removed, trailing slash removed.

    One story reached by two links has one canonical URL, which is what makes the URL usable as an
    alias. A URL with no scheme or host is left alone, because guessing one would invent an alias.
    """
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return url.strip()
    host = parts.hostname or ""
    port = parts.port
    netloc = host.lower()
    if port is not None and DEFAULT_PORTS.get(parts.scheme.lower()) != str(port):
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def aliases(outlet: str, row: dict[str, Any]) -> list[str]:
    """Every id this row carries, most specific first, with the canonical URL last.

    The first entry becomes the story's primary alias and never changes, so a cluster id minted
    from it later (section 7.8) means the same event for the life of the store.
    """
    found: list[str] = []
    for field in ALIAS_FIELDS.get(outlet, ()):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, int):
            found.append(str(value))
    url = canonical_url(row.get("url"))
    if url:
        found.append(url)
    return list(dict.fromkeys(found))


def story_id(outlet: str, primary_alias: str) -> str:
    """A stable id for a story: st_ plus 16 hex characters over the outlet and its primary alias."""
    digest = hashlib.sha256(f"{outlet}\n{primary_alias}".encode()).hexdigest()
    return f"st_{digest[:16]}"


def utc_iso(value: Any) -> str | None:
    """A time as YYYY-MM-DDTHH:MM:SSZ, or None when it will not parse.

    scraper.common.rss_date passes values it cannot parse through unchanged, and outlets send
    several shapes, so accept ISO 8601 first, then RFC 822, then a unix timestamp. A naive time is
    read as UTC: every outlet here sends UTC or an offset, and a wrong guess of a local zone would
    be worse than reading the value as written.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Milliseconds if the number is far too large to be seconds.
        seconds = float(value) / 1000 if abs(float(value)) > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_text(text: str) -> str:
    """Whitespace collapsed, so a hash does not change when an outlet reflows a paragraph."""
    return _WHITESPACE.sub(" ", text).strip()


def text_hash(text: str | None) -> str | None:
    """A hash of the article text, used to notice a material change (section 7.4)."""
    if not text:
        return None
    normalised = normalise_text(text)
    if not normalised:
        return None
    return hashlib.sha256(normalised.encode()).hexdigest()


def _category_values(categories: Any, key: str) -> list[Any]:
    if not isinstance(categories, dict):
        return []
    value = categories.get(key)
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _path_of(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    return urlsplit(url).path.lower()


def format_flags(outlet: str, row: dict[str, Any]) -> list[str]:
    """What kind of page this is, from the outlet's own metadata only.

    A flagged story still gets a category, but section 7.5 forbids it ever becoming a pin. Nothing
    here reads the article text and nothing here is inferred by a model.
    """
    flags: set[str] = set()
    categories = row.get("categories")
    path = _path_of(row.get("url"))

    if "/live/" in path or path.endswith("/live") or "/live-" in path:
        flags.add("live")

    if outlet == "reuters":
        for subject in _category_values(categories, "subjects"):
            code = subject.get("code") if isinstance(subject, dict) else subject
            if isinstance(code, str) and code.upper() in REUTERS_N2_FLAGS:
                flags.add(REUTERS_N2_FLAGS[code.upper()])
        media = row.get("primary_media_type")
        if isinstance(media, str) and media.lower() in {"video", "gallery", "image"}:
            flags.add("video" if media.lower() == "video" else "gallery")
        article_type = row.get("article_type")
        if isinstance(article_type, str) and "live" in article_type.lower():
            flags.add("live")

    elif outlet == "bbc":
        for field in ("type", "subtype"):
            value = row.get(field)
            if isinstance(value, str):
                key = value.replace(" ", "").replace("-", "").lower()
                if key in BBC_TYPE_FLAGS:
                    flags.add(BBC_TYPE_FLAGS[key])
                elif field == "type" and key and key != "article":
                    flags.add("other_format")

    elif outlet == "guardian":
        for tone in _category_values(categories, "tones"):
            if isinstance(tone, str):
                key = tone.replace(" ", "").replace("-", "").lower()
                flag = GUARDIAN_TONE_FLAGS.get(key) or GUARDIAN_TONE_FLAGS.get(tone.strip().lower())
                if flag:
                    flags.add(flag)
        design = categories.get("design") if isinstance(categories, dict) else None
        if isinstance(design, str):
            flag = GUARDIAN_DESIGN_FLAGS.get(design.replace(" ", "").lower())
            if flag:
                flags.add(flag)
        for card_type in _category_values(categories, "card_type"):
            if isinstance(card_type, str) and card_type.lower() in {"video", "gallery", "audio"}:
                flags.add(card_type.lower())

    elif outlet == "pbs":
        post_type = row.get("post_type")
        if isinstance(post_type, str) and post_type.lower() not in {"", "post"}:
            # A /newshour/show/ page is a broadcast segment, not a written report.
            flags.add("broadcast" if post_type.lower() == "show" else "other_format")
        if path.startswith("/newshour/show/"):
            flags.add("broadcast")
        if row.get("is_video") and not row.get("text"):
            flags.add("video")

    elif outlet == "nyt":
        for fragment, flag in NYT_PATH_FLAGS.items():
            if fragment in path:
                flags.add(flag)

    return sorted(flags)


def authors_of(row: dict[str, Any]) -> list[str]:
    """Author names as a flat list of strings. Reuters sends dicts; the feeds send strings."""
    names: list[str] = []
    for author in row.get("authors") or []:
        if isinstance(author, str) and author.strip():
            names.append(author.strip())
        elif isinstance(author, dict):
            name = author.get("byline") or author.get("name") or author.get("full_name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    single = row.get("author")
    if not names and isinstance(single, str) and single.strip():
        names.append(single.strip())
    return list(dict.fromkeys(names))


def word_count_of(row: dict[str, Any]) -> int | None:
    """The outlet's own word count when it sent one, otherwise counted from the text."""
    value = row.get("word_count")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    text = row.get("text")
    if isinstance(text, str) and text.strip():
        return len(normalise_text(text).split())
    return None
