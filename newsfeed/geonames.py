"""The GeoNames gazetteer: the only place a coordinate is ever allowed to come from.

Section 7.7 of docs/ARCHITECTURE.md. A model writes place names; it never writes numbers we
keep. This module turns the GeoNames dumps into a local SQLite gazetteer, and resolve.py asks
it questions. Nothing else here reaches the network.

WHAT IS KEPT, AND WHY IT IS NOT EVERYTHING. allCountries holds about twelve million rows across
nine feature classes. The precision table in section 7.7 can only give a precision to classes A
(administrative), P (populated places) and S (spots, buildings, farms). A row in any other class
resolves to no precision, which resolve.py treats exactly as it treats no match at all: it climbs
to the containing place. So keeping L (parks), R (roads), H (water) and the rest would add rows
that can never change an answer. KEPT_CLASSES is the single place that choice lives, and
PRECISION_BY_CODE is the table it has to agree with. Widen one and you must widen the other, or
the gazetteer will be missing the very rows the new precision was added to describe.

WHY TWO TABLES AND NOT ONE. A place has one identity and many spellings: its own name, its ASCII
form, and its English alternates. Searching means asking who is called this, so the spellings are
their own table with an index on the folded form, and places holds each place once.

THE BUILD TABLE IS NOT BOOKKEEPING. Every source file SHA-256 goes in it, and those hashes are
part of the config hash (section 10.5). A pin is only as trustworthy as the gazetteer that placed
it, so a gazetteer that cannot say which bytes it was built from cannot be audited, and a pin
from it cannot be defended.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sqlite3
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.request import Request, urlopen

from .settings import Settings, load

DOWNLOAD_ROOT = "https://download.geonames.org/export/dump/"

# The five files section 7.7 names. The two zips are read from memory rather than unpacked: the
# unpacked allCountries.txt is well over a gigabyte and nothing needs it on disk.
SOURCE_FILES = (
    "allCountries.zip",
    "alternateNamesV2.zip",
    "admin1CodesASCII.txt",
    "admin2Codes.txt",
    "countryInfo.txt",
)

# Feature classes worth storing. See the module docstring: this must stay in step with
# PRECISION_BY_CODE, which is the reason any of them are here.
KEPT_CLASSES = frozenset({"A", "P", "S"})

# Section 7.7 precision table. A code that is not here has no precision, and a place with no
# precision is never pinned; resolve.py climbs to the containing place instead.
PRECISION_BY_CODE: dict[str, str] = {
    "PPLX": "district",
    "ADM3": "district",
    "ADM4": "district",
    "PPL": "city",
    "PPLA": "city",
    "PPLA2": "city",
    "PPLA3": "city",
    "PPLA4": "city",
    "PPLC": "city",
    "ADM1": "region",
    "ADM2": "region",
    "PCLI": "country",
    "PCL": "country",
    "PCLD": "country",
    "PCLF": "country",
    "PCLS": "country",
}

# Precisions a pin may carry. Section 6.1: region and country are never pinned.
PINNABLE_PRECISIONS = frozenset({"point", "district", "city"})

# Section 8.1 caps location.place at 120 characters. It lives here rather than in the publish stage
# because this module is what builds the string, and a cap the builder does not know about is a cap
# that gets exceeded and then truncated by whoever notices last.
DISPLAY_MAX = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    geonames_id   INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    ascii_name    TEXT,
    latitude      REAL    NOT NULL,
    longitude     REAL    NOT NULL,
    feature_class TEXT    NOT NULL,
    feature_code  TEXT    NOT NULL,
    country       TEXT    NOT NULL,
    admin1        TEXT,
    admin2        TEXT,
    population    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS names (
    folded      TEXT    NOT NULL,
    geonames_id INTEGER NOT NULL,
    source      TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS countries (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_areas (
    code  TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    level INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS build (
    file          TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    downloaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS build_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# How many place rows to hold in memory at once while building the name index. See build_database:
# reading the whole table instead reached 2 GB of RSS on a host with under 1 GB free.
READ_BATCH = 200_000

# Built after the rows are in. Writing rows into an indexed table is far slower than indexing once.
INDEXES = """
CREATE INDEX IF NOT EXISTS names_folded      ON names(folded);
CREATE INDEX IF NOT EXISTS names_by_place    ON names(geonames_id);
CREATE INDEX IF NOT EXISTS places_by_country ON places(country);
"""


@dataclass(frozen=True)
class Place:
    """One GeoNames row, as resolve.py needs it."""

    geonames_id: int
    name: str
    latitude: float
    longitude: float
    feature_class: str
    feature_code: str
    country: str
    admin1: str | None
    admin2: str | None
    population: int

    @property
    def precision(self) -> str | None:
        """The precision this feature earns, or None when the table gives it none."""
        if self.feature_class == "S":
            return "point"
        return PRECISION_BY_CODE.get(self.feature_code)

    @property
    def pinnable(self) -> bool:
        return self.precision in PINNABLE_PRECISIONS


def fold(value: str) -> str:
    """Case and accent folded, for matching a name a model wrote against a name GeoNames holds.

    Accents are stripped rather than normalised because the two sides disagree in both directions:
    an outlet writes Zurich where GeoNames holds Zuerich, and a model writes Odesa with or without
    its accent depending on the article. Folding both sides makes the comparison symmetric.
    """
    decomposed = unicodedata.normalize("NFKD", value.strip().casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.split())


def _restates(name: str, area: str) -> bool:
    """Does `area` only repeat `name`, so that carrying it says nothing new?

    True for "Guangzhou" against "Guangzhou Shi" and for "Dublin" against "Dublin City". False for
    "London" against "Greater London", which is a different and useful thing, and false for
    "Belgorod" against "Belgorodskiy Rayon", where the match runs into the middle of a word. The
    boundary check is what stops "York" eating "Yorkshire".
    """
    head, rest = fold(name), fold(area)
    if not head or not rest:
        return False
    if head == rest:
        return True
    if not rest.startswith(head):
        return False
    return not rest[len(head)].isalnum()


def _text(value: str) -> str | None:
    value = value.strip()
    return value or None


def parse_places(lines: Iterable[str]) -> Iterator[tuple[Any, ...]]:
    """allCountries.txt rows worth keeping, as places-table tuples.

    A row with an unparseable coordinate is dropped rather than stored as zero. Null Island is a
    real coordinate and a pin there is a visible, confident lie.
    """
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 15:
            continue
        if parts[6] not in KEPT_CLASSES:
            continue
        try:
            geonames_id = int(parts[0])
            latitude = float(parts[4])
            longitude = float(parts[5])
        except ValueError:
            continue
        try:
            population = int(parts[14] or 0)
        except ValueError:
            population = 0
        yield (
            geonames_id,
            parts[1].strip(),
            _text(parts[2]),
            latitude,
            longitude,
            parts[6],
            parts[7].strip(),
            parts[8].strip(),
            _text(parts[10]),
            _text(parts[11]),
            population,
        )


def parse_alternate_names(lines: Iterable[str]) -> Iterator[tuple[str, int, str]]:
    """English and preferred alternate names, as names-table tuples.

    Section 7.7 says English and preferred only. Historic names are dropped as well: Constantinople
    pointing at Istanbul would let a story about the wrong century land on a real pin. Rows whose
    language column holds a link, a postal code or an airport code are not names anyone writes in
    an article.
    """
    skipped_languages = {"link", "post", "iata", "icao", "faac", "abbr", "wkdt", "unlc"}
    for line in lines:
        if not line:
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        language = parts[2].strip().lower()
        if language in skipped_languages:
            continue
        preferred = len(parts) > 4 and parts[4].strip() == "1"
        historic = len(parts) > 7 and parts[7].strip() == "1"
        if historic:
            continue
        if language not in ("en", "") and not preferred:
            continue
        name = parts[3].strip()
        if not name:
            continue
        try:
            geonames_id = int(parts[1])
        except ValueError:
            continue
        yield (fold(name), geonames_id, "alternate")


def _digest(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def download(directory: Path, files: Iterable[str] = SOURCE_FILES, log: Any = None) -> list[Path]:
    """Fetch the source files, skipping any already on disk. Returns them in the order given."""
    directory.mkdir(parents=True, exist_ok=True)
    fetched: list[Path] = []
    for name in files:
        target = directory / name
        if not target.exists() or target.stat().st_size == 0:
            if log:
                print(f"geonames: downloading {name}", file=log)
            request = Request(DOWNLOAD_ROOT + name, headers={"User-Agent": "NewsScraper/1.0"})
            # Written to a partial file and renamed, so an interrupted download is never mistaken
            # for a complete one by the skip above.
            partial = target.with_suffix(target.suffix + ".part")
            with urlopen(request, timeout=300) as response, partial.open("wb") as handle:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(chunk)
            partial.replace(target)
        fetched.append(target)
    return fetched


def _zip_lines(path: Path, member: str) -> Iterator[str]:
    with zipfile.ZipFile(path) as archive:
        with archive.open(member) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8", errors="replace"):
                yield line


def _text_lines(path: Path) -> Iterator[str]:
    """A plain source file, streamed. The three name files are small, but so is the code to stream
    them, and reading them whole would be a second memory habit in a module that already learned."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line


