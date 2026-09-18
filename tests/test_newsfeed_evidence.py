"""The evidence check (section 13). Both failures were found in real output, not imagined.

Every quote here is one the pipeline actually produced, or a minimal variation on one. The two
that must fail are the two the architecture document names; the ones that must pass are what an
over-wide version of either rule would throw away, which is the expensive half of the mistake.
"""

from __future__ import annotations

import unittest

from newsfeed.extract import check_evidence


def payload(quote: str, name: str) -> dict:
    return {"is_physical_event": True, "event_place": {"name": name, "quote": quote}}


class AttributionTests(unittest.TestCase):
    def test_the_aleppo_quote_fails(self) -> None:
        """Section 13, failure 1. Verbatim, contains the place name, and still not evidence."""
        reason = check_evidence(payload("Yasser Salim told Reuters in Aleppo.", "Aleppo"))
        self.assertIsNotNone(reason)
        self.assertIn("spoke to a reporter", reason)

    def test_a_place_named_before_the_attribution_passes(self) -> None:
        """The expensive half. This quote is real evidence and a wider rule would drop it."""
        quote = (
            "Iranian strikes damaged multiple American military aircraft this week on the "
            "Muwaffaq Salti Air Base in Jordan, a U.S. official told Reuters"
        )
        self.assertIsNone(check_evidence(payload(quote, "Muwaffaq Salti Air Base")))

    def test_every_attribution_verb_and_outlet_is_caught(self) -> None:
        for verb in ("told", "spoke to", "speaking to", "said to"):
            for outlet in ("Reuters", "the BBC", "the Guardian", "reporters"):
                quote = f"A resident {verb} {outlet} in Aleppo."
                with self.subTest(quote=quote):
                    self.assertIsNotNone(check_evidence(payload(quote, "Aleppo")))

    def test_the_preposition_may_vary(self) -> None:
        for preposition in ("in", "at", "from", "near", "outside"):
            quote = f"A witness told Reuters {preposition} Aleppo."
            with self.subTest(preposition=preposition):
                self.assertIsNotNone(check_evidence(payload(quote, "Aleppo")))

    def test_an_attribution_naming_a_different_place_does_not_fail(self) -> None:
        """The rule is about THIS place being the interview place, not about any attribution."""
        quote = "A fire tore through the market in Aleppo, a witness told Reuters in Damascus."
        self.assertIsNone(check_evidence(payload(quote, "Aleppo")))

    def test_a_quote_with_no_attribution_passes(self) -> None:
        quote = "A man was stabbed near Westminster Bridge in central London on Sunday"
        self.assertIsNone(check_evidence(payload(quote, "Westminster")))


class PlannedTests(unittest.TestCase):
    def test_the_miami_quote_fails(self) -> None:
        """Section 13, failure 2."""
        quote = (
            "Alex Saab is scheduled to appear for a change of plea hearing before U.S. District "
            "Judge Kathleen Williams in Miami"
        )
        reason = check_evidence(payload(quote, "Miami"))
        self.assertIsNotNone(reason)
        self.assertIn("planned", reason)

    def test_the_other_ways_of_saying_it_are_caught(self) -> None:
        for phrase in (
            "is due to appear in Miami",
            "are expected to gather in Miami",
            "was set to open in Miami",
            "will take place in Miami",
            "will go on trial in Miami",
            "plans to march in Miami",
            "planned for Miami",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(check_evidence(payload(f"The hearing {phrase}.", "Miami")))

    def test_a_past_event_with_a_similar_shape_passes(self) -> None:
        """`appeared`, not `is scheduled to appear`. The rule must not read any court story out."""
        for quote in (
            "Alex Saab appeared for a change of plea hearing in Miami on Tuesday",
            "The trial opened in Miami on Monday",
            "Protesters marched through Miami on Sunday",
        ):
            with self.subTest(quote=quote):
                self.assertIsNone(check_evidence(payload(quote, "Miami")))


class ShapeTests(unittest.TestCase):
    def test_a_missing_quote_or_name_is_not_this_check_to_refuse(self) -> None:
        """check_quote already rejects those, and two checks reporting one fault reads as two."""
        self.assertIsNone(check_evidence(payload("", "Aleppo")))
        self.assertIsNone(check_evidence(payload("Something happened.", "")))
        self.assertIsNone(check_evidence({"event_place": None}))
        self.assertIsNone(check_evidence({}))


class WiringTests(unittest.TestCase):
    """apply_checks must actually run it, and must not pin a story that fails it."""

    def run_checks(self, quote: str, name: str):
        from newsfeed.extract import apply_checks

        text = f"Reporting from the region. {quote} More text follows."
        built = payload(quote, name)
        built["event_date"] = "2026-09-17"
        built["category"] = "attack_or_violent_crime"
        return apply_checks(built, text, "2026-09-17T09:00:00Z", [])

    def test_a_failing_quote_becomes_world_news_with_the_reason(self) -> None:
        checked = self.run_checks("Yasser Salim told Reuters in Aleppo.", "Aleppo")
        self.assertFalse(checked.payload["is_physical_event"])
        self.assertIsNone(checked.payload["event_place"])
        self.assertIn("spoke to a reporter", checked.reason)
        self.assertIn("failed", checked.checks["evidence"])

    def test_a_good_quote_still_pins(self) -> None:
        quote = "A man was stabbed near Westminster Bridge in central London on Sunday"
        checked = self.run_checks(quote, "Westminster")
        self.assertTrue(checked.payload["is_physical_event"])
        self.assertEqual(checked.checks["evidence"], "passed")

    def test_the_check_is_recorded_as_skipped_when_the_quote_was_not_real(self) -> None:
        """A quote that is not in the text is not evidence, so reading it as evidence is noise."""
        from newsfeed.extract import apply_checks

        built = payload("A quote that is nowhere in the article, in Aleppo.", "Aleppo")
        built["event_date"] = "2026-09-17"
        checked = apply_checks(built, "Different text entirely.", "2026-09-17T09:00:00Z", [])
        self.assertIn("not applicable", checked.checks["evidence"])
        self.assertIn("word for word", checked.reason)


if __name__ == "__main__":
    unittest.main()
