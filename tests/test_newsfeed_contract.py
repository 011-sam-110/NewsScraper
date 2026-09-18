"""The section 8.1 validator, checked against the contract's own example and one mutation per rule.

The example is READ OUT of `docs/ARCHITECTURE.md` rather than copied here. A copy would drift: the
document could be edited and these tests would go on passing against the old wording, which is the
one failure a conformance test cannot afford. Reading it also means the document keeps a valid
example, since an edit that breaks it turns this file red.

Every rule gets a mutation. A validator nobody has watched go red is decoration, and a check that
cannot be made to fail is not a check.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import unittest

from newsfeed import contract

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md"


def load_example() -> dict:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"### 8\.2 Example.*?```json\n(.*?)\n```", text, re.S)
    if not match:
        raise AssertionError("section 8.2 no longer holds a JSON example")
    return json.loads(match.group(1))


EXAMPLE = load_example()


def mutate(**changes):
    """A copy of the example with top level fields replaced."""
    body = copy.deepcopy(EXAMPLE)
    body.update(changes)
    return body


def with_item(index: int, **changes) -> dict:
    body = copy.deepcopy(EXAMPLE)
    body["items"][index].update(changes)
    return body


def with_location(**changes) -> dict:
    body = copy.deepcopy(EXAMPLE)
    body["items"][0]["location"].update(changes)
    return body


def with_report(index: int, **changes) -> dict:
    body = copy.deepcopy(EXAMPLE)
    body["items"][0]["reports"][index].update(changes)
    return body


class TheContractsOwnExampleTests(unittest.TestCase):
    def test_the_example_in_section_8_2_is_valid(self):
        self.assertEqual(contract.validate(EXAMPLE), [])

    def test_the_example_carries_a_pin_and_a_world_news_item(self):
        # If it ever stopped carrying both, most of the mutations below would test nothing.
        located = [item for item in EXAMPLE["items"] if item["location"]]
        unlocated = [item for item in EXAMPLE["items"] if item["location"] is None]
        self.assertTrue(located, "the example has no pin to mutate")
        self.assertTrue(unlocated, "the example has no World news item to mutate")

    def test_a_body_that_is_not_an_object_is_refused(self):
        for body in ([], "snapshot", None, 7):
            self.assertEqual(contract.validate(body), ["the body is not a JSON object"])

    def test_the_validator_reports_every_problem_not_only_the_first(self):
        body = mutate(schema="wrong", snapshotId="nope", pinsWithheld="yes")
        self.assertGreaterEqual(len(contract.validate(body)), 3)


class MutationTests(unittest.TestCase):
    """One turn of the screw per rule in the 8.1 tables."""

    def assertRefused(self, body, fragment: str):
        problems = contract.validate(body)
        self.assertTrue(problems, f"the validator accepted a body that breaks: {fragment}")
        joined = " | ".join(problems)
        self.assertIn(fragment, joined, f"refused for the wrong reason: {joined}")

    # The body

    def test_the_schema_string_is_exact(self):
        self.assertRefused(mutate(schema="provenance.newsfeed/2"), "schema is")

    def test_the_snapshot_id_shape(self):
        for bad in ("snap_3f9c2a71d04b8e6", "snap_3F9C2A71D04B8E65", "3f9c2a71d04b8e65", 17):
            self.assertRefused(mutate(snapshotId=bad), "snapshotId is not snap_")

    def test_times_carry_a_z(self):
        self.assertRefused(mutate(generatedAt="2026-09-15T09:31:02+00:00"), "generatedAt is not")
        self.assertRefused(mutate(generatedAt="2026-09-15 09:31:02Z"), "generatedAt is not")

    def test_data_as_of_is_a_string_not_null(self):
        # 8.1 types it as a string with no "or null", so a store with no completed run has to say
        # something rather than send nothing.
        self.assertRefused(mutate(dataAsOf=None), "dataAsOf is not")

    def test_the_withheld_flags_are_booleans(self):
        self.assertRefused(mutate(pinsWithheld="false"), "pinsWithheld is not a boolean")
        self.assertRefused(mutate(worldNewsWithheld=0), "worldNewsWithheld is not a boolean")

    def test_a_missing_field_is_named(self):
        body = copy.deepcopy(EXAMPLE)
        del body["dataAsOf"]
        self.assertRefused(body, "the body is missing ['dataAsOf']")

    def test_a_field_8_1_does_not_name_is_refused(self):
        # The "never in the body" rule is enforced as a closed field list. Nothing mechanical can
        # tell that a string was written by a model, but it can tell that it has no business here.
        self.assertRefused(mutate(summary="a model wrote this"), "does not name: ['summary']")

    def test_every_outlet_appears_exactly_once(self):
        body = copy.deepcopy(EXAMPLE)
        body["outlets"] = [row for row in body["outlets"] if row["outlet"] != "pbs"]
        self.assertRefused(body, "outlets names pbs 0 times")

        body = copy.deepcopy(EXAMPLE)
        body["outlets"].append({"outlet": "bbc", "lastNewStoryAt": None})
        self.assertRefused(body, "outlets names bbc 2 times")

    def test_an_outlet_row_has_two_fields_and_a_known_outlet(self):
        body = copy.deepcopy(EXAMPLE)
        body["outlets"][0]["count"] = 4
        self.assertRefused(body, "outlets[0] fields are")

        body = copy.deepcopy(EXAMPLE)
        body["outlets"][0]["outlet"] = "ap"
        self.assertRefused(body, "outlets[0].outlet is 'ap'")

    def test_last_new_story_at_may_be_null_but_not_rubbish(self):
        body = copy.deepcopy(EXAMPLE)
        body["outlets"][3]["lastNewStoryAt"] = None
        self.assertEqual(contract.validate(body), [])

        body["outlets"][3]["lastNewStoryAt"] = "yesterday"
        self.assertRefused(body, "lastNewStoryAt is not UTC ISO 8601")

    def test_items_is_an_array_and_capped(self):
        self.assertRefused(mutate(items={}), "items is not an array")

        body = copy.deepcopy(EXAMPLE)
        seed = body["items"][1]
        body["items"] = [dict(seed, id="nf_%012x" % n) for n in range(contract.MAX_ITEMS + 1)]
        self.assertRefused(body, "over the 3000 cap")

    def test_two_items_may_not_share_an_id(self):
        body = copy.deepcopy(EXAMPLE)
        body["items"][1]["id"] = body["items"][0]["id"]
        self.assertRefused(body, "appears more than once")

    def test_withholding_means_the_items_are_not_sent(self):
        self.assertRefused(mutate(pinsWithheld=True), "still carries a located item")
        self.assertRefused(mutate(worldNewsWithheld=True), "still carries an unlocated item")

        body = mutate(pinsWithheld=True, worldNewsWithheld=True, items=[])
        self.assertEqual(contract.validate(body), [])

    # An item

    def test_the_item_id_shape(self):
        for bad in ("nf_8a41c09e2d7", "nf_8A41C09E2D7B", "8a41c09e2d7b", None):
            self.assertRefused(with_item(0, id=bad), "is not nf_ plus 12 hex")

    def test_the_category_is_one_of_the_v1_categories(self):
        self.assertRefused(with_item(0, category="shooting"), "not a v1 category")
        self.assertRefused(with_item(0, category=None), "not a v1 category")

    def test_the_title_is_present_and_capped(self):
        self.assertRefused(with_item(0, title="   "), "title is missing or empty")
        long = "x" * (contract.MAX_TITLE + 1)
        body = with_item(0, title=long)
        body["items"][0]["reports"][0]["headline"] = long  # keep the lead rule out of it
        self.assertRefused(body, "over 300")

    def test_an_item_cannot_be_first_reported_after_it_was_last_reported(self):
        self.assertRefused(
            with_item(1, firstReportedAt="2026-09-15T08:00:00Z"),
            "first reported after it was last reported",
        )

    def test_the_event_date_is_a_plain_day_or_null(self):
        self.assertRefused(with_item(0, eventDate="2026-09-14T00:00:00Z"), "neither null nor")
        self.assertRefused(with_item(0, eventDate="14/09/2026"), "neither null nor")
        self.assertEqual(contract.validate(with_item(0, eventDate=None)), [])

    def test_an_item_field_8_1_does_not_name_is_refused(self):
        self.assertRefused(with_item(0, authors=["a reporter"]), "does not name: ['authors']")
        self.assertRefused(with_item(0, description="the model's summary"), "['description']")

    def test_the_title_is_the_lead_reports_headline_verbatim(self):
        self.assertRefused(
            with_item(0, title="Man injured in stabbing near Westminster Bridge"),
            "not the lead report's headline",
        )

    def test_first_reported_at_is_when_the_lead_was_published(self):
        body = with_item(0, firstReportedAt="2026-09-14T18:19:00Z")
        self.assertRefused(body, "not when the lead report was published")

    # Reports

    def test_an_item_carries_between_one_and_eight_reports(self):
        self.assertRefused(with_item(0, reports=[]), "outside 1..8")

        body = copy.deepcopy(EXAMPLE)
        lead = body["items"][0]["reports"][0]
        body["items"][0]["reports"] = [lead] + [
            dict(lead, outlet="pbs", url=f"https://www.pbs.org/example{n}",
                 publishedAt="2026-09-14T20:00:00Z")
            for n in range(8)
        ]
        self.assertRefused(body, "outside 1..8")

    def test_the_lead_report_comes_first(self):
        body = copy.deepcopy(EXAMPLE)
        body["items"][0]["reports"].reverse()
        self.assertRefused(body, "not the earliest published report")

    def test_the_same_report_is_not_listed_twice(self):
        body = copy.deepcopy(EXAMPLE)
        body["items"][0]["reports"].append(copy.deepcopy(body["items"][0]["reports"][1]))
        self.assertRefused(body, "the same outlet and url twice")

    def test_a_report_names_one_of_the_five_outlets(self):
        self.assertRefused(with_report(0, outlet="ap"), "reports[0].outlet is 'ap'")
        self.assertRefused(with_report(0, outlet="BBC"), "reports[0].outlet is 'BBC'")

    def test_a_report_url_is_http_and_capped(self):
        self.assertRefused(with_report(1, url="ftp://example.invalid/story"), "not an http")
        self.assertRefused(with_report(1, url="/news/example"), "not an http")
        self.assertRefused(
            with_report(1, url="https://example.invalid/" + "x" * contract.MAX_URL), "over 600"
        )

    def test_a_report_headline_is_present_and_capped(self):
        self.assertRefused(with_report(1, headline=""), "headline is missing or empty")
        self.assertRefused(with_report(1, headline="x" * 301), "over 300")

    def test_a_report_published_at_is_utc_iso(self):
        self.assertRefused(with_report(1, publishedAt="2026-09-14"), "publishedAt is not UTC")

    def test_a_report_field_8_1_does_not_name_is_refused(self):
        self.assertRefused(with_report(1, byline="a reporter"), "reports[1] fields are")

    # location

    def test_the_coordinates_are_numbers_in_range(self):
        self.assertRefused(with_location(lat=91), "outside -90..90")
        self.assertRefused(with_location(lon=-180.5), "outside -180..180")
        self.assertRefused(with_location(lat="51.50086"), "lat is not a number")
        self.assertRefused(with_location(lat=True), "lat is not a number")

    def test_the_coordinates_carry_at_most_five_decimal_places(self):
        self.assertRefused(with_location(lat=51.500861), "over 5 decimals")
        self.assertEqual(contract.validate(with_location(lat=51.50086)), [])
        self.assertEqual(contract.validate(with_location(lat=51.0)), [])

    def test_only_three_precisions_may_be_published(self):
        for bad in ("region", "country", "POINT", None):
            self.assertRefused(with_location(precision=bad), "precision is")
        for good in contract.PRECISIONS:
            self.assertEqual(contract.validate(with_location(precision=good)), [])

    def test_the_place_is_present_and_capped(self):
        self.assertRefused(with_location(place=""), "place is missing or empty")
        self.assertRefused(with_location(place="x" * (contract.MAX_PLACE + 1)), "over 120")

    def test_the_geonames_id_is_a_positive_integer(self):
        for bad in (0, -1, "1000001", 1.5, True):
            self.assertRefused(with_location(geonamesId=bad), "not a positive integer")

    def test_a_pin_carries_a_quote_within_200_characters(self):
        self.assertRefused(with_location(evidence=None), "evidence is missing or empty")
        self.assertRefused(with_location(evidence="  "), "evidence is missing or empty")
        self.assertRefused(with_location(evidence="x" * (contract.MAX_EVIDENCE + 1)), "over 200")

    def test_the_quote_names_the_outlet_it_came_from(self):
        self.assertRefused(with_location(evidenceOutlet="ap"), "evidenceOutlet is 'ap'")

    def test_a_location_field_8_1_does_not_name_is_refused(self):
        self.assertRefused(with_location(confidence=0.9), "does not name: ['confidence']")

    def test_a_missing_location_field_is_named(self):
        body = copy.deepcopy(EXAMPLE)
        del body["items"][0]["location"]["evidence"]
        self.assertRefused(body, "location is missing ['evidence']")

    def test_a_location_that_is_not_an_object_is_refused(self):
        self.assertRefused(with_item(0, location="London"), "neither an object nor null")


class WindowTests(unittest.TestCase):
    """A pin stays 7 days from lastReportedAt, a World news item 72 hours."""

    def test_a_pin_older_than_seven_days_is_refused(self):
        body = copy.deepcopy(EXAMPLE)
        body["generatedAt"] = "2026-09-22T09:31:02Z"  # 7 days and 12 hours after the pin
        problems = contract.validate(body)
        self.assertTrue(any("outside its window" in p and "pin" in p for p in problems), problems)

    def test_a_pin_inside_seven_days_is_kept(self):
        body = copy.deepcopy(EXAMPLE)
        body["generatedAt"] = "2026-09-21T09:31:02Z"
        body["dataAsOf"] = "2026-09-21T09:05:40Z"
        body["items"] = [body["items"][0]]
        self.assertEqual(contract.validate(body), [])

    def test_a_world_news_item_older_than_seventy_two_hours_is_refused(self):
        body = copy.deepcopy(EXAMPLE)
        body["generatedAt"] = "2026-09-18T09:31:02Z"
        problems = contract.validate(body)
        self.assertTrue(any("World news item" in p for p in problems), problems)

    def test_the_windows_can_be_set_aside_without_setting_aside_the_shape(self):
        body = copy.deepcopy(EXAMPLE)
        body["generatedAt"] = "2026-10-15T09:31:02Z"
        self.assertTrue(contract.validate(body))
        self.assertEqual(contract.validate(body, check_windows=False), [])

        body["schema"] = "wrong"
        self.assertTrue(contract.validate(body, check_windows=False))


class DecimalCountingTests(unittest.TestCase):
    """The decimal rule counts the wire, not the binary float."""

    def test_it_counts_what_json_would_write(self):
        self.assertEqual(contract._decimals(51.50086), 5)
        self.assertEqual(contract._decimals(-0.12190), 4)
        self.assertEqual(contract._decimals(0.0), 1)
        self.assertEqual(contract._decimals(51), 0)

    def test_exponent_form_is_counted_not_refused(self):
        # repr(1e-05) is "1e-05". A string split on "." would call that zero decimals and let a
        # six place number through, or call it malformed and refuse a legal one.
        self.assertEqual(repr(1e-05), "1e-05")
        self.assertEqual(contract._decimals(1e-05), 5)
        self.assertEqual(contract._decimals(1e-06), 6)
        self.assertEqual(contract.validate(with_location(lon=1e-06))[0].count("over 5 decimals"), 1)


if __name__ == "__main__":
    unittest.main()
