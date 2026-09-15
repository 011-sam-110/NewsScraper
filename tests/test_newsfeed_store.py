"""The store: pragmas, numbered migrations, story lookup, leases, outlet health and two writers."""

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import store as store_module  # noqa: E402
from newsfeed.identity import now_utc  # noqa: E402
from newsfeed.store import Store  # noqa: E402


def temp_db() -> Path:
    return Path(tempfile.mkdtemp()) / "news.sqlite3"


def add_story(store: Store, story_id: str, outlet: str = "bbc", status: str = "scraped",
              published: str = "2026-09-14T10:00:00Z", aliases: tuple[str, ...] = ()) -> None:
    now = now_utc()
    with store.write() as connection:
        connection.execute(
            """INSERT INTO stories (story_id, outlet, primary_alias, published, first_seen_at,
                                    last_seen_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (story_id, outlet, aliases[0] if aliases else story_id, published, now, now, status),
        )
        for alias in aliases or (story_id,):
            connection.execute(
                "INSERT INTO story_aliases (outlet, alias, story_id, first_seen_at) VALUES (?, ?, ?, ?)",
                (outlet, alias, story_id, now),
            )


class SchemaTests(unittest.TestCase):
    def test_pragmas_match_the_design(self) -> None:
        with Store(temp_db()) as store:
            self.assertEqual(store.scalar("PRAGMA journal_mode"), "wal")
            self.assertEqual(store.scalar("PRAGMA foreign_keys"), 1)
            self.assertEqual(store.scalar("PRAGMA busy_timeout"), store_module.BUSY_TIMEOUT_MS)
            self.assertIsNone(store.connection.isolation_level)

    def test_migrating_twice_changes_nothing(self) -> None:
        path = temp_db()
        with Store(path) as store:
            self.assertEqual(store.migrate(), store_module.SCHEMA_VERSION)
            versions = [row[0] for row in store.query("SELECT version FROM schema_version ORDER BY version")]
        with Store(path) as store:
            store.migrate()
            self.assertEqual(
                [row[0] for row in store.query("SELECT version FROM schema_version ORDER BY version")],
                versions,
            )

    def test_a_newer_store_is_refused_rather_than_used(self) -> None:
        path = temp_db()
        with Store(path) as store:
            with store.write() as connection:
                connection.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (store_module.SCHEMA_VERSION + 5, now_utc()),
                )
        with self.assertRaises(RuntimeError) as caught:
            with Store(path):
                pass
        self.assertIn("schema version", str(caught.exception))

    def test_statement_splitter_keeps_statements_whole(self) -> None:
        script = "CREATE TABLE a (x TEXT);\nCREATE INDEX a_x ON a(x);\n"
        self.assertEqual(len(store_module.statements(script)), 2)
        with self.assertRaises(RuntimeError):
            store_module.statements("CREATE TABLE a (x TEXT")

    def test_deleting_a_story_takes_its_children(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_1")
            with store.write() as connection:
                connection.execute(
                    "INSERT INTO story_sections (story_id, section, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("st_1", "world", now_utc(), now_utc()),
                )
                connection.execute("DELETE FROM stories WHERE story_id = ?", ("st_1",))
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM story_sections"), 0)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM story_aliases"), 0)


class WriteTransactionTests(unittest.TestCase):
    def test_a_failed_write_rolls_back(self) -> None:
        with Store(temp_db()) as store:
            with self.assertRaises(ValueError):
                with store.write() as connection:
                    connection.execute(
                        "INSERT INTO stories (story_id, outlet, primary_alias, first_seen_at, last_seen_at) "
                        "VALUES ('st_x', 'bbc', 'a', '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')"
                    )
                    raise ValueError("stop")
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 0)


class FindStoryTests(unittest.TestCase):
    def test_any_alias_finds_the_story(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_1", aliases=("urn:bbc:asset:1", "https://www.bbc.co.uk/news/articles/1"))
            self.assertEqual(store.find_story("bbc", ["https://www.bbc.co.uk/news/articles/1"]), "st_1")
            self.assertEqual(store.find_story("bbc", ["unknown", "urn:bbc:asset:1"]), "st_1")
            self.assertIsNone(store.find_story("bbc", ["unknown"]))
            self.assertIsNone(store.find_story("bbc", []))

    def test_an_alias_belongs_to_one_outlet(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_1", outlet="bbc", aliases=("shared-id",))
            self.assertIsNone(store.find_story("reuters", ["shared-id"]))

    def test_aliases_pointing_two_ways_pick_the_earlier_story(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_old", aliases=("a",))
            time.sleep(1.1)  # first_seen_at has one second resolution
            add_story(store, "st_new", aliases=("b",))
            self.assertEqual(store.find_story("bbc", ["a", "b"]), "st_old")


class LeaseTests(unittest.TestCase):
    def test_claiming_takes_the_newest_first_and_hides_them_from_the_next_claim(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_old", published="2026-09-10T00:00:00Z")
            add_story(store, "st_new", published="2026-09-14T00:00:00Z")
            self.assertEqual(store.claim_stories("scraped", 1, owner="one"), ["st_new"])
            self.assertEqual(store.claim_stories("scraped", 5, owner="two"), ["st_old"])
            self.assertEqual(store.claim_stories("scraped", 5, owner="three"), [])

    def test_an_expired_lease_can_be_claimed_again(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_1")
            store.claim_stories("scraped", 1, owner="crashed", lease_seconds=-1)
            self.assertEqual(store.claim_stories("scraped", 1, owner="next"), ["st_1"])

    def test_releasing_clears_the_lease_and_can_move_the_status(self) -> None:
        with Store(temp_db()) as store:
            add_story(store, "st_1")
            store.claim_stories("scraped", 1)
            store.release_stories(["st_1"], status=store_module.STATUS_EXTRACTED)
            row = store.story("st_1")
            self.assertIsNone(row["lease_owner"])
            self.assertIsNone(row["lease_until"])
            self.assertEqual(row["status"], store_module.STATUS_EXTRACTED)
            store.release_stories([])  # no rows, no error


class OutletHealthTests(unittest.TestCase):
    def test_a_failure_counts_up_and_a_success_clears_it(self) -> None:
        with Store(temp_db()) as store:
            store.start_outlet_run("reuters")
            store.finish_outlet_run("reuters", "HTTP 401 from reuters.com", 0)
            store.finish_outlet_run("reuters", "HTTP 401 from reuters.com", 0)
            row = store.one("SELECT * FROM outlet_health WHERE outlet = 'reuters'")
            self.assertEqual(row["consecutive_failures"], 2)
            self.assertIsNone(row["last_new_story_at"])

            store.finish_outlet_run("reuters", None, 3)
            row = store.one("SELECT * FROM outlet_health WHERE outlet = 'reuters'")
            self.assertEqual(row["consecutive_failures"], 0)
            self.assertIsNotNone(row["last_success_at"])
            self.assertIsNotNone(row["last_new_story_at"])
            # The last error is kept, so health can say what went wrong last, and it is dated so a
            # reader can tell history from a live problem.
            self.assertIn("401", row["last_error"])
            self.assertIsNotNone(row["last_error_at"])

    def test_a_run_with_no_new_story_leaves_the_last_new_story_time_alone(self) -> None:
        with Store(temp_db()) as store:
            store.finish_outlet_run("bbc", None, 2)
            first = store.one("SELECT last_new_story_at FROM outlet_health WHERE outlet='bbc'")[0]
            store.finish_outlet_run("bbc", None, 0)
            self.assertEqual(
                store.one("SELECT last_new_story_at FROM outlet_health WHERE outlet='bbc'")[0], first
            )


class RunTests(unittest.TestCase):
    def test_data_as_of_is_the_newest_finished_run(self) -> None:
        with Store(temp_db()) as store:
            self.assertIsNone(store.data_as_of())
            unfinished = store.start_run(["bbc:world"])
            self.assertIsNone(store.data_as_of())
            store.finish_run(unfinished, 10, 4, {})
            self.assertIsNotNone(store.data_as_of())
            row = store.one("SELECT * FROM scrape_runs WHERE run_id = ?", (unfinished,))
            self.assertEqual((row["rows_seen"], row["new_stories"]), (10, 4))


class ConcurrentWriterTests(unittest.TestCase):
    """Section 7.3: two stages writing at once must not raise "database is locked"."""

    def run_writers(self, path: Path, seconds: float, writers: int = 2) -> list[BaseException]:
        errors: list[BaseException] = []
        stop = threading.Event()

        def writer(index: int) -> None:
            try:
                with Store(path) as store:
                    counter = 0
                    while not stop.is_set():
                        counter += 1
                        add_story(store, f"st_{index}_{counter}", aliases=(f"{index}-{counter}",))
                        store.claim_stories("scraped", 3, owner=f"writer-{index}")
            except BaseException as error:  # noqa: BLE001 - the test reports whatever was raised
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(writers)]
        for thread in threads:
            thread.start()
        time.sleep(seconds)
        stop.set()
        for thread in threads:
            thread.join(timeout=60)
        return errors

    def test_two_writers_do_not_lock_each_other_out(self) -> None:
        path = temp_db()
        with Store(path):
            pass
        errors = self.run_writers(path, seconds=3.0)
        self.assertEqual([f"{type(e).__name__}: {e}" for e in errors], [])
        with Store(path) as store:
            self.assertGreater(store.scalar("SELECT COUNT(*) FROM stories"), 0)

    def test_a_connection_is_not_shared_between_threads(self) -> None:
        with Store(temp_db()) as store:
            first = store.connection
            seen: list[sqlite3.Connection] = []
            thread = threading.Thread(target=lambda: seen.append(store.connection))
            thread.start()
            thread.join()
            self.assertIsNot(first, seen[0])


if __name__ == "__main__":
    unittest.main()