def parse_countries(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """`countryInfo.txt` to (code, name). Column 4 is the name; column 0 is the ISO code.

    This file is CRLF where `admin1CodesASCII.txt` and `admin2Codes.txt` are LF, measured on the
    real downloads on 2026-09-18. The name is column 4 of 19, so a stray carriage return would only
    damage the last column and nothing here would look wrong, which is exactly why every line is
    stripped of both endings rather than split on one of them.

    It is also the only one of the three with comment lines, about 50 of them, each starting with a
    hash. They carry the column headings.
    """
    for line in lines:
        stripped = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 5:
            continue
        code, name = parts[0].strip(), parts[4].strip()
        if code and name:
            yield code, name


def parse_admin_areas(lines: Iterable[str], level: int) -> Iterator[tuple[str, str, int]]:
    """An admin codes file to (code, name, level).

    The key is dotted and carries the country: `GB.ENG` at level 1, `GB.ENG.GLA` at level 2. Column
    1 is the real name and column 2 is its ASCII form. Column 1 is taken, accents and all, because
    this is the string a reader sees: Ile-de-France is not what anyone calls it.

    Nothing here checks the shape of the key. A place is looked up by building the same dotted
    string from its own columns, so a key that does not fit the pattern simply never matches, and a
    place with no admin row gets a shorter display name rather than a wrong one.
    """
    for line in lines:
        stripped = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 2:
            continue
        code, name = parts[0].strip(), parts[1].strip()
        if code and name:
            yield code, name, level


def build_database(
    connection: sqlite3.Connection,
    place_lines: Iterable[str],
    alternate_lines: Iterable[str],
    country_lines: Iterable[str],
    admin1_lines: Iterable[str],
    admin2_lines: Iterable[str],
) -> dict[str, int]:
    """Fill a gazetteer from already-opened sources. Separate from download so tests can use it.

    The three name sources have no default. A build that omitted them would produce a gazetteer
    that resolves every place correctly and then displays "GB" where a reader expects "United
    Kingdom": right coordinates, wrong words, and nothing failing. Making them required means that
    mistake is a TypeError at the call site instead.
    """
    connection.executescript(SCHEMA)
    connection.execute("DELETE FROM places")
    connection.execute("DELETE FROM names")
    connection.execute("DELETE FROM countries")
    connection.execute("DELETE FROM admin_areas")

    connection.executemany(
        "INSERT OR REPLACE INTO countries VALUES (?,?)", parse_countries(country_lines)
    )
    connection.executemany(
        "INSERT OR REPLACE INTO admin_areas VALUES (?,?,?)", parse_admin_areas(admin1_lines, 1)
    )
    connection.executemany(
        "INSERT OR REPLACE INTO admin_areas VALUES (?,?,?)", parse_admin_areas(admin2_lines, 2)
    )

    places = 0
    names = 0

    def place_rows() -> Iterator[tuple[Any, ...]]:
        nonlocal places
        for row in parse_places(place_lines):
            places += 1
            yield row

    connection.executemany(
        "INSERT OR REPLACE INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?)", place_rows()
    )

    # A place own name and its ASCII form are searchable spellings too, and they come free from
    # rows already stored, so they are read back rather than buffered during the pass above.
    #
    # READ IN BATCHES, NEVER ALL AT ONCE. The first build of this did `.fetchall()` here and
    # reached 2 GB of RSS on a 7.6 GB host that was down to 911 MB free. That host also runs the
    # graphical session Reuters depends on, so an OOM kill there costs an outlet as well as the
    # build. A batch is read to the end before anything is written, so no statement is stepping
    # over `places` while `names` is being inserted into.
    last_id = -1
    while True:
        batch = connection.execute(
            "SELECT geonames_id, name, ascii_name FROM places WHERE geonames_id > ?"
            " ORDER BY geonames_id LIMIT ?",
            (last_id, READ_BATCH),
        ).fetchall()
        if not batch:
            break
        last_id = batch[-1][0]

        spellings: list[tuple[str, int, str]] = []
        for geonames_id, name, ascii_name in batch:
            folded = fold(name)
            if folded:
                spellings.append((folded, geonames_id, "name"))
            if ascii_name:
                ascii_folded = fold(ascii_name)
                if ascii_folded and ascii_folded != folded:
                    spellings.append((ascii_folded, geonames_id, "ascii"))
        names += len(spellings)
        connection.executemany("INSERT INTO names VALUES (?,?,?)", spellings)

    def alternate_rows() -> Iterator[tuple[str, int, str]]:
        nonlocal names
        for folded, geonames_id, source in parse_alternate_names(alternate_lines):
            if folded:
                names += 1
                yield (folded, geonames_id, source)

    connection.executemany("INSERT INTO names VALUES (?,?,?)", alternate_rows())

    # An alternate for a place we did not keep would match a name and then resolve to nothing.
    # They are removed in SQL rather than filtered in Python against a set of every kept id: that
    # set is about 6.6 million integers, which is the same memory mistake in a different shape.
    removed = connection.execute(
        "DELETE FROM names WHERE NOT EXISTS"
        " (SELECT 1 FROM places p WHERE p.geonames_id = names.geonames_id)"
    ).rowcount
    names -= max(0, removed)
    connection.executescript(INDEXES)
    connection.commit()
    return {
        "places": places,
        "names": names,
        "countries": connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0],
        "admin_areas": connection.execute("SELECT COUNT(*) FROM admin_areas").fetchone()[0],
    }


