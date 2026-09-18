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
MONTE_CARLO = 2992741


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

    def test_the_five_real_rows_parse_to_their_names(self) -> None:
        self.assertEqual(
            self.parsed,
            {
                "FR": "France",
                "GB": "United Kingdom",
                "GE": "Georgia",
                "MC": "Monaco",
                "US": "United States",
            },
        )

    def test_the_fifty_comment_lines_are_skipped(self) -> None:
        self.assertEqual(len(self.parsed), 5)

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

    def test_the_code_00_is_a_real_area_in_monaco_and_is_not_a_none_marker(self) -> None:
        """`00` means "no admin1" in most countries and names a real one in Monaco.

        Measured on the full 2026-09-18 download: 3,865 admin1 rows, and exactly one of them has
        `00` as its code. So a rule like "skip admin1 when it is 00" is a guess about what the code
        means, and it is wrong for the 49 Monaco places that carry it. Asking the table is right in
        both cases: GB.00 is absent and simply does not match, MC.00 is present and names the place.
        """
        self.assertEqual(self.admin1["MC.00"], "Municipality of Monaco")

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
        # 5 countries, then 6 admin1 rows and 5 admin2 rows.
        self.assertEqual(counts, (5, 11))

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


class SurveyedShapesTests(unittest.TestCase):
    """Cases taken from a full scan of the real 2026-09-18 download, not from imagination.

    13,472,129 place rows and all 3,865 admin1 and 47,643 admin2 rows were read to find the shapes
    that actually occur. Each test below is one of them. The scan was run by a second agent on
    provenance-1 and the counts are quoted where they decide something.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.gazetteer = build_gazetteer()

    def place(self, **kwargs: object) -> Place:
        base = dict(
            geonames_id=-99, name="Somewhere", latitude=0.0, longitude=0.0,
            feature_class="P", feature_code="PPL", country="GB",
            admin1=None, admin2=None, population=0,
        )
        base.update(kwargs)
        return Place(**base)  # type: ignore[arg-type]

    def test_monaco_uses_the_admin1_code_00_as_a_real_area(self) -> None:
        """49 Monaco places carry admin1 `00`, and `MC.00` is a named row.

        The tempting rule is "treat 00 as absent", which is right for the 364 GB, 20 US, 584 ES and
        628 UA places that carry it and wrong for these 49. No rule is needed: the table answers.
        """
        monte_carlo = place_by_id(self.gazetteer, MONTE_CARLO)
        self.assertEqual(monte_carlo.admin1, "00")
        self.assertEqual(
            self.gazetteer.display_name(monte_carlo), "Monte-Carlo, Municipality of Monaco, Monaco"
        )

    def test_the_code_00_is_dropped_where_no_row_defines_it(self) -> None:
        """The same code, the other country. `GB.00` is absent from all 3,865 admin1 rows."""
        self.assertIsNone(self.gazetteer.admin_name("GB", "00"))
        self.assertEqual(
            self.gazetteer.display_name(self.place(name="Anywhere", admin1="00")),
            "Anywhere, United Kingdom",
        )

    def test_an_empty_admin1_with_a_real_admin2_is_not_keyed_at_all(self) -> None:
        """`US..037` and `FR..64` both occur. A 3-part key built from them has a hole in the middle.

        291 US and 210 FR rows have an empty admin1, and some of those still carry an admin2. The
        empty check has to come before the lookup, or the key becomes `US..037`, which matches
        nothing and hides the reason.
        """
        self.assertIsNone(self.gazetteer.admin_name("US", "", "037"))
        self.assertEqual(
            self.gazetteer.display_name(self.place(name="Anywhere", country="US", admin1="", admin2="037")),
            "Anywhere, United States",
        )

    def test_an_admin2_that_is_prose_rather_than_a_code_is_dropped(self) -> None:
        """Real RU values include `NOVAYA ZEMLYA` and `Nozhay-Yurtovskiy Rayon and Gumbetovskiy
        Rayon` in the admin2 FIELD of a place row, where every one of the 47,643 admin2 rows is a
        code with no space in it. So these can only miss, and a miss costs the part.
        """
        prose = self.place(name="Anywhere", country="RU", admin1="06", admin2="NOVAYA ZEMLYA")
        self.assertEqual(self.gazetteer.display_name(prose), "Anywhere")

    def test_an_admin2_code_that_exists_under_a_different_admin1_is_not_recovered(self) -> None:
        """All 48 of the ES admin2 orphans exist in the table under another admin1. Ignoring the
        admin1 part would recover every one of them, and would name a province the place is not in.

        That is the GDELT failure this project exists to avoid: a plausible label for the wrong
        place. Section 7.7 already refuses to lean on admin codes across countries for the same
        reason. The fallback stays "drop the part".
        """
        # GB.ENG.GLA is a real row. The same last part under a different admin1 must not find it.
        self.assertIsNone(self.gazetteer.admin_name("GB", "WLS", "GLA"))
        wrong_region = self.place(name="Anywhere", admin1="WLS", admin2="GLA")
        self.assertEqual(self.gazetteer.display_name(wrong_region), "Anywhere, United Kingdom")

    def test_a_name_holding_a_comma_is_never_edited(self) -> None:
        """6 admin2 names and one country name contain a comma, and so do 2,601 US place names.

        `Cartwright, Labrador` is the name. Joining the parts with ", " makes the result ambiguous
        to anything that tries to split it back, and nothing does: section 8.1 `location.place` is
        prose rendered into a dossier. Trimming a name to remove its comma would be inventing a
        place name, which section 3 forbids more strongly than it dislikes an extra comma.
        """
        self.assertEqual(
            self.gazetteer.display_name(
                self.place(name="Cartwright, Labrador", country="GB", admin1="ENG")
            ),
            "Cartwright, Labrador, England, United Kingdom",
        )

    def test_the_trailing_space_in_a_country_name_is_trimmed(self) -> None:
        """The BQ row is `Bonaire, Saint Eustatius and Saba ` in the source, with the space."""
        self.assertEqual(
            dict(parse_countries(["BQ\tBES\t535\t\tBonaire, Saint Eustatius and Saba \tKralendijk"])),
            {"BQ": "Bonaire, Saint Eustatius and Saba"},
        )


class PlaceByIdTests(unittest.TestCase):
    """`Gazetteer.place` is the path from a stored resolution to a display name.

    `resolutions` keeps a `geonames_id` and no name, so without this there is no way to get from a
    row the resolver wrote to the string section 8.1 asks for, and `display_name` would have no
    caller that could ever reach it from the store.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.gazetteer = build_gazetteer()

    def test_it_returns_the_same_row_the_raw_query_returns(self) -> None:
        # place_by_id runs its own SQL, so this compares the method against an independent read
        # rather than against itself.
        for geonames_id in (LONDON, WESTMINSTER_HOTEL, PARIS_TEXAS, MONTE_CARLO):
            with self.subTest(geonames_id=geonames_id):
                self.assertEqual(
                    self.gazetteer.place(geonames_id), place_by_id(self.gazetteer, geonames_id)
                )

    def test_an_unknown_id_is_none_rather_than_an_exception(self) -> None:
        """A resolution can outlive the row it matched: a rebuild can retire a GeoNames id."""
        self.assertIsNone(self.gazetteer.place(999_999_999))
        self.assertIsNone(self.gazetteer.place(None))

    def test_a_resolution_can_be_displayed_end_to_end(self) -> None:
        place = self.gazetteer.place(LONDON)
        assert place is not None
        self.assertEqual(self.gazetteer.display_name(place), "London, Greater London, United Kingdom")


