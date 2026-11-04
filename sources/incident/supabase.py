"""Server-side Supabase upserts for the incident evidence graph."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, str], bytes]]


class SupabaseWriter:
    def __init__(self, base_url: str, service_key: str, transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self.transport = transport or self._urllib_transport

    def __repr__(self) -> str:
        return f"SupabaseWriter(base_url={self.base_url!r}, service_key=<redacted>)"

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }

    def _urllib_transport(self, table: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            f"{self.base_url}/rest/v1/{table}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers or {}), error.read()

    def upsert(self, table: str, payload: dict[str, Any]) -> None:
        status, _, body = self.transport(table, payload, self._headers())
        if status >= 300:
            raise RuntimeError(f"Supabase {table} answered {status}: {body[:200].decode('utf-8', 'replace')}")

    def write_snapshot(self, snapshot: dict[str, Any], source_row: dict[str, str] | None = None) -> None:
        incident = snapshot["incident"]
        run = snapshot["run"]
        self.upsert("sources", {
            "id": run["source"], "route": "keyless", "owner": run["worker"], "cadence": "minutes",
            "terms": "Public read API; custom User-Agent and cache headers",
            "license": "No explicit licence supplied; preserve source links", "kill_switch": False,
        })
        self.upsert("raw_documents", {
            "id": run["idempotency_key"], "source_id": run["source"],
            "external_id": incident["id"], "url": snapshot["receipts"][0]["source_url"],
            "content_hash": snapshot["receipts"][0]["content_hash"], "fetched_at": run["fetched_at"],
            "raw_storage_uri": None, "body": source_row,
        })
        self.upsert("document_routes", {
            "id": run["idempotency_key"], "document_hash": run["idempotency_key"],
            "theater": run["worker"], "schema_version": run["schema_version"],
            "status": "complete", "updated_at": run["fetched_at"],
        })
        self.upsert("extractions", {
            "id": run["idempotency_key"], "document_hash": run["idempotency_key"],
            "theater": run["worker"], "schema_version": run["schema_version"],
            "model": run["model"], "prompt_version": run["prompt_version"],
            "payload": {"claims": snapshot["claims"], "model_error": run["model_error"]},
        })
        self.upsert("incidents", {"id": incident["id"], "data": incident})
        for claim in snapshot["claims"]:
            self.upsert("claims", {"id": claim["id"], "incident_id": incident["id"], "data": claim})
            self.upsert("incident_evidence", {
                "id": f"evidence:{claim['id']}", "incident_id": incident["id"],
                "claim_id": claim["id"], "relation": claim["relation"],
            })
        for receipt in snapshot["receipts"]:
            self.upsert("receipts", {"id": receipt["id"], "incident_id": incident["id"], "data": receipt})
        self.upsert("crawler_runs", {
            "id": f"run:{run['idempotency_key']}", "worker": run["worker"], "source": run["source"],
            "fetched_at": run["fetched_at"], "raw_document_count": run["raw_document_count"],
            "model": run["model"], "prompt_version": run["prompt_version"],
            "schema_version": run["schema_version"], "status": "complete", "model_error": run["model_error"],
        })
