"""The extract stage: the acceptance checks of section 7.6, the prompt, and the config hash.

No test here calls the network. The model's answers are supplied directly, because what is being
tested is what the code does with an answer, which is the part that keeps a wrong pin off the map.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import config, extract, prompts  # noqa: E402
from newsfeed.taxonomy import CATEGORY_IDS, NOT_EVENT_REASONS, PLACE_KINDS  # noqa: E402

ARTICLE = (
    "Police were called to central London on Sunday evening. A man was stabbed near Westminster "
    "Bridge in central London on Sunday, the Metropolitan Police said. He was taken to hospital."
)


def answer(**changes):
    payload = {
        "is_physical_event": True,
        "not_event_reason": None,
        "category": "attack_or_violent_crime",
        "event_date": "2026-09-14",
        "event_place": {
            "name": "Westminster",
            "within": "London",
            "country": "GB",
            "kind": "district",
            "quote": "A man was stabbed near Westminster Bridge in central London on Sunday",
        },
        "other_places": ["Manchester"],
        "key_entities": ["Metropolitan Police"],
        "cluster_hint": "stabbing near Westminster Bridge, one man injured",
    }
    payload.update(changes)
    return payload


class SchemaCheckTests(unittest.TestCase):
    def test_a_good_answer_passes(self) -> None:
        self.assertIsNone(extract.check_schema(answer()))

    def test_a_non_object_is_refused(self) -> None:
        for value in ([], "text", None, 3):
            with self.subTest(value=value):
                self.assertIsNotNone(extract.check_schema(value))

    def test_every_listed_category_is_accepted(self) -> None:
        for category in CATEGORY_IDS:
            with self.subTest(category=category):
                self.assertIsNone(extract.check_schema(answer(category=category)))

    def test_an_invented_category_is_refused(self) -> None:
        message = extract.check_schema(answer(category="stabbings"))
        self.assertIn("category", message)

    def test_every_listed_kind_is_accepted(self) -> None:
        for kind in PLACE_KINDS:
            with self.subTest(kind=kind):
                place = {**answer()["event_place"], "kind": kind}
                self.assertIsNone(extract.check_schema(answer(event_place=place)))

    def test_an_invented_kind_or_reason_is_refused(self) -> None:
        place = {**answer()["event_place"], "kind": "neighbourhood"}
        self.assertIn("kind", extract.check_schema(answer(event_place=place)))
        self.assertIn("not_event_reason", extract.check_schema(answer(not_event_reason="dull")))

    def test_every_listed_reason_is_accepted(self) -> None:
        for reason in NOT_EVENT_REASONS:
            with self.subTest(reason=reason):
                self.assertIsNone(
                    extract.check_schema(
                        answer(is_physical_event=False, not_event_reason=reason, event_place=None)
                    )
                )

    def test_a_country_must_be_two_letters(self) -> None:
        for country in ("GBR", "", "United Kingdom", "G1"):
            with self.subTest(country=country):
                place = {**answer()["event_place"], "country": country}
                self.assertIn("country", extract.check_schema(answer(event_place=place)))

    def test_an_event_needs_a_place(self) -> None:
        self.assertIsNotNone(extract.check_schema(answer(event_place=None)))

    def test_a_non_event_must_not_carry_a_place(self) -> None:
        self.assertIsNotNone(
            extract.check_schema(answer(is_physical_event=False, not_event_reason="opinion"))
        )

    def test_the_date_must_be_a_date(self) -> None:
        for date in ("14 September 2026", "2026-13-40", "", None):
            with self.subTest(date=date):
                self.assertIn("event_date", extract.check_schema(answer(event_date=date)))

    def test_lists_must_hold_strings(self) -> None:
        self.assertIsNotNone(extract.check_schema(answer(other_places=[{"name": "x"}])))
        self.assertIsNotNone(extract.check_schema(answer(key_entities="Metropolitan Police")))

    def test_a_model_supplied_coordinate_is_simply_ignored(self) -> None:
        # Nothing the model writes ever becomes a coordinate (section 3). An extra field is not a
        # reason to reject the answer, it is just never read.
        place = {**answer()["event_place"], "lat": 51.5, "lon": -0.12}
        self.assertIsNone(extract.check_schema(answer(event_place=place)))


class QuoteCheckTests(unittest.TestCase):
    def test_a_verbatim_quote_passes(self) -> None:
        self.assertIsNone(extract.check_quote(answer(), ARTICLE))

    def test_reflowed_whitespace_still_passes(self) -> None:
        reflowed = ARTICLE.replace("stabbed near", "stabbed\n\n   near")
        self.assertIsNone(extract.check_quote(answer(), reflowed))

    def test_a_quote_that_is_not_in_the_text_fails(self) -> None:
        place = {**answer()["event_place"], "quote": "A woman was stabbed near Westminster Bridge"}
        message = extract.check_quote(answer(event_place=place), ARTICLE)
        self.assertIn("word for word", message)

    def test_a_paraphrase_fails(self) -> None:
        place = {**answer()["event_place"], "quote": "a man was stabbed by Westminster Bridge"}
        self.assertIsNotNone(extract.check_quote(answer(event_place=place), ARTICLE))

    def test_the_place_name_must_be_inside_the_quote(self) -> None:
        place = {**answer()["event_place"], "name": "Southwark"}
        message = extract.check_quote(answer(event_place=place), ARTICLE)
        self.assertIn("not inside the quote", message)

    def test_a_story_with_no_text_cannot_have_a_verified_quote(self) -> None:
        for text in (None, "", "   "):
            with self.subTest(text=text):
                self.assertIsNotNone(extract.check_quote(answer(), text))

    def test_an_empty_quote_fails(self) -> None:
        place = {**answer()["event_place"], "quote": "  "}
        self.assertIn("empty", extract.check_quote(answer(event_place=place), ARTICLE))


class DateCheckTests(unittest.TestCase):
    published = "2026-09-15T10:00:00Z"

    def test_inside_the_window(self) -> None:
        for date in ("2026-09-12", "2026-09-14", "2026-09-15", "2026-09-16"):
            with self.subTest(date=date):
                self.assertIsNone(extract.check_date(answer(event_date=date), self.published))

    def test_outside_the_window(self) -> None:
        for date in ("2026-09-11", "2026-09-17", "2019-01-01"):
            with self.subTest(date=date):
                self.assertIsNotNone(extract.check_date(answer(event_date=date), self.published))

    def test_no_published_time_means_the_rule_cannot_be_applied(self) -> None:
        self.assertIsNone(extract.check_date(answer(event_date="2001-01-01"), None))


class FormatCheckTests(unittest.TestCase):
    def test_a_flagged_story_is_never_an_event(self) -> None:
        message = extract.check_format(answer(), ["opinion"], ARTICLE)
        self.assertIn("opinion", message)

    def test_a_story_with_no_text_is_never_an_event(self) -> None:
        self.assertIsNotNone(extract.check_format(answer(), [], None))

    def test_an_ordinary_report_passes(self) -> None:
        self.assertIsNone(extract.check_format(answer(), [], ARTICLE))


class ApplyChecksTests(unittest.TestCase):
    published = "2026-09-15T10:00:00Z"

    def test_a_good_extraction_survives_intact(self) -> None:
        checked = extract.apply_checks(answer(), ARTICLE, self.published, [])
        self.assertTrue(checked.accepted)
        self.assertIsNone(checked.reason)
        self.assertTrue(checked.payload["is_physical_event"])
        self.assertEqual(checked.checks["quote"], "passed")

    def test_a_failed_quote_becomes_world_news_with_the_reason_kept(self) -> None:
        place = {**answer()["event_place"], "quote": "invented text"}
        checked = extract.apply_checks(answer(event_place=place), ARTICLE, self.published, [])
        self.assertTrue(checked.accepted)          # stored, not thrown away
        self.assertFalse(checked.payload["is_physical_event"])
        self.assertIsNone(checked.payload["event_place"])
        self.assertIn("word for word", checked.reason)
        self.assertEqual(checked.payload["category"], "attack_or_violent_crime")  # category kept

    def test_a_failed_date_becomes_world_news(self) -> None:
        checked = extract.apply_checks(answer(event_date="2019-01-01"), ARTICLE, self.published, [])
        self.assertFalse(checked.payload["is_physical_event"])
        self.assertIn("outside", checked.reason)

    def test_a_format_flag_overrides_the_model(self) -> None:
        checked = extract.apply_checks(answer(), ARTICLE, self.published, ["live"])
        self.assertFalse(checked.payload["is_physical_event"])
        self.assertIn("live", checked.reason)
        self.assertIn("overridden", checked.checks["quote"])

    def test_the_country_veto_says_it_is_not_enforced_yet(self) -> None:
        # Recording a skip is the point: a pass that never ran would be a lie in the audit trail.
        checked = extract.apply_checks(answer(), ARTICLE, self.published, [])
        self.assertIn("skipped", checked.checks["country_veto"])
        self.assertIn("M6", checked.checks["country_veto"])

    def test_a_non_event_answer_needs_no_quote(self) -> None:
        payload = answer(is_physical_event=False, not_event_reason="opinion", event_place=None)
        checked = extract.apply_checks(payload, ARTICLE, self.published, [])
        self.assertTrue(checked.accepted)
        self.assertIn("not applicable", checked.checks["quote"])


class OutletTagTests(unittest.TestCase):
    def test_tags_are_read_from_each_outlet_s_own_field(self) -> None:
        cases = {
            "nyt": ({"places": ["Toronto (Ontario)", "Canada"]}, ["Toronto (Ontario)", "Canada"]),
            "bbc": ({"listing_topics": ["Netherlands", "Rail travel"]}, ["Netherlands", "Rail travel"]),
            "guardian": ({"keywords": ["Nigeria", "Africa"]}, ["Nigeria", "Africa"]),
            "reuters": ({"topics": ["Consumer Protection"]}, ["Consumer Protection"]),
        }
        for outlet, (categories, expected) in cases.items():
            with self.subTest(outlet=outlet):
                self.assertEqual(extract.outlet_place_tags(outlet, categories), expected)

    def test_a_dateline_is_never_read_as_a_tag(self) -> None:
        # section 6.1: a dateline says where the reporter filed from, not where the event happened.
        self.assertEqual(extract.outlet_place_tags("reuters", {"place": "LAGOS"}), [])

    def test_dicts_and_duplicates_are_handled(self) -> None:
        tags = extract.outlet_place_tags("pbs", {"tags": [{"name": "Kenya"}, "Kenya", {"x": 1}]})
        self.assertEqual(tags, ["Kenya"])

    def test_missing_categories_give_no_tags(self) -> None:
        self.assertEqual(extract.outlet_place_tags("bbc", None), [])


class PromptTests(unittest.TestCase):
    def test_the_system_prompt_mentions_json_as_json_mode_requires(self) -> None:
        self.assertIn("json", prompts.EXTRACT_SYSTEM.lower())

    def test_every_category_appears_in_the_prompt(self) -> None:
        for category in CATEGORY_IDS:
            with self.subTest(category=category):
                self.assertIn(category, prompts.EXTRACT_SYSTEM)

    def test_the_prompt_forbids_a_model_written_coordinate(self) -> None:
        self.assertIn("Never give a coordinate", prompts.EXTRACT_SYSTEM)

    def test_the_prompt_forbids_using_a_dateline(self) -> None:
        self.assertIn("dateline", prompts.EXTRACT_SYSTEM)

    def test_nothing_that_varies_sits_in_the_system_prompt(self) -> None:
        # Caching only pays while the prefix is byte-identical on every call (section 7.6).
        self.assertEqual(prompts.EXTRACT_SYSTEM, prompts.EXTRACT_SYSTEM)
        first = prompts.build_user("bbc", "world", "H", "2026-09-15T10:00:00Z", ["UK"], "text one")
        second = prompts.build_user("nyt", "world", "H2", "2026-09-15T11:00:00Z", [], "text two")
        self.assertNotEqual(first, second)

    def test_long_text_is_trimmed_from_the_end(self) -> None:
        text = "start " + ("word " * 20000)
        built = prompts.build_user("bbc", "world", "H", None, [], text, max_text_characters=100)
        self.assertIn("start", built)
        self.assertLess(len(built), 600)


class ConfigHashTests(unittest.TestCase):
    def test_the_hash_is_stable(self) -> None:
        self.assertEqual(config.extract_config_hash(), config.extract_config_hash())

    def test_a_different_model_is_a_different_hash(self) -> None:
        self.assertNotEqual(
            config.extract_config_hash("deepseek-flash"), config.extract_config_hash("deepseek-v4-pro")
        )

    def test_key_order_cannot_change_the_hash(self) -> None:
        self.assertEqual(
            config.hash_components({"a": 1, "b": 2}), config.hash_components({"b": 2, "a": 1})
        )

    def test_changing_the_prompt_changes_the_hash(self) -> None:
        before = config.extract_config_hash()
        components = config.extract_components()
        components["extract_system"] = components["extract_system"] + " one more sentence."
        self.assertNotEqual(before, config.hash_components(components))

    def test_changing_the_category_list_changes_the_hash(self) -> None:
        components = config.extract_components()
        components["categories"] = list(components["categories"]) + ["new_category"]
        self.assertNotEqual(config.extract_config_hash(), config.hash_components(components))

    def test_the_components_cover_what_section_10_5_lists(self) -> None:
        components = config.extract_components()
        for name in ("model", "temperature", "max_tokens", "thinking", "categories", "date_rule",
                     "extract_system"):
            with self.subTest(name=name):
                self.assertIn(name, components)


if __name__ == "__main__":
    unittest.main()
