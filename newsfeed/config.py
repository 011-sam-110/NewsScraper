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

from . import prompts
from .deepseek import DEFAULT_MAX_TOKENS, DEFAULT_MODEL
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
EXTRACT_THINKING = "disabled"


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
        "categories": list(CATEGORY_IDS),
        "not_event_reasons": list(NOT_EVENT_REASONS),
        "place_kinds": list(PLACE_KINDS),
        "date_rule": {"days_before": EVENT_DATE_DAYS_BEFORE, "days_after": EVENT_DATE_DAYS_AFTER},
        **prompts.prompt_components(),
    }


def extract_config_hash(model: str = DEFAULT_MODEL) -> str:
    return hash_components(extract_components(model))


def resolve_components(gazetteer_hash: str) -> dict[str, Any]:
    """Everything that can move a coordinate, including the bytes the gazetteer was built from.

    The gazetteer hash is an argument rather than read here, because reading it means opening the
    database, and config.py is imported by code that must work without one. Passing it in also
    makes it impossible to compute this hash while forgetting which gazetteer it describes.
    """
    from .geonames import KEPT_CLASSES, PINNABLE_PRECISIONS, PRECISION_BY_CODE
    from .resolve import (
        AMBIGUITY_DISTANCE_KM,
        AMBIGUITY_POPULATION_RATIO,
        CITY_RADIUS_KM,
        KIND_PRECISION,
        REGION_RADIUS_KM,
    )

    return {
        "stage": "resolve",
        "gazetteer": gazetteer_hash,
        "kept_classes": sorted(KEPT_CLASSES),
        "precision_by_code": dict(sorted(PRECISION_BY_CODE.items())),
        "pinnable": sorted(PINNABLE_PRECISIONS),
        "kind_precision": dict(sorted(KIND_PRECISION.items())),
        "city_radius_km": CITY_RADIUS_KM,
        "region_radius_km": REGION_RADIUS_KM,
        "ambiguity_distance_km": AMBIGUITY_DISTANCE_KM,
        "ambiguity_population_ratio": AMBIGUITY_POPULATION_RATIO,
    }


def resolve_config_hash(gazetteer_hash: str) -> str:
    return hash_components(resolve_components(gazetteer_hash))
