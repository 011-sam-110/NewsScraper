"""Re-extracting after a prompt change.

Stories move to `extracted` and are never claimed by status again, so moving the config hash by
editing a prompt leaves every downstream stage reading an empty table while nothing reports a
fault. `--redo` is the deliberate way to pay for the backlog again, and these tests pin what it
does and does not pick up.
"""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from pathlib import Path

from newsfeed.identity import now_utc
from newsfeed.settings import Settings
from newsfeed.store import Store

CURRENT = "cfg_current"
PREVIOUS = "cfg_previous"


class RedoClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            data_dir=Path(tempfile.mkdtemp()),
            deepseek_api_key=None,
            ingest_url=None,
            ingest_secret=None,
            daily_budget_usd=1.0,
            telegram_bot_token=None,
            telegram_chat_id=None,
            deadman_url=None,
            proxy=None,
        )

    def seed(self, story_id: str, status: str, config_hash: str | None,
             text_hash: str | None = "t1") -> None:
        now = now_utc()
        with Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as db:
                db.execute(
                    """INSERT INTO stories (story_id, outlet, primary_alias, published, text_hash,
                                            first_seen_at, last_seen_at, status)
                       VALUES (?, 'bbc', ?, ?, ?, ?, ?, ?)""",
                    (story_id, story_id, now, text_hash, now, now, status),
                )
                if config_hash:
                    db.execute(
                        """INSERT INTO extractions (story_id, config_hash, accepted, created_at)
                           VALUES (?, ?, 1, ?)""",
                        (story_id, config_hash, now),
                    )

    def claim(self, limit: int = 10) -> list[str]:
        with Store(self.settings.news_db) as store:
            store.migrate()
            return store.claim_missing_extractions(CURRENT, limit)

    def test_a_story_done_under_the_current_hash_is_left_alone(self) -> None:
        self.seed("st_done", "extracted", CURRENT)
        self.assertEqual(self.claim(), [])

    def test_a_story_done_under_an_older_hash_is_claimed(self) -> None:
        """The whole point: status says extracted, but not under the prompt in force now."""
        self.seed("st_stale", "extracted", PREVIOUS)
        self.assertEqual(self.claim(), ["st_stale"])

    def test_a_story_never_extracted_is_claimed(self) -> None:
        self.seed("st_new", "scraped", None)
        self.assertEqual(self.claim(), ["st_new"])

    def test_a_story_with_no_text_is_not_claimed(self) -> None:
        """No text means no quote can be checked, so the call would be paid for and wasted."""
        self.seed("st_textless", "scraped", None, text_hash=None)
        self.assertEqual(self.claim(), [])

    def test_a_claim_is_not_handed_out_twice(self) -> None:
        self.seed("st_a", "extracted", PREVIOUS)
        self.assertEqual(self.claim(), ["st_a"])
        self.assertEqual(self.claim(), [], "a leased story must not be claimed again")

    def test_the_newest_stories_come_first(self) -> None:
        """A budget that runs out mid-backlog must leave the OLDEST undone, not the newest."""
        now = now_utc()
        with Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as db:
                for story_id, published in (
                    ("st_old", "2026-01-01T00:00:00Z"),
                    ("st_new", "2026-09-17T00:00:00Z"),
                    ("st_mid", "2026-05-01T00:00:00Z"),
                ):
                    db.execute(
                        """INSERT INTO stories (story_id, outlet, primary_alias, published,
                                                text_hash, first_seen_at, last_seen_at, status)
                           VALUES (?, 'bbc', ?, ?, 't1', ?, ?, 'extracted')""",
                        (story_id, story_id, published, now, now),
                    )
        self.assertEqual(self.claim(limit=2), ["st_new", "st_mid"])


class RedoCountTests(unittest.TestCase):
    """The dry run must report the redo backlog, not the count of stories waiting to be scraped."""

    def setUp(self) -> None:
        RedoClaimTests.setUp(self)
        self.seed = lambda *a, **k: RedoClaimTests.seed(self, *a, **k)

    def report(self, redo: bool) -> str:
        import contextlib
        from newsfeed import extract

        args = argparse.Namespace(
            limit=10, story=None, model="deepseek-flash", dry_run=True, redo=redo
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(extract.run(args, self.settings), 0)
        return stderr.getvalue()

    def test_a_redo_dry_run_counts_the_stale_stories(self) -> None:
        from newsfeed import extract

        self.seed("st_stale", "extracted", PREVIOUS)
        self.seed("st_done", "extracted", extract.extract_config_hash("deepseek-flash"))

        report = self.report(redo=True)
        self.assertIn("waiting 1", report, "one story has no answer under the current hash")
        self.assertIn("already extracted 1", report)

    def test_a_plain_dry_run_does_not(self) -> None:
        """Without --redo the backlog stays invisible, which is why --redo has to be asked for."""
        self.seed("st_stale", "extracted", PREVIOUS)
        self.assertIn("waiting 0", self.report(redo=False))


if __name__ == "__main__":
    unittest.main()
