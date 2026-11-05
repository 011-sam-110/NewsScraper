"""Resolve and the gazetteer, against real GeoNames rows.

The four golden tests section 7.7 requires before M6 ends are the GoldenTests class. The rows in
tests/fixtures/geonames/ are unedited lines from the real GeoNames dumps for exactly the places
those tests name, so a rule that passes here passes against the bytes the gazetteer is built from.
GeoNames data is CC BY 4.0.
"""

import argparse
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
GEORGIA_COUNTRY = 614540
# Added for the display tests: its admin1 is the code "00", which names a real area in
# Monaco and no area at all in most countries. It resolves like any other place here.
MONTE_CARLO = 2992741  # A PCLI GE


def build_gazetteer() -> Gazetteer:
    connection = sqlite3.connect(":memory:")
    places = (FIXTURES / "allCountries.sample.txt").read_text(encoding="utf-8").splitlines()
    alternates = (FIXTURES / "alternateNamesV2.sample.txt").read_text(encoding="utf-8").splitlines()
    geonames.build_database(
        connection, places, alternates,
        (FIXTURES / "countryInfo.sample.txt").read_text(encoding="utf-8").splitlines(),
        (FIXTURES / "admin1Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
        (FIXTURES / "admin2Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
    )
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
                MONTE_CARLO,
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


class StageTests(unittest.TestCase):
    """The stage itself, not just the rules: a runtime fault here is invisible to every test above."""

    def setUp(self) -> None:
        import tempfile
        from newsfeed.settings import Settings
        from newsfeed.store import Store

        self.data_dir = Path(tempfile.mkdtemp())
        self.settings = Settings(
            data_dir=self.data_dir,
            deepseek_api_key=None,
            ingest_url=None,
            ingest_secret=None,
            daily_budget_usd=1.0,
            telegram_bot_token=None,
            telegram_chat_id=None,
            deadman_url=None,
            proxy=None,
        )
        self.Store = Store

    def write_gazetteer(self) -> None:
        connection = sqlite3.connect(self.settings.geonames_db)
        places = (FIXTURES / "allCountries.sample.txt").read_text(encoding="utf-8").splitlines()
        alternates = (FIXTURES / "alternateNamesV2.sample.txt").read_text(encoding="utf-8").splitlines()
        geonames.build_database(
            connection, places, alternates,
            (FIXTURES / "countryInfo.sample.txt").read_text(encoding="utf-8").splitlines(),
            (FIXTURES / "admin1Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
            (FIXTURES / "admin2Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
        )
        connection.close()

    def seed_extraction(self, config_hash: str | None = None, story: bool = True, **over: object) -> None:
        from newsfeed.config import extract_config_hash
        from newsfeed.identity import now_utc

        now = now_utc()
        with self.Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as connection:
                if story:
                    connection.execute(
                        """INSERT INTO stories (story_id, outlet, primary_alias, published,
                                                first_seen_at, last_seen_at, status)
                           VALUES ('st_1', 'reuters', 'st_1', ?, ?, ?, 'extracted')""",
                        (now, now, now),
                    )
                row = {
                    "place_name": "Paris",
                    "place_within": None,
                    "place_country": "FR",
                    "place_kind": "city",
                }
                row.update(over)
                connection.execute(
                    """INSERT INTO extractions (story_id, config_hash, accepted, is_physical_event,
                                                place_name, place_within, place_country, place_kind,
                                                created_at)
                       VALUES ('st_1', ?, 1, 1, ?, ?, ?, ?, ?)""",
                    (
                        config_hash or extract_config_hash(),
                        row["place_name"],
                        row["place_within"],
                        row["place_country"],
                        row["place_kind"],
                        now,
                    ),
                )

    def resolutions(self) -> list[sqlite3.Row]:
        with self.Store(self.settings.news_db) as store:
            store.migrate()
            return store.query("SELECT * FROM resolutions")

    def test_a_run_writes_a_resolution_row(self) -> None:
        self.write_gazetteer()
        self.seed_extraction()
        code = resolve.run(
            argparse.Namespace(limit=10, story=None, dry_run=False), self.settings
        )
        self.assertEqual(code, 0)
        rows = self.resolutions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["geonames_id"], PARIS_FRANCE)
        self.assertEqual(rows[0]["place_precision"], "city")
        self.assertEqual(rows[0]["pinnable"], 1)
        from newsfeed.config import extract_config_hash

        self.assertEqual(rows[0]["extract_hash"], extract_config_hash())

    def test_a_re_extracted_story_is_resolved_again_from_its_new_answer(self) -> None:
        # Trap found 2026-11-05: a story re-extracted under a new hash kept the coordinate its OLD answer
        # made, because resolve skipped any story that already had a row under the resolve hash. One of
        # six stories re-asked at max effort that day named a different place.
        self.write_gazetteer()
        self.seed_extraction(config_hash="cfg_old", place_name="Atlantis")
        with self.Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as connection:
                connection.execute(
                    """INSERT INTO resolutions (story_id, config_hash, extract_hash, resolved, pinnable,
                                                climbed, reason, created_at)
                       VALUES ('st_1', ?, 'cfg_old', 0, 0, 0, 'old answer', '2026-11-01T00:00:00Z')""",
                    (self.resolve_hash(),),
                )
        self.seed_extraction(story=False)  # the same story, now under the current hash: Paris
        resolve.run(argparse.Namespace(limit=10, story=None, dry_run=False), self.settings)
        rows = self.resolutions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["geonames_id"], PARIS_FRANCE)
        from newsfeed.config import extract_config_hash

        self.assertEqual(rows[0]["extract_hash"], extract_config_hash())

    def test_an_answer_under_an_older_hash_only_is_not_resolved(self) -> None:
        # Cluster reads only the current hash, so resolving an old answer would place a story nothing reads.
        self.write_gazetteer()
        self.seed_extraction(config_hash="cfg_old")
        resolve.run(argparse.Namespace(limit=10, story=None, dry_run=False), self.settings)
        self.assertEqual(self.resolutions(), [])

    def resolve_hash(self) -> str:
        from newsfeed.config import gazetteer_resolve_hash

        gazetteer = geonames.Gazetteer(sqlite3.connect(self.settings.geonames_db))
        try:
            return gazetteer_resolve_hash(gazetteer.connection)
        finally:
            gazetteer.connection.close()

    def test_a_second_run_does_not_redo_the_work(self) -> None:
        self.write_gazetteer()
        self.seed_extraction()
        args = argparse.Namespace(limit=10, story=None, dry_run=False)
        resolve.run(args, self.settings)
        resolve.run(args, self.settings)
        self.assertEqual(len(self.resolutions()), 1)

    def test_a_dry_run_writes_nothing(self) -> None:
        self.write_gazetteer()
        self.seed_extraction()
        resolve.run(argparse.Namespace(limit=10, story=None, dry_run=True), self.settings)
        self.assertEqual(self.resolutions(), [])

    def test_an_unresolved_place_is_still_recorded_with_its_reason(self) -> None:
        # A place that resolves to nothing must leave a row saying so. Without one, the next run
        # asks the same question again forever, and nothing can report how often this happens.
        self.write_gazetteer()
        self.seed_extraction(place_name="Atlantis")
        resolve.run(argparse.Namespace(limit=10, story=None, dry_run=False), self.settings)
        rows = self.resolutions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolved"], 0)
        self.assertEqual(rows[0]["pinnable"], 0)
        self.assertIn("Atlantis", rows[0]["reason"])

    def test_no_gazetteer_exits_2_and_says_how_to_build_one(self) -> None:
        self.seed_extraction()
        code = resolve.run(argparse.Namespace(limit=10, story=None, dry_run=False), self.settings)
        self.assertEqual(code, 2)


# ------------------------------------------------------------------------------------------------
# Namesakes (2026-11-05). Real GeoNames rows for two wrong pins the verification plan found and one
# from the main store: "Villarreal" on a hamlet in Badajoz, "Mindanao" on a village in Cebu, "Lara"
# on a village in Apure. tests/fixtures/geonames-namesakes/ holds unedited lines of the 2026-09-18
# download for exactly these places and the containers the stories named (English, unnamed-language,
# preferred and link rows only; the parser drops every other language anyway).
# ------------------------------------------------------------------------------------------------

NAMESAKES = Path(__file__).parent / "fixtures" / "geonames-namesakes"

VILA_REAL = 2509509  # P PPL ES 60 CS, population 50,577. English Wikipedia title: "Villarreal"
VILLARREAL_HAMLET = 2509493  # P PPL ES 57 BA (Badajoz), population 0
CASTELLON = 3125881  # A ADM2 ES 60 CS
MINDANAW = 1699596  # P PPL PH 07 21 (Cebu), population 0, alternate name "Mindanao"
MINDANAO_BARANGAY = 11869674  # A ADM4 PH 07 21 (Cebu), population 0
PHILIPPINES = 1694008  # A PCLI PH
ESTADO_LARA = 3636539  # A ADM1 VE 13, population 1,795,100, alternate name "Lara"
GUACHARA = 3640454  # P PPL VE 03 (Apure), population 0, alternate name "Lara"
GAZA_CITY = 281133  # P PPLA PS, population 410,000. English Wikipedia: "Gaza City"
GAZA_ADM2 = 11184609  # A ADM2 PS, population 505,700. Links "Gaza City" AND "Gaza Governorate"
DHUBAB_TOWN = 76089  # P PPLA2 YE 25 1507, population 0
DHUBAB_DISTRICT = 6940689  # A ADM2 YE 25 1507, population 0


def namesake_lines(name: str) -> list[str]:
    return (NAMESAKES / name).read_text(encoding="utf-8").splitlines()


def build_namesake_gazetteer(alternates: list[str] | None = None) -> Gazetteer:
    connection = sqlite3.connect(":memory:")
    geonames.build_database(
        connection,
        namesake_lines("allCountries.sample.txt"),
        namesake_lines("alternateNamesV2.sample.txt") if alternates is None else alternates,
        [], [], [],
    )
    return Gazetteer(connection)


class WikipediaTitleTests(unittest.TestCase):
    """Names rule 2: the title of a place's English Wikipedia article is one of its names."""

    def test_an_english_wikipedia_link_is_read_as_a_name(self) -> None:
        rows = list(geonames.parse_alternate_names(
            ["2944706\t2509509\tlink\thttps://en.wikipedia.org/wiki/Villarreal\t\t\t\t\t\t"]
        ))
        self.assertEqual(rows, [("villarreal", VILA_REAL, "wikipedia")])

    def test_the_title_is_decoded_and_loses_its_qualifier(self) -> None:
        title = geonames.wikipedia_title
        self.assertEqual(title("https://en.wikipedia.org/wiki/Vila_Real_%28Oliven%C3%A7a%29"), "Vila Real")
        self.assertEqual(title("https://en.wikipedia.org/wiki/Lara_(state)"), "Lara")
        self.assertEqual(title("https://en.wikipedia.org/wiki/Athens,_Texas"), "Athens")
        self.assertEqual(title("https://en.wikipedia.org/wiki/Province_of_Castell%C3%B3n"), "Province of Castellón")

    def test_other_links_are_still_not_names(self) -> None:
        title = geonames.wikipedia_title
        self.assertIsNone(title("https://ru.wikipedia.org/wiki/%D0%A4"))
        self.assertIsNone(title("http://id.loc.gov/authorities/names/n79097478"))
        self.assertIsNone(title("https://example.org"))
        self.assertIsNone(title("https://en.wikipedia.org/wiki/"))
        rows = list(geonames.parse_alternate_names(
            ["3050651\t1694008\tlink\thttps://ru.wikipedia.org/wiki/%D0%A4\t\t\t\t\t\t",
             "11262212\t1694008\tlink\thttp://id.loc.gov/authorities/names/n79058386\t\t\t\t\t\t"]
        ))
        self.assertEqual(rows, [])

    def test_the_names_rule_is_recorded_in_the_build_and_read_back(self) -> None:
        connection = sqlite3.connect(":memory:")
        geonames.record_build(connection, [])
        self.assertEqual(geonames.NAMES_RULE, 2)
        self.assertEqual(geonames.names_rule(connection), geonames.NAMES_RULE)

    def test_a_gazetteer_built_before_the_rule_reads_as_rule_1(self) -> None:
        # The live gazetteer of 2026-09-18 has no names_rule row. It must never pass for a rule 2 build.
        connection = sqlite3.connect(":memory:")
        connection.executescript(geonames.SCHEMA)
        self.assertEqual(geonames.names_rule(connection), 1)


class VillarrealTests(unittest.TestCase):
    """#861: "Villarreal" landed on a hamlet of population 0 in Badajoz. The football club's town is
    Vila-real, population 50,577, and GeoNames holds no "Villarreal" name for it at all: only the
    link to its English Wikipedia article, whose title is "Villarreal"."""

    def test_villarreal_is_vila_real(self) -> None:
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Villarreal", None, "ES", "city")
        self.assertEqual(result.place.geonames_id, VILA_REAL)
        self.assertTrue(result.pinnable)
        self.assertFalse(result.climbed)

    def test_villarreal_within_castellon_is_vila_real_too(self) -> None:
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Villarreal", "Castellón", "ES", "city")
        self.assertEqual(result.place.geonames_id, VILA_REAL)
        self.assertFalse(result.climbed)

    def test_without_the_wikipedia_names_it_is_the_hamlet(self) -> None:
        # The fault this rule fixes, kept visible: names rule 1 drops every link row.
        without = [line for line in namesake_lines("alternateNamesV2.sample.txt") if "\tlink\t" not in line]
        gazetteer = build_namesake_gazetteer(without)
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Villarreal", None, "ES", "city")
        self.assertEqual(result.place.geonames_id, VILLARREAL_HAMLET)

    def test_an_admin_area_takes_no_wikipedia_title(self) -> None:
        # Measured over the store on 2026-11-05: the Gaza ADM2 (population 505,700) links the article
        # "Gaza City" as well as "Gaza Governorate". With the title as a name it outranked the city
        # (410,000), so every district "within Gaza City" climbed to a region. An admin area's article is
        # often the article about its main town, so names rule 2 reads titles for towns and spots only.
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        holders = {row[0] for row in gazetteer.connection.execute(
            "SELECT geonames_id FROM names WHERE folded = 'gaza city'")}
        self.assertEqual(holders, {GAZA_CITY})
        result = resolve.resolve_place(gazetteer, "Tal al-Hawa", "Gaza City", "PS", "district")
        self.assertEqual((result.place.geonames_id, result.precision, result.climbed), (GAZA_CITY, "city", True))

    def test_the_name_comes_from_the_wikipedia_row(self) -> None:
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        sources = {row[0] for row in gazetteer.connection.execute(
            "SELECT source FROM names WHERE folded = 'villarreal' AND geonames_id = ?", (VILA_REAL,))}
        self.assertEqual(sources, {"wikipedia"})


class RegionAnswerTests(unittest.TestCase):
    """A region answer never lands on a town."""

    def test_mindanao_the_region_is_not_a_village_in_cebu(self) -> None:
        # Main store, 2026-11-05: "A magnitude 5.7 earthquake struck Mindanao in the Philippines",
        # kind region, pinned on Mindanaw, a village of population 0 in Cebu.
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Mindanao", "Philippines", "PH", "region")
        self.assertFalse(result.pinnable)
        self.assertNotIn(result.place.geonames_id if result.place else None, (MINDANAW, MINDANAO_BARANGAY))

    def test_a_region_answer_with_no_container_is_no_place(self) -> None:
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Mindanao", None, "PH", "region")
        self.assertFalse(result.pinnable)
        self.assertIsNone(result.place)

    def test_a_country_answer_never_lands_on_a_town_either(self) -> None:
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        self.assertFalse(resolve.resolve_place(gazetteer, "Mindanao", None, "PH", "country").pinnable)

    def test_lara_the_state_is_not_a_village_in_apure(self) -> None:
        # Main store, 2026-11-05: "the town of Lara, west of Caracas", kind city. "Caracas" alone is
        # ambiguous with the state of Miranda, so the search ran country-wide and the village won on
        # fit. A town of population 0 never takes a name that also names a region of the country.
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Lara", "Caracas", "VE", "city")
        self.assertFalse(result.pinnable)
        self.assertEqual(result.place.geonames_id, ESTADO_LARA)

    def test_a_town_inside_the_region_of_its_name_keeps_it(self) -> None:
        # "The Iran-backed Houthis reached the Yemeni town of Dhubab": Dhubab is the seat of the Dhubab
        # district, both population 0. The town is inside the region that shares its name, so it is the
        # place, not a namesake from elsewhere. Rule 2 acts only on a town OUTSIDE that region (Lara).
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        result = resolve.resolve_place(gazetteer, "Dhubab", None, "YE", "city")
        self.assertEqual(result.place.geonames_id, DHUBAB_TOWN)
        self.assertTrue(result.pinnable)

    def test_a_town_with_people_still_wins_its_own_name(self) -> None:
        # The guard acts only on a town of population 0 against a region of the same name.
        gazetteer = build_namesake_gazetteer()
        self.addCleanup(gazetteer.close)
        self.assertTrue(resolve.resolve_place(gazetteer, "Villarreal", None, "ES", "city").pinnable)
        self.assertTrue(resolve.resolve_place(gazetteer, "Caracas", None, "VE", "city").pinnable)

    def test_the_golden_city_answers_are_untouched(self) -> None:
        gazetteer = build_gazetteer()
        self.addCleanup(gazetteer.close)
        self.assertEqual(resolve.resolve_place(gazetteer, "Paris", "Texas", "US", "city").place.geonames_id,
                         PARIS_TEXAS)
        self.assertEqual(
            resolve.resolve_place(gazetteer, "Westminster", "London", "GB", "district").place.geonames_id,
            WESTMINSTER_LONDON_ADM,
        )


class ResolveHashTests(unittest.TestCase):
    def test_the_names_rule_moves_the_resolve_hash(self) -> None:
        from newsfeed.config import resolve_config_hash

        self.assertNotEqual(resolve_config_hash("gazetteer", 1), resolve_config_hash("gazetteer", 2))

    def test_the_region_guard_is_in_the_resolve_hash(self) -> None:
        from newsfeed.config import resolve_components

        components = resolve_components("gazetteer", 2)
        self.assertEqual(components["names_rule"], 2)
        self.assertEqual(components["region_kinds"], ["country", "region"])
        self.assertEqual(components["namesake_town_max_population"], 0)

    def test_the_stage_hash_reads_the_rule_the_gazetteer_was_built_with(self) -> None:
        from newsfeed.config import gazetteer_resolve_hash, resolve_config_hash

        connection = sqlite3.connect(":memory:")
        connection.executescript(geonames.SCHEMA)
        old = gazetteer_resolve_hash(connection)
        self.assertEqual(old, resolve_config_hash(geonames.build_hash(connection), 1))
        connection.execute("INSERT INTO build_meta VALUES ('names_rule', '2')")
        self.assertNotEqual(gazetteer_resolve_hash(connection), old)
