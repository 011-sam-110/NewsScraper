"""Resolve and the gazetteer, against real GeoNames rows.

The four golden tests section 7.7 requires before M6 ends are the GoldenTests class. The rows in
tests/fixtures/geonames/ are unedited lines from the real GeoNames dumps for exactly the places
those tests name, so a rule that passes here passes against the bytes the gazetteer is built from.
GeoNames data is CC BY 4.0.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import geonames, resolve  # noqa: E402
from newsfeed.geonames import Gazetteer  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

# Ids of the fixture rows, so a test says which place it means rather than repeating a name that
# several places share, which is the whole subject of this module.
LONDON = 2643743  # P PPLC GB
WESTMINSTER_LONDON_PPL = 2634341  # P PPLA3 GB, alternate name Westminster
WESTMINSTER_LONDON_ADM = 3333218  # A ADM3 GB, alternate name Westminster
WESTMINSTER_HOTEL = 6467820  # S HTL GB, and it is in Nottingham, not London
WESTMINSTER_COLORADO = 5443910  # P PPL US CO
PARIS_FRANCE = 2988507  # P PPLC FR
PARIS_TEXAS = 4717560  # P PPLA2 US TX
TEXAS = 4736286  # A ADM1 US TX
GEORGIA_STATE = 4197000  # A ADM1 US GA
GEORGIA_COUNTRY = 614540  # A PCLI GE


def build_gazetteer() -> Gazetteer:
    connection = sqlite3.connect(":memory:")
    places = (FIXTURES / "allCountries.sample.txt").read_text(encoding="utf-8").splitlines()
    alternates = (FIXTURES / "alternateNamesV2.sample.txt").read_text(encoding="utf-8").splitlines()
    geonames.build_database(connection, places, alternates)
    return Gazetteer(connection)


class FixtureTests(unittest.TestCase):
    def test_every_named_place_is_in_the_fixture(self) -> None:
        # A golden test that silently lost its data would pass by resolving nothing.
        gazetteer = build_gazetteer()
        self.addCleanup(gazetteer.close)
        held = {row[0] for row in gazetteer.connection.execute("SELECT geonames_id FROM places")}
        self.assertEqual(
            held,
            {
                LONDON,
                WESTMINSTER_LONDON_PPL,
                WESTMINSTER_LONDON_ADM,
                WESTMINSTER_HOTEL,
                WESTMINSTER_COLORADO,
                PARIS_FRANCE,
                PARIS_TEXAS,
                TEXAS,
                GEORGIA_STATE,
                GEORGIA_COUNTRY,
            },
        )

    def test_westminster_reaches_the_london_district_only_through_an_alternate_name(self) -> None:
        # The London district is called "City of Westminster". If the alternate-names table were
        # dropped, golden test one would resolve to a hotel in Nottingham and still look green
        # unless something asserts why the match exists.
        gazetteer = build_gazetteer()
        self.addCleanup(gazetteer.close)
        sources = {
            row[0]
            for row in gazetteer.connection.execute(
                "SELECT source FROM names WHERE folded = 'westminster' AND geonames_id = ?",
                (WESTMINSTER_LONDON_ADM,),
            )
        }
        self.assertEqual(sources, {"alternate"})


class GoldenTests(unittest.TestCase):
    """The four checks section 7.7 requires before M6 can end."""

    def setUp(self) -> None:
        self.gazetteer = build_gazetteer()
        self.addCleanup(self.gazetteer.close)

    def test_westminster_within_london_is_the_london_district(self) -> None:
        result = resolve.resolve_place(
            self.gazetteer, "Westminster", within="London", country="GB", kind="district"
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.place.geonames_id, WESTMINSTER_LONDON_ADM)
        self.assertEqual(result.precision, "district")
        self.assertTrue(result.pinnable)

    def test_westminster_in_gb_is_never_the_colorado_one(self) -> None:
        result = resolve.resolve_place(
            self.gazetteer, "Westminster", within="London", country="GB", kind="district"
        )
        self.assertNotEqual(result.place.geonames_id, WESTMINSTER_COLORADO)

    def test_westminster_within_london_is_not_the_nottingham_hotel(self) -> None:
        # The hotel is an S row, so it is the best possible fit for a venue and would win on fit
        # alone. Only the distance gate against London keeps it out.
        result = resolve.resolve_place(
            self.gazetteer, "Westminster", within="London", country="GB", kind="venue"
        )
        self.assertNotEqual(result.place.geonames_id, WESTMINSTER_HOTEL)

    def test_paris_in_france_is_the_capital(self) -> None:
        result = resolve.resolve_place(self.gazetteer, "Paris", country="FR", kind="city")
        self.assertEqual(result.place.geonames_id, PARIS_FRANCE)
        self.assertEqual(result.precision, "city")
        self.assertTrue(result.pinnable)

    def test_paris_within_texas_is_paris_texas(self) -> None:
        result = resolve.resolve_place(
            self.gazetteer, "Paris", within="Texas", country="US", kind="city"
        )
        self.assertEqual(result.place.geonames_id, PARIS_TEXAS)
        self.assertEqual(result.precision, "city")
        self.assertTrue(result.pinnable)

    def test_georgia_the_country_is_not_pinned(self) -> None:
        result = resolve.resolve_place(self.gazetteer, "Georgia", country="GE", kind="country")
        self.assertEqual(result.place.geonames_id, GEORGIA_COUNTRY)
        self.assertEqual(result.precision, "country")
        self.assertFalse(result.pinnable)

    def test_georgia_the_state_is_not_pinned(self) -> None:
        result = resolve.resolve_place(self.gazetteer, "Georgia", country="US", kind="region")
        self.assertEqual(result.place.geonames_id, GEORGIA_STATE)
        self.assertEqual(result.precision, "region")
        self.assertFalse(result.pinnable)

    def test_a_street_with_no_entry_climbs_to_its_city(self) -> None:
        result = resolve.resolve_place(
            self.gazetteer,
            "Acacia Avenue",
            within="London",
            country="GB",
            kind="street",
        )
        self.assertTrue(result.climbed)
        self.assertEqual(result.place.geonames_id, LONDON)
        self.assertEqual(result.precision, "city")
        self.assertTrue(result.pinnable)


class ContainmentTests(unittest.TestCase):
    """The admin-code rule, which replaced a radius that threw away a golden test."""

    def setUp(self) -> None:
        self.gazetteer = build_gazetteer()
        self.addCleanup(self.gazetteer.close)

    def places(self) -> dict[int, geonames.Place]:
        found = {}
        for name, country in (("Paris", "US"), ("Texas", "US"), ("London", "GB")):
            for place in self.gazetteer.candidates(name, country):
                found[place.geonames_id] = place
        return found

    def test_paris_texas_is_too_far_from_the_texas_centroid_for_the_radius_rule(self) -> None:
        # This is the measurement that made the radius rule wrong, kept as a test so nobody
        # reinstates it. Texas is about 1,200 km across.
        places = self.places()
        gap = resolve.distance_km(
            places[PARIS_TEXAS].latitude,
            places[PARIS_TEXAS].longitude,
            places[TEXAS].latitude,
            places[TEXAS].longitude,
        )
        self.assertGreater(gap, resolve.REGION_RADIUS_KM)

    def test_and_the_admin_code_still_contains_it(self) -> None:
        places = self.places()
        self.assertTrue(resolve._inside(places[PARIS_TEXAS], places[TEXAS]))

    def test_a_city_container_still_uses_distance(self) -> None:
        places = self.places()
        london = places[LONDON]
        far = self.gazetteer.candidates("Westminster", "GB")
        hotel = next(p for p in far if p.geonames_id == WESTMINSTER_HOTEL)
        district = next(p for p in far if p.geonames_id == WESTMINSTER_LONDON_ADM)
        self.assertFalse(resolve._inside(hotel, london))
        self.assertTrue(resolve._inside(district, london))


class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gazetteer = build_gazetteer()
        self.addCleanup(self.gazetteer.close)

    def test_no_country_is_refused_rather_than_searched_worldwide(self) -> None:
        result = resolve.resolve_place(self.gazetteer, "Paris", kind="city")
        self.assertFalse(result.resolved)
        self.assertIn("country", result.reason)

    def test_an_empty_name_resolves_to_nothing(self) -> None:
        self.assertFalse(resolve.resolve_place(self.gazetteer, "  ", country="FR").resolved)

    def test_a_name_that_matches_nothing_and_has_no_container_is_unresolved(self) -> None:
        result = resolve.resolve_place(self.gazetteer, "Atlantis", country="FR", kind="city")
        self.assertFalse(result.resolved)
        self.assertFalse(result.pinnable)


class FoldingTests(unittest.TestCase):
    def test_accents_and_case_fold_together(self) -> None:
        self.assertEqual(geonames.fold("Odesa"), geonames.fold("ODESA"))
        self.assertEqual(geonames.fold("Zürich"), "zurich")
        self.assertEqual(geonames.fold("  Sao   Paulo "), "sao paulo")


class ParsingTests(unittest.TestCase):
    def test_only_the_kept_classes_survive(self) -> None:
        rows = list(
            geonames.parse_places(
                [
                    "1\tRoad\tRoad\t\t10.0\t20.0\tR\tRD\tGB\t\t\t\t\t\t0",
                    "2\tTown\tTown\t\t10.0\t20.0\tP\tPPL\tGB\t\t\t\t\t\t5",
                ]
            )
        )
        self.assertEqual([row[0] for row in rows], [2])

    def test_a_row_with_an_unparseable_coordinate_is_dropped_not_zeroed(self) -> None:
        rows = list(
            geonames.parse_places(["3\tNowhere\tNowhere\t\tnorth\t20.0\tP\tPPL\tGB\t\t\t\t\t\t0"])
        )
        self.assertEqual(rows, [])

    def test_historic_and_link_alternates_are_dropped(self) -> None:
        rows = list(
            geonames.parse_alternate_names(
                [
                    "1\t100\ten\tConstantinople\t\t\t\t1",
                    "2\t100\tlink\thttps://example.org\t",
                    "3\t100\ten\tIstanbul\t1",
                    "4\t100\tfr\tIstanbul-sur-Mer\t",
                ]
            )
        )
        self.assertEqual([row[1] for row in rows], [100])
        self.assertEqual([row[0] for row in rows], ["istanbul"])


class PrecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gazetteer = build_gazetteer()
        self.addCleanup(self.gazetteer.close)

    def test_an_s_class_row_is_a_point(self) -> None:
        hotel = next(
            p for p in self.gazetteer.candidates("Westminster", "GB") if p.geonames_id == WESTMINSTER_HOTEL
        )
        self.assertEqual(hotel.precision, "point")
        self.assertTrue(hotel.pinnable)

    def test_region_and_country_are_never_pinnable(self) -> None:
        texas = next(p for p in self.gazetteer.candidates("Texas", "US"))
        georgia = next(p for p in self.gazetteer.candidates("Georgia", "GE"))
        self.assertEqual((texas.precision, texas.pinnable), ("region", False))
        self.assertEqual((georgia.precision, georgia.pinnable), ("country", False))


if __name__ == "__main__":
    unittest.main()
