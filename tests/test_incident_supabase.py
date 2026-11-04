import argparse
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from sources.incident.cli import run
from sources.incident.geoconfirmed import parse_csv, snapshot_for
from sources.incident.supabase import SupabaseWriter


FIXTURE = Path(__file__).parent / "fixtures" / "incident" / "geoconfirmed.csv"
FAKE_KEY = "fake-service-key-for-tests-only-0123456789"


class SupabaseTests(unittest.TestCase):
    def test_snapshot_writes_all_tables_once_without_leaking_key(self):
        snapshot = snapshot_for(parse_csv(FIXTURE.read_text(encoding="utf-8"))[0], "2026-11-03T20:00:00Z")
        calls = []

        def transport(table, payload, headers):
            calls.append((table, payload, headers))
            return 201, dict(headers), b"{}"

        writer = SupabaseWriter("https://example.supabase.co", FAKE_KEY, transport=transport)
        writer.write_snapshot(snapshot)

        tables = [call[0] for call in calls]
        self.assertEqual(tables[:5], ["sources", "raw_documents", "document_routes", "extractions", "incidents"])
        self.assertEqual(tables[-1], "crawler_runs")
        self.assertEqual(tables.count("claims"), len(snapshot["claims"]))
        self.assertEqual(tables.count("receipts"), 1)
        self.assertEqual(tables.count("incident_evidence"), len(snapshot["claims"]))
        for _, _, headers in calls:
            self.assertEqual(headers["apikey"], FAKE_KEY)
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertNotIn(FAKE_KEY, repr(writer))

    def test_write_requires_the_existing_secret_name(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SUPABASE_SECRET_KEY"):
                run(argparse.Namespace(fixture=str(FIXTURE), write=True, no_deepseek=True))

    def test_write_uses_the_existing_url_and_secret_names(self):
        env = {"NEXT_PUBLIC_SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": FAKE_KEY}
        with patch.dict(os.environ, env, clear=True), patch("sources.incident.cli.SupabaseWriter") as writer:
            run(argparse.Namespace(fixture=str(FIXTURE), write=True, no_deepseek=True))
            writer.assert_called_once_with(env["NEXT_PUBLIC_SUPABASE_URL"], FAKE_KEY)
            writer.return_value.write_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