class RestatedAreaTests(unittest.TestCase):
    """Every pair here was seen in the real store on 2026-09-18, not invented.

    The rule drops a middle part that only repeats the place name. It is narrow on purpose: it can
    only make a name less specific, never wrong, so the cost of keeping a part is small and the
    cost of eating a real one ("York" swallowing "Yorkshire") is not.
    """

    def test_an_area_that_only_restates_the_place_is_dropped(self) -> None:
        for name, area in [
            ("Guangzhou", "Guangzhou Shi"),
            ("Dnipro", "Dnipro raion"),
            ("Dublin", "Dublin City"),
            ("Kyiv", "Kyiv City"),
            ("Blantyre", "Blantyre District"),
            ("Lusaka", "Lusaka Province"),
            ("Gurugram", "Gurugram district"),
            ("Kharkiv", "Kharkiv Raion"),
            ("Krasnodar", "Krasnodar Krai"),
            ("Buenos Aires", "Buenos Aires F.D."),
            ("Miami", "Miami-Dade County"),
            ("Los Angeles", "Los Angeles County"),
            ("Paris", "Paris"),
        ]:
            with self.subTest(name=name, area=area):
                self.assertTrue(geonames._restates(name, area))

    def test_an_area_that_adds_something_is_kept(self) -> None:
        for name, area in [
            ("London", "Greater London"),
            ("Johannesburg", "City of Johannesburg Metropolitan Municipality"),
            ("Kuala Lumpur", "WP. Kuala Lumpur"),
            ("Odesa", "Odeskyi Raion"),
            ("Westminster", "Nottingham"),
            ("Chiswick", "Greater London"),
            ("Citi Field", "Queens County"),
        ]:
            with self.subTest(name=name, area=area):
                self.assertFalse(geonames._restates(name, area))

    def test_the_match_must_end_on_a_word_boundary(self) -> None:
        """The whole reason the rule is a prefix test and not a substring test."""
        self.assertFalse(geonames._restates("York", "Yorkshire"))
        self.assertFalse(geonames._restates("Belgorod", "Belgorodskiy Rayon"))
        self.assertTrue(geonames._restates("York", "York County"))

    def test_accents_and_case_do_not_defeat_it(self) -> None:
        self.assertTrue(geonames._restates("Ile-de-France", "Île-de-France"))

    def test_a_real_place_keeps_its_useful_area(self) -> None:
        gazetteer = build_gazetteer()
        self.addCleanup(gazetteer.close)
        place = gazetteer.place(WESTMINSTER_LONDON_PPL)
        assert place is not None
        self.assertEqual(
            gazetteer.display_name(place), "City of Westminster, Greater London, United Kingdom"
        )
