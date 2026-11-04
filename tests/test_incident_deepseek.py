import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from sources.incident.deepseek import maybe_interpret
from sources.incident.geoconfirmed import parse_csv


FIXTURE = Path(__file__).parent / "fixtures" / "incident" / "geoconfirmed.csv"


class FakeClient:
    def __init__(self, answer):
        self.answer = answer

    def complete(self, system, user, **kwargs):
        return SimpleNamespace(content=json.dumps(self.answer), model="deepseek-flash")


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.row = parse_csv(FIXTURE.read_text(encoding="utf-8"))[0]

    def test_model_claims_never_replace_source_coordinates(self):
        answer = {
            "event_type": "drone_strike",
            "event_time_start": "2026-10-27",
            "actors": ["Rubicon"],
            "weapons": ["Bomber UAV"],
            "fatalities": [],
            "evidence_quote": self.row["Description"],
            "geometry": {"type": "Point", "coordinates": [0, 0]},
        }
        result = maybe_interpret(self.row, FakeClient(answer))
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(result["payload"]["event_type"], "drone_strike")
        self.assertNotIn("geometry", result["payload"])

    def test_non_verbatim_quote_is_rejected(self):
        answer = {"event_type": "drone_strike", "evidence_quote": "invented elsewhere"}
        with self.assertRaisesRegex(ValueError, "evidence_quote"):
            maybe_interpret(self.row, FakeClient(answer))

    def test_whitespace_changed_quote_is_rejected(self):
        answer = {
            "event_type": "drone_strike",
            "evidence_quote": self.row["Description"].replace("Rubicon unit", "Rubicon  unit"),
        }
        with self.assertRaisesRegex(ValueError, "evidence_quote"):
            maybe_interpret(self.row, FakeClient(answer))

    def test_missing_key_uses_explicit_structured_fallback(self):
        result = maybe_interpret(self.row, None)
        self.assertEqual(result["model"], "structured-parser")
        self.assertIsNone(result["model_error"])


if __name__ == "__main__":
    unittest.main()
