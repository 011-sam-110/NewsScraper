import json
import subprocess
import sys
import unittest
from pathlib import Path

from sources.incident.geoconfirmed import parse_csv, snapshot_for


FIXTURE = Path(__file__).parent / "fixtures" / "incident" / "geoconfirmed.csv"


class GeoConfirmedTests(unittest.TestCase):
    def setUp(self):
        self.text = FIXTURE.read_text(encoding="utf-8")
        self.rows = parse_csv(self.text)

    def test_parser_keeps_only_dated_visual_incidents(self):
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[0]["Id"], "4b70e207-3abf-4d63-8f10-0715fb05e60b")
        self.assertNotIn("satellite-site-row", {row["Id"] for row in self.rows})

    def test_parser_rejects_a_malformed_date(self):
        text = self.text.replace("2026-10-27", "not-a-date")
        self.assertEqual(parse_csv(text), [])

    def test_parser_rejects_invalid_coordinates(self):
        for value in ("nan", "inf", "91"):
            with self.subTest(value=value):
                text = self.text.replace("50.334626", value)
                rows = parse_csv(text)
                self.assertNotIn("4b70e207-3abf-4d63-8f10-0715fb05e60b", {row["Id"] for row in rows})

    def test_snapshot_preserves_source_coordinates_and_receipts(self):
        snapshot = snapshot_for(self.rows[0], "2026-11-03T20:00:00Z")
        incident = snapshot["incident"]
        self.assertEqual(incident["location"]["geometry"]["coordinates"], [36.20466, 50.334626])
        self.assertEqual(incident["location"]["method"], "source")
        self.assertEqual(incident["location"]["uncertainty_m"], 0)
        self.assertEqual(incident["title"], self.rows[0]["Description"])
        self.assertEqual(snapshot["run"]["model"], "structured-parser")
        self.assertEqual(snapshot["run"]["schema_version"], 1)
        self.assertEqual(snapshot["run"]["raw_document_count"], 1)
        self.assertTrue(snapshot["receipts"][0]["source_url"].startswith("https://x.com/"))
        self.assertTrue(snapshot["receipts"][0]["content_hash"].startswith("sha256:"))
        self.assertEqual(snapshot["receipts"][0]["origin_group"], incident["id"])
        self.assertFalse(any(claim["kind"] == "fatalities" for claim in snapshot["claims"]))

    def test_document_key_scopes_extraction_to_theater_and_schema(self):
        first = snapshot_for(self.rows[0], "2026-11-03T20:00:00Z")
        second = snapshot_for(self.rows[0], "2026-11-03T20:00:00Z", schema_version=2)
        self.assertNotEqual(first["run"]["idempotency_key"], second["run"]["idempotency_key"])
        self.assertTrue(first["run"]["idempotency_key"].endswith(":ukraine:1"))

    def test_cli_fixture_path_returns_a_real_snapshot(self):
        root = Path(__file__).parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "sources", "incident-ukraine", "--fixture", str(FIXTURE), "--no-deepseek"],
            cwd=root, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual(snapshot["schema"], "provenance.incident/1")
        self.assertEqual(snapshot["incident"]["theater"], "ukraine")


if __name__ == "__main__":
    unittest.main()
