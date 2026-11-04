"""Build and optionally publish one Ukraine incident from GeoConfirmed."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from newsfeed.deepseek import Client
from .deepseek import maybe_interpret
from .geoconfirmed import parse_csv, snapshot_for
from .supabase import SupabaseWriter

EXPORT_URL = "https://geoconfirmed.org/api/Map/export/csv"
USER_AGENT = "ProvenanceIncident/1.0 (+https://provenance-online.com)"


def fetch_export() -> str:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    url = f"{EXPORT_URL}?start={start}&end={end}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8-sig")


def build_incident(text: str, use_deepseek: bool = True) -> tuple[dict[str, Any], dict[str, str]]:
    rows = parse_csv(text)
    if not rows:
        raise ValueError("GeoConfirmed returned no dated Ukraine visual incidents with coordinates")
    row = sorted(rows, key=lambda item: (item["Date"], item["Id"]), reverse=True)[0]
    interpretation = None
    if use_deepseek and os.environ.get("DEEPSEEK_KEY_UKRAINE"):
        try:
            interpretation = maybe_interpret(row, Client(os.environ["DEEPSEEK_KEY_UKRAINE"]))
        except Exception as error:
            interpretation = {
                "model": "structured-parser",
                "payload": {},
                "model_error": {"type": type(error).__name__, "message": str(error)[:200]},
            }
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return snapshot_for(row, fetched_at, interpretation=interpretation), row


def run(args: argparse.Namespace) -> int:
    text = Path(args.fixture).read_text(encoding="utf-8-sig") if args.fixture else fetch_export()
    snapshot, row = build_incident(text, use_deepseek=not args.no_deepseek)
    if args.write:
        url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        if not url or not key:
            raise ValueError("NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SECRET_KEY are required with --write")
        SupabaseWriter(url, key).write_snapshot(snapshot, row)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixture", help="Read a saved GeoConfirmed CSV instead of the live export")
    parser.add_argument("--write", action="store_true", help="Upsert the incident into Supabase")
    parser.add_argument("--no-deepseek", action="store_true", help="Use structured parsing even when a key exists")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sources incident-ukraine", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))
