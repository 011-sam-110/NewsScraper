"""drop-outlets: forget the stored rows of an outlet Sam dropped, without breaking anyone else's."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from newsfeed import drop
from newsfeed.settings import load
from newsfeed.store import Store

SEEN = "2026-11-05T10:00:00Z"


class DropTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = load({"NEWSFEED_DATA_DIR": self.directory.name})
        self.store = Store(self.settings.news_db)
        self.store.migrate()
        self.addCleanup(self.store.close)
        with self.store.write() as connection:
            for story_id, outlet in (("g1", "guardian"), ("n1", "nyt"), ("b1", "bbc"), ("b2", "bbc")):
                connection.execute(
                    "INSERT INTO stories (story_id, outlet, primary_alias, url, headline,"
                    " published, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (story_id, outlet, story_id, f"https://example.com/{story_id}", story_id, SEEN, SEEN, SEEN),
                )
                connection.execute(
                    "INSERT INTO story_sections (story_id, section, first_seen_at, last_seen_at)"
                    " VALUES (?, 'world', ?, ?)", (story_id, SEEN, SEEN),
                )
            # A cluster founded by a Guardian story that a BBC story joined, and one founded by BBC.
            for cluster_id, founder in (("c_g", "g1"), ("c_b", "b2")):
                connection.execute(
                    "INSERT INTO clusters (cluster_id, config_hash, founder_story, created_at)"
                    " VALUES (?, 'h', ?, ?)", (cluster_id, founder, SEEN),
                )
            for story_id, cluster_id in (("g1", "c_g"), ("b1", "c_g"), ("b2", "c_b")):
                connection.execute(
                    "INSERT INTO cluster_members (story_id, config_hash, cluster_id, verdict, joined_at)"
                    " VALUES (?, 'h', ?, 'joined', ?)", (story_id, cluster_id, SEEN),
                )
            connection.execute(
                "INSERT INTO llm_calls (stage, story_id, model, outcome, created_at, cost_usd)"
                " VALUES ('extract', 'g1', 'm', 'ok', ?, 0.01)", (SEEN,),
            )
            connection.execute(
                "INSERT INTO outlet_health (outlet, updated_at) VALUES ('guardian', ?)", (SEEN,)
            )

    def count(self, sql: str) -> int:
        return self.store.scalar(sql)

    def test_a_plan_changes_nothing(self) -> None:
        plan = drop.plan(self.store, ("guardian", "nyt"))
        self.assertEqual(plan["stories"], 2)
        self.assertEqual(plan["other_members"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM stories"), 4)

    def test_the_rows_go_and_everything_else_stays(self) -> None:
        drop.apply(self.store, ("guardian", "nyt"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM stories WHERE outlet IN ('guardian', 'nyt')"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM stories WHERE outlet = 'bbc'"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM story_sections"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM outlet_health WHERE outlet = 'guardian'"), 0)
        # The spend record stays: it is what was paid, whatever happened to the story.
        self.assertEqual(self.count("SELECT COUNT(*) FROM llm_calls"), 1)

    def test_a_story_left_in_a_cluster_that_lost_its_founder_is_freed_to_cluster_again(self) -> None:
        drop.apply(self.store, ("guardian", "nyt"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM clusters WHERE cluster_id = 'c_g'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM cluster_members WHERE story_id = 'b1'"), 0)
        # A cluster the dropped outlets never founded is untouched.
        self.assertEqual(self.count("SELECT COUNT(*) FROM cluster_members WHERE story_id = 'b2'"), 1)

    def test_only_a_dropped_outlet_can_be_named(self) -> None:
        with self.assertRaises(ValueError):
            drop.apply(self.store, ("bbc",))

    def test_the_stage_is_a_dry_run_unless_told_otherwise(self) -> None:
        self.store.close()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            drop.run(argparse.Namespace(yes=False), self.settings)
        self.assertIn("2 stories", out.getvalue())
        with Store(self.settings.news_db) as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 4)
        with contextlib.redirect_stdout(io.StringIO()):
            drop.run(argparse.Namespace(yes=True), self.settings)
        with Store(self.settings.news_db) as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 2)


if __name__ == "__main__":
    unittest.main()