def record_build(connection: sqlite3.Connection, paths: Iterable[Path]) -> dict[str, str]:
    """Store each source file SHA-256. These hashes go into the config hash."""
    connection.executescript(SCHEMA)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hashes: dict[str, str] = {}
    for path in paths:
        sha, size = _digest(path)
        hashes[path.name] = sha
        connection.execute(
            "INSERT OR REPLACE INTO build VALUES (?,?,?,?)", (path.name, sha, size, now)
        )
    connection.execute("INSERT OR REPLACE INTO build_meta VALUES ('built_at', ?)", (now,))
    connection.execute(
        "INSERT OR REPLACE INTO build_meta VALUES ('kept_classes', ?)",
        (",".join(sorted(KEPT_CLASSES)),),
    )
    connection.commit()
    return hashes


def build(data_dir: Path, log: Any = None) -> dict[str, Any]:
    """Download what is missing, then build the gazetteer beside the store."""
    downloads = data_dir / "geonames-source"
    paths = download(downloads, log=log)
    by_name = {path.name: path for path in paths}

    target = data_dir / "geonames.sqlite3"
    # Built beside the live file and moved into place, so a failed build never leaves a
    # half-filled gazetteer that resolve would happily read.
    working = target.with_suffix(".building")
    working.unlink(missing_ok=True)
    connection = sqlite3.connect(working)
    try:
        counts = build_database(
            connection,
            _zip_lines(by_name["allCountries.zip"], "allCountries.txt"),
            _zip_lines(by_name["alternateNamesV2.zip"], "alternateNamesV2.txt"),
            _text_lines(by_name["countryInfo.txt"]),
            _text_lines(by_name["admin1CodesASCII.txt"]),
            _text_lines(by_name["admin2Codes.txt"]),
        )
        hashes = record_build(connection, paths)
    finally:
        connection.close()
    working.replace(target)
    return {"database": str(target), "hashes": hashes, **counts}


