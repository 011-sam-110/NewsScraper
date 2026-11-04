"""GeoConfirmed CSV parsing and the provenance.incident/1 snapshot builder."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import datetime
from typing import Any

VISUAL_ORIGINS = frozenset({"VID", "UAV", "PIC"})


def parse_csv(text: str) -> list[dict[str, str]]:
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    accepted = []
    for row in rows:
        conflict = (row.get("Conflict") or "").casefold()
        origin = (row.get("Origin") or "").upper()
        if "ukraine" not in conflict or origin not in VISUAL_ORIGINS:
            continue
        if not row.get("Date") or not row.get("Latitude") or not row.get("Longitude"):
            continue
        try:
            datetime.fromisoformat(row["Date"].replace("Z", "+00:00"))
            latitude = float(row["Latitude"])
            longitude = float(row["Longitude"])
        except ValueError:
            continue
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            continue
        if not row.get("Id"):
            continue
        accepted.append(row)
    return accepted


def _row_hash(row: dict[str, str]) -> str:
    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _claim(source_id: str, kind: str, index: int, value: Any, quote: str) -> dict[str, Any]:
    return {
        "id": f"claim:{source_id}:{kind}:{index}",
        "kind": kind,
        "value": value,
        "attribution": "GeoConfirmed",
        "origin_group": source_id,
        "evidence_quote": quote,
        "relation": "supports",
    }


def _event_type(row: dict[str, str]) -> str:
    text = f"{row.get('Name', '')} {row.get('Description', '')}".casefold()
    if "drone" in text or "uav" in text:
        return "drone_strike"
    if "missile" in text:
        return "missile_strike"
    if "explosion" in text or "strike" in text or "attack" in text:
        return "explosive_impact"
    return "conflict_event"


def snapshot_for(
    row: dict[str, str],
    fetched_at: str,
    schema_version: int = 1,
    interpretation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_id = f"geoconfirmed:{row['Id']}"
    quote = row.get("Description") or row.get("Name") or row["Id"]
    model = "structured-parser"
    model_error = None
    event_type = _event_type(row)
    event_time = row["Date"]
    actors = [value for value in (row.get("OrbatUnits"), row.get("Units"), row.get("Faction")) if value]
    weapons = [row["EquipmentItems"]] if row.get("EquipmentItems") else []
    if interpretation:
        model = interpretation.get("model", model)
        model_error = interpretation.get("model_error")
        payload = interpretation.get("payload", {})
        event_type = payload.get("event_type") or event_type
        event_time = payload.get("event_time_start") or event_time
        actors = payload.get("actors") or actors
        weapons = payload.get("weapons") or weapons

    digest = _row_hash(row)
    idempotency_key = f"{digest}:ukraine:{schema_version}"
    claims = [
        _claim(source_id, "location", 1, {
            "coordinates": [float(row["Longitude"]), float(row["Latitude"])],
            "method": "source",
        }, quote),
        _claim(source_id, "event_type", 2, event_type, quote),
    ]
    if actors:
        claims.append(_claim(source_id, "actor", 3, actors, quote))
    if weapons:
        claims.append(_claim(source_id, "weapon", 4, weapons, quote))

    incident = {
        "id": source_id,
        "theater": "ukraine",
        "type": event_type,
        "title": quote,
        "event_time_start": event_time,
        "event_time_end": None,
        "first_seen": fetched_at,
        "latest_evidence": fetched_at,
        "location": {
            "method": "source",
            "text": row.get("PlusCode") or row.get("Description") or row.get("Name") or row["Id"],
            "geometry": {"type": "Point", "coordinates": [float(row["Longitude"]), float(row["Latitude"])]},
            "precision": "point",
            "uncertainty_m": 0,
        },
        "status": "visually_located",
    }
    receipts = [{
        "id": f"receipt:{row['Id']}",
        "source_name": "GeoConfirmed",
        "source_url": (row.get("Source") or "").split(",", 1)[0].strip() or None,
        "archive_url": row.get("Geolocation") or None,
        "content_hash": f"sha256:{digest}",
        "publication_time": row["Date"],
        "fetched_at": fetched_at,
        "evidence_quote": quote,
        "origin_group": source_id,
        "source_event_id": row["Id"],
    }]
    run = {
        "worker": "ukraine",
        "source": "geoconfirmed",
        "fetched_at": fetched_at,
        "raw_document_count": 1,
        "model": model,
        "model_error": model_error,
        "prompt_version": "incident-ukraine-v1" if model != "structured-parser" else None,
        "schema_version": schema_version,
        "idempotency_key": idempotency_key,
    }
    return {"schema": "provenance.incident/1", "incident": incident, "claims": claims, "receipts": receipts, "run": run}
