"""The config hash: what produced an extraction, so a pin can never outlive the setup that made it.

Section 10.5. The hash covers everything that can change what the model says. `gate.json` records
the hash that passed, and publish sends pins only while the current hash matches a passing one.

Two hashes, not one. The design names a single hash over every component, including the resolver
thresholds and the GeoNames build. Splitting out an extract hash is an implementation choice with a
reason: `extractions` is keyed by (story_id, config_hash), so if the extract key moved whenever a
resolver threshold changed, every stored extraction would be thrown away and paid for again. The
gate hash still covers everything, and is what withholds pins.

One deliberate gap. The design says the hash covers "the model string the API returns", which is
known only after a call, while the key is needed before one to tell whether a story was already
done. So the hash carries the model id that was asked for, and every extraction row also stores the
string the answer carried. `served_model_disagreements()` finds any row where the two drifted, so a
silent point release shows up in the audit rather than hiding.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import prompts, taxonomy
from .deepseek import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, EFFORT
from .taxonomy import (
    CATEGORY_IDS,
    EVENT_DATE_DAYS_AFTER,
    EVENT_DATE_DAYS_BEFORE,
    NOT_EVENT_REASONS,
    PLACE_KINDS,
)

# How many hex characters of the digest a hash keeps. 16 is 64 bits: enough that two configurations
# cannot collide in a project with a handful of them, and short enough to read in a table.
HASH_LENGTH = 16

EXTRACT_TEMPERATURE = 0.0
EXTRACT_MAX_TOKENS = DEFAULT_MAX_TOKENS
# Thinking on at max effort since 2026-11-05 (Sam). This moves the extract hash from 633932b5b392beb3, so the
# cut-over extracts the stories of the last 72 hours again with `extract --redo` (section 10.5).
EXTRACT_THINKING = "enabled"
EXTRACT_EFFORT = EFFORT


def hash_components(components: dict[str, Any]) -> str:
    """A stable hash over a mapping. Keys are sorted, so the order they were written cannot matter."""
    canonical = json.dumps(components, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def extract_components(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Everything that can change what the extract stage gets back from the model."""
    return {
        "stage": "extract",
        "model": model,
        "temperature": EXTRACT_TEMPERATURE,
        "max_tokens": EXTRACT_MAX_TOKENS,
        "thinking": EXTRACT_THINKING,
        "reasoning_effort": EXTRACT_EFFORT,
        "categories": list(CATEGORY_IDS),
        "not_event_reasons": list(NOT_EVENT_REASONS),
        "place_kinds": list(PLACE_KINDS),
        "date_rule": {"days_before": EVENT_DATE_DAYS_BEFORE, "days_after": EVENT_DATE_DAYS_AFTER},
        **prompts.prompt_components(),
    }


def extract_config_hash(model: str = DEFAULT_MODEL) -> str:
    return hash_components(extract_components(model))


def resolve_components(gazetteer_hash: str, names_rule: int) -> dict[str, Any]:
    """Everything that can move a coordinate, including the bytes the gazetteer was built from.

    The gazetteer hash is an argument rather than read here, because reading it means opening the
    database, and config.py is imported by code that must work without one. Passing it in also
    makes it impossible to compute this hash while forgetting which gazetteer it describes.

    The names rule is an argument for the same reason, and because the build hash cannot carry it: the
    build hash covers the five source files, and names rule 2 (geonames.NAMES_RULE) reads more names out
    of the same files. Pass the rule the gazetteer was BUILT with (gazetteer_resolve_hash does), never
    the rule this code knows, or a gazetteer built before the rule would carry the new hash.
    """
    from .geonames import KEPT_CLASSES, PINNABLE_PRECISIONS, PRECISION_BY_CODE
    from .resolve import (
        AMBIGUITY_DISTANCE_KM,
        AMBIGUITY_POPULATION_RATIO,
        CITY_RADIUS_KM,
        KIND_PRECISION,
        NAMESAKE_TOWN_MAX_POPULATION,
        REGION_KINDS,
        REGION_RADIUS_KM,
    )

    return {
        "stage": "resolve",
        "gazetteer": gazetteer_hash,
        "names_rule": names_rule,
        "kept_classes": sorted(KEPT_CLASSES),
        "precision_by_code": dict(sorted(PRECISION_BY_CODE.items())),
        "pinnable": sorted(PINNABLE_PRECISIONS),
        "kind_precision": dict(sorted(KIND_PRECISION.items())),
        "city_radius_km": CITY_RADIUS_KM,
        "region_radius_km": REGION_RADIUS_KM,
        "ambiguity_distance_km": AMBIGUITY_DISTANCE_KM,
        "ambiguity_population_ratio": AMBIGUITY_POPULATION_RATIO,
        "region_kinds": sorted(REGION_KINDS),
        "namesake_town_max_population": NAMESAKE_TOWN_MAX_POPULATION,
    }


