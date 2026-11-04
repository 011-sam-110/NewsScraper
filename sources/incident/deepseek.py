"""Optional DeepSeek interpretation with source-evidence validation."""

from __future__ import annotations

import json
from typing import Any

SYSTEM = """You interpret one visually located conflict record. Return only JSON with event_type, event_time_start, actors, weapons, fatalities and evidence_quote. Use null for unknown values. Never invent a location, coordinate, casualty number or final narrative. evidence_quote must be a verbatim span from the supplied description."""


def maybe_interpret(row: dict[str, str], client: Any) -> dict[str, Any]:
    if client is None:
        return {"model": "structured-parser", "payload": {}, "model_error": None}
    user = json.dumps({
        "date": row.get("Date"),
        "name": row.get("Name"),
        "description": row.get("Description"),
        "faction": row.get("Faction"),
        "equipment": row.get("EquipmentItems"),
        "units": row.get("OrbatUnits") or row.get("Units"),
    }, ensure_ascii=False)
    completion = client.complete(SYSTEM, user, json_object=True, max_tokens=400, temperature=0.0)
    payload = json.loads(completion.content)
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek response must be a JSON object")
    quote = payload.get("evidence_quote")
    source = row.get("Description") or row.get("Name") or ""
    if quote and str(quote) not in source:
        raise ValueError("evidence_quote must be a verbatim source span")
    allowed = {"event_type", "event_time_start", "actors", "weapons", "fatalities", "evidence_quote"}
    clean = {key: payload[key] for key in allowed if key in payload}
    return {"model": completion.model, "payload": clean, "model_error": None}
