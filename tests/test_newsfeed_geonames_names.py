"""The GeoNames place-NAME tables and the display string. Section 8.1 `location.place`.

Every row in the three fixtures is a real, unedited line from the files GeoNames publishes, taken
from the download on provenance-1 on 2026-09-18. That matters more here than usual: the whole point
of these tables is to turn a code into the words a reader sees, so a fixture written by hand would
be testing what I imagined the file says.

The fixtures were chosen to carry the cases that break a naive implementation:

- `FR.11` is `Ile-de-France` in the ASCII column and `Île-de-France` in the real one.
- `GE` is named `Georgia`, and so is the admin1 area `US.GA`, and so are two places in
  `allCountries.sample.txt`. Four different things called Georgia is not a contrived test.
- `GB.ENG.J8` is `Nottingham`, and the fixture hotel called Westminster sits in it, so the obvious
  assumption that a place called Westminster is in London is wrong in the sample data.
- `countryInfo.txt` is CRLF and carries 50 comment lines. The other two files are neither.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from newsfeed import geonames
from newsfeed.geonames import DISPLAY_MAX, Gazetteer, Place, parse_admin_areas, parse_countries

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

LONDON = 2643743
WESTMINSTER_LONDON_PPL = 2634341
WESTMINSTER_HOTEL = 6467820
WESTMINSTER_COLORADO = 5443910
PARIS_FRANCE = 2988507
PARIS_TEXAS = 4717560
TEXAS = 4736286
GEORGIA_STATE = 4197000
GEORGIA_COUNTRY = 614540


def lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def build_gazetteer() -> Gazetteer:
    connection = sqlite3.connect(":memory:")
    geonames.build_database(
        connection,
        lines("allCountries.sample.txt"),
        lines("alternateNamesV2.sample.txt"),
        lines("countryInfo.sample.txt"),
        lines("admin1Codes.sample.txt"),
        lines("admin2Codes.sample.txt"),
    )
    return Gazetteer(connection)


def place_by_id(gazetteer: Gazetteer, geonames_id: int) -> Place:
    """Read one row straight from the table, so a display test cannot fail on the resolver."""
    row = gazetteer.connection.execute(
        "SELECT geonames_id, name, latitude, longitude, feature_class, feature_code,"
        " country, admin1, admin2, population FROM places WHERE geonames_id = ?",
        (geonames_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"{geonames_id} is not in the fixture")
    return Place(*row)


class FixtureShapeTests(unittest.TestCase):
    """Guards the fixtures themselves. A fixture that quietly lost its shape proves nothing."""

    def test_country_info_is_still_crlf_with_its_comment_header(self) -> None:
        raw = (FIXTURES / "countryInfo.sample.txt").read_bytes()
        self.assertGreater(raw.count(b"\r\n"), 50, "git must not normalise this file; see .gitattributes")
        self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0, "no bare LF may creep in")
        self.assertTrue(raw.startswith(b"#"), "the real file opens with its comment header")

    def test_the_admin_files_are_not_crlf(self) -> None:
        """The three files do not agree, which is the reason both endings are stripped."""
        for name in ("admin1Codes.sample.txt", "admin2Codes.sample.txt"):
            with self.subTest(file=name):
                self.assertEqual((FIXTURES / name).read_bytes().count(b"\r\n"), 0)

    def test_the_admin1_fixture_really_carries_an_accent(self) -> None:
        """If this became ASCII, the column-1-not-column-2 test below would pass for free."""
        self.assertIn("Île-de-France", (FIXTURES / "admin1Codes.sample.txt").read_text(encoding="utf-8"))


class ParseCountryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = dict(parse_countries(lines("countryInfo.sample.txt")))

    def test_the_four_real_rows_parse_to_their_names(self) -> None:
        self.assertEqual(
            self.parsed,
            {"FR": "France", "GB": "United Kingdom", "GE": "Georgia", "US": "United States"},
        )

    def test_the_fifty_comment_lines_are_skipped(self) -> None:
        self.assertEqual(len(self.parsed), 4)

    def test_the_uk_is_named_united_kingdom_not_by_its_fips_code(self) -> None:
        """Column 3 of that row is `UK` and column 4 is the name. Reading column 3 would look fine."""
        self.assertEqual(self.parsed["GB"], "United Kingdom")

    def test_a_carriage_return_never_reaches_a_value(self) -> None:
        for code, name in self.parsed.items():
            with self.subTest(code=code):
                self.assertEqual(name, name.strip())
                self.assertNotIn("\r", name)

    def test_a_short_or_blank_line_is_skipped_rather_than_raising(self) -> None:
        rough = ["", "   ", "#comment", "XX\tXXX\t999", "ZZ\tZZZ\t000\tZZ\tZedland\tCapital"]
        self.assertEqual(dict(parse_countries(rough)), {"ZZ": "Zedland"})


class ParseAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.admin1 = {code: name for code, name, _ in parse_admin_areas(lines("admin1Codes.sample.txt"), 1)}
        self.admin2 = {code: name for code, name, _ in parse_admin_areas(lines("admin2Codes.sample.txt"), 2)}

    def test_the_real_admin1_rows_parse(self) -> None:
        self.assertEqual(self.admin1["GB.ENG"], "England")
        self.assertEqual(self.admin1["US.TX"], "Texas")
        self.assertEqual(self.admin1["US.GA"], "Georgia")

    def test_the_accented_name_is_kept_not_the_ascii_column(self) -> None:
        """Column 1 is the name, column 2 is its ASCII form. A reader wants the accents."""
        self.assertEqual(self.admin1["FR.11"], "Île-de-France")

    def test_the_real_admin2_rows_parse(self) -> None:
        self.assertEqual(self.admin2["GB.ENG.GLA"], "Greater London")
        self.assertEqual(self.admin2["US.TX.277"], "Lamar County")
        self.assertEqual(self.admin2["US.CO.001"], "Adams County")

    def test_the_level_is_recorded(self) -> None:
        for _, _, level in parse_admin_areas(lines("admin1Codes.sample.txt"), 1):
            self.assertEqual(level, 1)
        for _, _, level in parse_admin_areas(lines("admin2Codes.sample.txt"), 2):
            self.assertEqual(level, 2)

    def test_a_malformed_line_is_skipped(self) -> None:
        self.assertEqual(list(parse_admin_areas(["", "onlyonecolumn", "#c"], 1)), [])


class BuildTests(unittest.TestCase):
    def test_the_tables_are_filled_and_counted(self) -> None:
        gazetteer = build_gazetteer()
        counts = gazetteer.connection.execute(
            "SELECT (SELECT COUNT(*) FROM countries), (SELECT COUNT(*) FROM admin_areas)"
        ).fetchone()
        self.assertEqual(counts, (4, 10))

    def test_the_name_sources_are_required_arguments(self) -> None:
        """A build that omits them resolves every place correctly and displays bare codes.

        That is the silent kind of wrong, so it is a TypeError at the call site instead.
        """
        with self.assertRaises(TypeError):
            geonames.build_database(
                sqlite3.connect(":memory:"),
                lines("allCountries.sample.txt"),
                lines("alternateNamesV2.sample.txt"),
            )


class DisplayNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gazetteer = build_gazetteer()

    def display(self, geonames_id: int) -> str:
        return self.gazetteer.display_name(place_by_id(self.gazetteer, geonames_id))

    def test_a_district_reads_place_area_country(self) -> None:
        self.assertEqual(
            self.display(WESTMINSTER_LONDON_PPL), "City of Westminster, Greater London, United Kingdom"
        )

    def test_a_capital_city(self) -> None:
        self.assertEqual(self.display(LONDON), "London, Greater London, United Kingdom")

    def test_the_admin_area_is_the_real_one_not_the_expected_one(self) -> None:
        """The fixture hotel called Westminster is in Nottingham, not London. GeoNames says so."""
        self.assertEqual(self.display(WESTMINSTER_HOTEL), "Westminster, Nottingham, United Kingdom")

    def test_the_same_name_in_another_country(self) -> None:
        self.assertEqual(self.display(WESTMINSTER_COLORADO), "Westminster, Adams County, United States")

    def test_a_city_whose_admin_area_repeats_its_own_name_drops_the_repeat(self) -> None:
        """Paris sits in admin2 `Paris`. "Paris, Paris, France" reads like a bug."""
        self.assertEqual(self.display(PARIS_FRANCE), "Paris, France")

    def test_the_other_paris(self) -> None:
        self.assertEqual(self.display(PARIS_TEXAS), "Paris, Lamar County, United States")

    def test_a_region_does_not_repeat_itself(self) -> None:
        self.assertEqual(self.display(TEXAS), "Texas, United States")

    def test_a_state_called_georgia(self) -> None:
        self.assertEqual(self.display(GEORGIA_STATE), "Georgia, United States")

    def test_a_country_called_georgia_is_just_the_country(self) -> None:
        """Its admin1 is `00`, which is GeoNames for none, so `GE.00` finds nothing and costs a part.

        The country name then repeats the place name and is dropped, leaving one word. That is the
        right answer, and it is reached without a single special case for `00`.
        """
        self.assertEqual(self.display(GEORGIA_COUNTRY), "Georgia")

    def test_every_display_name_fits_the_contract_cap(self) -> None:
        for geonames_id in (
            LONDON, WESTMINSTER_LONDON_PPL, WESTMINSTER_HOTEL, WESTMINSTER_COLORADO,
            PARIS_FRANCE, PARIS_TEXAS, TEXAS, GEORGIA_STATE, GEORGIA_COUNTRY,
        ):
            with self.subTest(geonames_id=geonames_id):
                self.assertLessEqual(len(self.display(geonames_id)), DISPLAY_MAX)

    def test_no_display_name_is_empty_or_ends_in_a_separator(self) -> None:
        for geonames_id in (LONDON, PARIS_FRANCE, GEORGIA_COUNTRY):
            with self.subTest(geonames_id=geonames_id):
                built = self.display(geonames_id)
                self.assertTrue(built)
                self.assertFalse(built.endswith(",") or built.endswith(" "))


class DisplayLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gazetteer = build_gazetteer()

    def test_an_unknown_country_code_returns_none_rather_than_guessing(self) -> None:
        self.assertIsNone(self.gazetteer.country_name("ZZ"))
        self.assertIsNone(self.gazetteer.country_name(None))
        self.assertIsNone(self.gazetteer.country_name(""))

    def test_a_partial_admin_key_is_not_built_with_a_hole_in_it(self) -> None:
        """`GB..GLA` would never match, and would never say why either."""
        self.assertIsNone(self.gazetteer.admin_name("GB", None, "GLA"))
        self.assertIsNone(self.gazetteer.admin_name("GB", "", "GLA"))
        self.assertEqual(self.gazetteer.admin_name("GB", "ENG", "GLA"), "Greater London")
        self.assertEqual(self.gazetteer.admin_name("GB", "ENG"), "England")

    def test_a_place_with_no_admin_row_still_gets_place_and_country(self) -> None:
        """A miss costs a part, never correctness. This is the shape of every unmapped code."""
        unmapped = Place(
            geonames_id=-1, name="Somewhere", latitude=0.0, longitude=0.0,
            feature_class="P", feature_code="PPL", country="GB",
            admin1="ZZZ", admin2="ZZZ", population=0,
        )
        self.assertEqual(self.gazetteer.display_name(unmapped), "Somewhere, United Kingdom")

    def test_a_place_in_an_unknown_country_is_still_named(self) -> None:
        orphan = Place(
            geonames_id=-2, name="Somewhere", latitude=0.0, longitude=0.0,
            feature_class="P", feature_code="PPL", country="ZZ",
            admin1=None, admin2=None, population=0,
        )
        self.assertEqual(self.gazetteer.display_name(orphan), "Somewhere")

    def test_an_over_long_name_drops_the_middle_before_it_cuts_a_word(self) -> None:
        long_place = Place(
            geonames_id=-3, name="X" * (DISPLAY_MAX - 20), latitude=0.0, longitude=0.0,
            feature_class="P", feature_code="PPL", country="GB",
            admin1="ENG", admin2="GLA", population=0,
        )
        built = self.gazetteer.display_name(long_place)
        self.assertLessEqual(len(built), DISPLAY_MAX)
        self.assertNotIn("Greater London", built)
        self.assertTrue(built.endswith("United Kingdom"), "place and country still locate the pin")


if __name__ == "__main__":
    unittest.main()