def resolve_config_hash(gazetteer_hash: str, names_rule: int) -> str:
    return hash_components(resolve_components(gazetteer_hash, names_rule))


def gazetteer_resolve_hash(connection: Any) -> str:
    """The resolve hash of an open gazetteer: its build hash and the names rule it was built with."""
    from .geonames import build_hash, names_rule

    return resolve_config_hash(build_hash(connection), names_rule(connection))


def cluster_components(
    resolve_hash: str, extract_hash: str, model: str = DEFAULT_MODEL
) -> dict[str, Any]:
    """Everything that can change which stories end up in one cluster.

    It carries the extract and resolve hashes rather than repeating their parts, because cluster
    reads what those two stages wrote and never recomputes it: a cluster made from one set of
    extractions is not the same cluster made from another, even with identical thresholds here.
    """
    from .cluster import (
        CANDIDATE_RADIUS_KM,
        CLUSTER_TEMPERATURE,
        CLUSTER_WINDOW_HOURS,
        HEADLINE_OVERLAP,
        JOINING_VERDICTS,
        PIN_SPREAD_KM,
        STOP_WORDS,
    )

    return {
        "stage": "cluster",
        "model": model,
        "temperature": CLUSTER_TEMPERATURE,
        "thinking": EXTRACT_THINKING,
        "reasoning_effort": EXTRACT_EFFORT,
        "extract": extract_hash,
        "resolve": resolve_hash,
        "window_hours": CLUSTER_WINDOW_HOURS,
        "candidate_radius_km": CANDIDATE_RADIUS_KM,
        "pin_spread_km": PIN_SPREAD_KM,
        "headline_overlap": HEADLINE_OVERLAP,
        "stop_words": sorted(STOP_WORDS),
        "joining_verdicts": sorted(JOINING_VERDICTS),
        "pin_possible": taxonomy.pin_possible_map(),
        **prompts.cluster_prompt_components(),
    }


def cluster_config_hash(
    resolve_hash: str, extract_hash: str, model: str = DEFAULT_MODEL
) -> str:
    return hash_components(cluster_components(resolve_hash, extract_hash, model))


# The verify stage (section 7.9). Same model and effort as every other call: a fresh call, not a stronger one.
VERIFY_TEMPERATURE = 0.0
VERIFY_MAX_TOKENS = DEFAULT_MAX_TOKENS


def verify_components(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Everything that can change what the verifier and the critic answer, and how code reads it.

    Its own hash, apart from extract's: `verifications` is keyed by it, so a verify prompt change asks
    the pins again and never throws away an extraction.
    """
    from .verify import CRITIC_CALLS_MAX, DECISIONS, RULE_POINTS

    return {
        "stage": "verify",
        "model": model,
        "temperature": VERIFY_TEMPERATURE,
        "max_tokens": VERIFY_MAX_TOKENS,
        "thinking": EXTRACT_THINKING,
        "reasoning_effort": EXTRACT_EFFORT,
        "rule_points": list(RULE_POINTS),
        # A category flag only when no category the verifier accepts can pin, so the pin flags are part of it.
        "category_flag": "no acceptable category can pin",
        "pin_possible": taxonomy.pin_possible_map(),
        "critic_calls_max": CRITIC_CALLS_MAX,
        "decisions": dict(sorted(DECISIONS.items())),
        **prompts.verify_prompt_components(),
    }


def verify_config_hash(model: str = DEFAULT_MODEL) -> str:
    return hash_components(verify_components(model))


def placement_components(extract_hash: str, resolve_hash: str, verify_hash: str) -> dict[str, Any]:
    """Everything that decides which headline pins Provenance gets: the snapshot's configHash.

    The gate (section 10) measures the final output, so this is the hash `gate.json` records for the
    Headline sources layer. It carries the three stage hashes rather than repeating their parts, and adds
    the rules headline-places applies itself: which categories can pin, the precision levels, the venue
    fallback, and which verify outcomes send a pin.
    """
    from .headline_places import FALLBACK_KINDS, FALLBACK_LEVEL, FALLBACK_PRECISIONS, PRECISION
    from .verify import PASSING

    return {
        "stage": "placement",
        "extract": extract_hash,
        "resolve": resolve_hash,
        "verify": verify_hash,
        "pin_possible": taxonomy.pin_possible_map(),
        "precision_levels": dict(sorted(PRECISION.items())),
        "venue_fallback": {
            "kinds": sorted(FALLBACK_KINDS),
            "container_precisions": sorted(FALLBACK_PRECISIONS),
            "level": FALLBACK_LEVEL,
        },
        "passing": sorted(PASSING),
    }


def placement_config_hash(extract_hash: str, resolve_hash: str, verify_hash: str) -> str:
    return hash_components(placement_components(extract_hash, resolve_hash, verify_hash))