def build_hash(connection: sqlite3.Connection) -> str:
    """A hash over the source files this gazetteer was built from, for the config hash."""
    rows = connection.execute("SELECT file, sha256 FROM build ORDER BY file").fetchall()
    joined = "|".join(f"{name}:{sha}" for name, sha in rows)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class Gazetteer:
    """Read-only questions against a built gazetteer."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "Gazetteer":
        if not path.exists():
            raise FileNotFoundError(
                f"No gazetteer at {path}. Build it with: python -m newsfeed geonames-build"
            )
        return cls(sqlite3.connect(f"file:{path}?mode=ro", uri=True))

    def country_name(self, code: str | None) -> str | None:
        """The display name for a country code, or None when the gazetteer does not know it."""
        if not code:
            return None
        row = self.connection.execute(
            "SELECT name FROM countries WHERE code = ?", (code,)
        ).fetchone()
        return row[0] if row else None

    def admin_name(self, *parts: str | None) -> str | None:
        """The display name for a dotted admin key, or None. `admin_name("GB", "ENG", "GLA")`.

        A missing part means there is nothing to look up, so it returns None rather than building a
        key with a hole in it: "GB..GLA" would never match, but it would also never say why.
        """
        if not all(parts):
            return None
        row = self.connection.execute(
            "SELECT name FROM admin_areas WHERE code = ?", (".".join(p for p in parts if p),)
        ).fetchone()
        return row[0] if row else None

    def display_name(self, place: "Place") -> str:
        """Section 8.1 `location.place`, built only from GeoNames. At most DISPLAY_MAX characters.

        Three parts at most: the place, the administrative area holding it, and the country. The
        document writes the example as "Westminster, London, United Kingdom". What GeoNames
        actually supports is "Westminster, Greater London, United Kingdom", because it knows
        Westminster sits in admin2 GLA and has no notion of a parent city. The document example is
        prose, not a fixture, and the alternative is the model naming the middle part, which
        section 3 forbids: nothing a model wrote is ever shown.

        ONE middle part, never two. "Westminster, Greater London, England, United Kingdom" is
        accurate and is not how anyone writes an address. The smaller unit wins when both exist.

        A part that repeats another is dropped, so a country resolves to "Ukraine" rather than
        "Ukraine, Ukraine", and a region to "Texas, United States" rather than "Texas, Texas,
        United States".

        The middle part is also dropped when it merely RESTATES the place name: an administrative
        area is carried to say which of several places this is, and one that only repeats the name
        distinguishes nothing. Measured over the 128 pinnable places the store held on 2026-09-18,
        this is 12% of them: "Guangzhou, Guangzhou Shi, China" becomes "Guangzhou, China" and
        "Dnipro, Dnipro raion, Ukraine" becomes "Dnipro, Ukraine".

        The test is a prefix ending on a WORD boundary, deliberately narrow. "Greater London" does
        not start with "London" and survives, which is right, because "London, United Kingdom"
        loses something real. Nor does a longer name that merely begins with the same letters:
        "Belgorodskiy Rayon" keeps its place beside Belgorod, because "York" must never eat
        "Yorkshire". Dropping a part can only make a name less specific, never wrong, which is why
        erring towards keeping it is safe in both directions.

        A lookup that misses costs a part, never correctness. That is deliberate, and `admin1`
        "00" shows why no code is ever judged by its shape. Measured over the whole 2026-09-18
        download: "00" means "no region" for the 364 GB, 20 US, 584 ES and 628 UA places that
        carry it, and there is no `GB.00` row, so the lookup misses and the part is dropped. It
        also names a real area for the 49 Monaco places that carry it, because `MC.00` IS a row:
        "Municipality of Monaco". A rule that skipped "00" would be right 1,596 times and wrong
        49 times; asking the table is right every time and is less code.

        An admin2 that misses is dropped, never retried without its admin1. All 48 Spanish
        admin2 orphans DO exist under a different admin1, so ignoring that part would recover
        every one of them and name a province the place is not in. That is the GDELT failure
        this project exists to avoid.
        """
        parts: list[str] = [place.name]

        middle = self.admin_name(place.country, place.admin1, place.admin2) or self.admin_name(
            place.country, place.admin1
        )
        if middle and not _restates(place.name, middle):
            parts.append(middle)

        country = self.country_name(place.country)
        if country:
            parts.append(country)

        seen: list[str] = []
        for part in parts:
            folded = fold(part)
            if folded and folded not in {fold(kept) for kept in seen}:
                seen.append(part)

        built = ", ".join(seen)
        if len(built) <= DISPLAY_MAX:
            return built
        # Too long. Drop the middle before cutting a word in half: a place and its country still
        # locate the pin, where a truncated administrative area only looks broken.
        if len(seen) == 3:
            shorter = f"{seen[0]}, {seen[2]}"
            if len(shorter) <= DISPLAY_MAX:
                return shorter
        return built[:DISPLAY_MAX].rstrip(" ,")

    def place(self, geonames_id: int | None) -> Place | None:
        """One place by its GeoNames id, or None.

        This is the path from a stored resolution to a display name. `resolutions` keeps the
        `geonames_id` and no name, deliberately: the display string is built at publish time from
        whatever gazetteer is current, so a rebuild that improves a name reaches every existing pin
        without re-resolving anything and without paying the model again.
        """
        if geonames_id is None:
            return None
        row = self.connection.execute(
            "SELECT geonames_id, name, latitude, longitude, feature_class, feature_code,"
            " country, admin1, admin2, population FROM places WHERE geonames_id = ?",
            (geonames_id,),
        ).fetchone()
        return Place(*row) if row else None

    def candidates(self, name: str, country: str | None = None) -> list[Place]:
        """Every place called name, optionally restricted to one country."""
        folded = fold(name)
        if not folded:
            return []
        sql = (
            "SELECT DISTINCT p.geonames_id, p.name, p.latitude, p.longitude, p.feature_class,"
            " p.feature_code, p.country, p.admin1, p.admin2, p.population"
            " FROM names n JOIN places p ON p.geonames_id = n.geonames_id"
            " WHERE n.folded = ?"
        )
        values: list[Any] = [folded]
        if country:
            sql += " AND p.country = ?"
            values.append(country.strip().upper())
        return [Place(*row) for row in self.connection.execute(sql, values)]

    def close(self) -> None:
        self.connection.close()


# --------------------------------------------------------------------------------------------
# The stage.
# --------------------------------------------------------------------------------------------


def add_arguments(parser: "argparse.ArgumentParser") -> None:
    parser.add_argument(
        "--keep-downloads",
        action="store_true",
        help="Leave the downloaded dumps in place. They are kept by default so a rebuild is free.",
    )


def run(args: "argparse.Namespace", settings: "Settings | None" = None) -> int:
    settings = settings or load()
    data_dir = settings.ensure_data_dir()
    # The dumps are several hundred megabytes, so say what is happening rather than look hung.
    print(f"geonames: building {settings.geonames_db}", file=sys.stderr)
    result = build(data_dir, log=sys.stderr)
    connection = sqlite3.connect(settings.geonames_db)
    try:
        print(
            f"geonames: {result['places']} places, {result['names']} names, "
            f"build {build_hash(connection)}"
        )
    finally:
        connection.close()
    return 0
