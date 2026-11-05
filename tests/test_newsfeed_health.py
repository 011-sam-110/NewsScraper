"""The health stage: is news still arriving, and does the hub hear when that changes.

Everything here is offline. The store is a temporary SQLite file, the production status is a dict,
and the hub is a function that records what it was given.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsfeed import health
from newsfeed.settings import load
from newsfeed.store import Store

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta: float) -> str:
    return iso(NOW - timedelta(**delta))


def ms(moment_iso: str) -> int:
    return int(datetime.fromisoformat(moment_iso.replace("Z", "+00:00")).timestamp() * 1000)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "news.sqlite3")
        self.store.migrate()
        self.addCleanup(self.store.close)

    def outlet(self, name: str, *, failures: int = 0, new_story: str | None = None,
               success: str | None = None, error: str | None = None) -> None:
        with self.store.write() as connection:
            connection.execute(
                "INSERT INTO outlet_health (outlet, last_run_at, last_success_at, last_new_story_at,"
                " last_error, consecutive_failures, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, ago(minutes=5), success, new_story, error, failures, ago(minutes=5)),
            )

    def story(self, story_id: str, outlet: str, first_seen: str) -> None:
        with self.store.write() as connection:
            connection.execute(
                "INSERT INTO stories (story_id, outlet, primary_alias, url, headline,"
                " published, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (story_id, outlet, story_id, f"https://example.com/{story_id}", story_id,
                 first_seen, first_seen, first_seen),
            )

    def checks(self) -> dict[str, health.Check]:
        return {check.name: check for check in health.store_checks(self.store, NOW)}


class OutletRunTests(StoreCase):
    def test_three_failed_runs_in_a_row_fail_the_check(self) -> None:
        self.outlet("reuters", failures=156, success="2026-10-13T09:00:00Z", error="401 from DataDome")
        check = self.checks()["runs:reuters"]
        self.assertFalse(check.ok)
        self.assertIn("156 failed runs in a row", check.detail)
        self.assertIn("2026-10-13", check.detail)
        self.assertIn("401 from DataDome", check.detail)

    def test_two_failed_runs_are_still_a_pass(self) -> None:
        self.outlet("npr", failures=2)
        self.assertTrue(self.checks()["runs:npr"].ok)

    def test_a_dropped_outlet_is_not_checked(self) -> None:
        self.outlet("guardian", failures=9)
        self.assertNotIn("runs:guardian", self.checks())

    def test_an_error_with_a_proxy_password_is_redacted(self) -> None:
        self.outlet("bbc", failures=4, error="via http://user-x:S3cretPass@gw.example:1 refused")
        self.assertNotIn("S3cretPass", self.checks()["runs:bbc"].detail)

    def test_a_long_error_is_cut(self) -> None:
        self.outlet("pbs", failures=3, error="x" * 5000)
        self.assertLess(len(self.checks()["runs:pbs"].detail), 400)


class SilenceTests(StoreCase):
    def test_a_scraped_outlet_silent_for_12_hours_fails(self) -> None:
        self.outlet("bbc", new_story=ago(hours=13))
        self.assertFalse(self.checks()["silence:bbc"].ok)

    def test_a_scraped_outlet_with_a_recent_story_passes(self) -> None:
        self.outlet("pbs", new_story=ago(hours=2))
        self.assertTrue(self.checks()["silence:pbs"].ok)

    def test_a_feed_has_no_silence_check(self) -> None:
        # One quiet feed (RNZ's Pacific feed) is normal. The newest-story check covers all of them.
        self.outlet("rnz", new_story=ago(hours=30))
        self.assertNotIn("silence:rnz", self.checks())


class NewestStoryTests(StoreCase):
    def test_no_new_story_for_an_hour_fails(self) -> None:
        self.story("a", "npr", ago(minutes=61))
        self.assertFalse(self.checks()["newest"].ok)

    def test_a_story_in_the_last_hour_passes(self) -> None:
        self.story("a", "npr", ago(minutes=61))
        self.story("b", "dw", ago(minutes=4))
        check = self.checks()["newest"]
        self.assertTrue(check.ok)
        self.assertIn("4 min", check.detail)

    def test_a_dropped_outlet_does_not_count_as_fresh(self) -> None:
        self.story("a", "npr", ago(hours=3))
        self.story("g", "guardian", ago(minutes=1))
        self.assertFalse(self.checks()["newest"].ok)

    def test_an_empty_store_fails(self) -> None:
        self.assertFalse(self.checks()["newest"].ok)


class RailTests(unittest.TestCase):
    def test_a_push_in_the_last_20_minutes_passes(self) -> None:
        check = health.rail_check({"last_ok_at": ago(minutes=6), "sent": 12, "held": 4200}, NOW)
        self.assertTrue(check.ok)
        self.assertIn("4200", check.detail)

    def test_no_push_for_20_minutes_fails_and_says_the_last_error(self) -> None:
        state = {"last_ok_at": ago(minutes=45), "last_error": "could not reach the box: timed out"}
        check = health.rail_check(state, NOW)
        self.assertFalse(check.ok)
        self.assertIn("45 min", check.detail)
        self.assertIn("timed out", check.detail)

    def test_a_rail_that_never_pushed_fails(self) -> None:
        self.assertFalse(health.rail_check({}, NOW).ok)


class ProductionTests(unittest.TestCase):
    def news(self, **overrides: object) -> dict:
        block = {
            "newestTs": ms(ago(minutes=7)), "held": 4100, "outlets": 28, "feedsAnswering": 27,
            "feedsTotal": 28, "lastPushAt": ms(ago(minutes=3)), "pushedHeld": 3900,
        }
        block.update(overrides)
        return {"news": block}

    def test_a_fresh_site_passes(self) -> None:
        check = health.production_check(self.news(), None, NOW)
        self.assertTrue(check.ok)
        self.assertIn("4100", check.detail)

    def test_a_stale_newest_headline_fails(self) -> None:
        check = health.production_check(self.news(newestTs=ms(ago(minutes=90))), None, NOW)
        self.assertFalse(check.ok)
        self.assertIn("90 min", check.detail)

    def test_no_push_received_fails(self) -> None:
        self.assertFalse(health.production_check(self.news(lastPushAt=None), None, NOW).ok)
        self.assertFalse(health.production_check(self.news(lastPushAt=ms(ago(minutes=40))), None, NOW).ok)

    def test_an_empty_push_store_fails(self) -> None:
        self.assertFalse(health.production_check(self.news(pushedHeld=0), None, NOW).ok)

    def test_a_site_without_the_news_block_is_unknown_not_failed(self) -> None:
        # Before the Provenance change is deployed, /api/status has no news block.
        check = health.production_check({"signals": []}, None, NOW)
        self.assertIsNone(check.ok)

    def test_a_status_that_cannot_be_read_fails(self) -> None:
        check = health.production_check(None, "HTTP 502", NOW)
        self.assertFalse(check.ok)
        self.assertIn("502", check.detail)

    def test_the_status_address_comes_from_the_ingest_address(self) -> None:
        self.assertEqual(
            health.status_url("https://provenance-online.com/api/news/ingest"),
            "https://provenance-online.com/api/status",
        )

    # A deploy restarts Provenance and empties its in-memory push store until the next rail push
    # (every 5 min). On 2026-11-05 the check ran 2 to 4 s after two restarts and posted FAIL both times.

    def restarted(self, started: str | None, **overrides: object) -> dict:
        """The status of a site that started at `started` and has had no push since."""
        status = self.news(newestTs=None, held=0, outlets=0, lastPushAt=None, pushedHeld=0)
        status["news"].update(overrides)
        status["evidenceScope"] = {"scope": "process", "ageMs": 0}
        if started is not None:
            status["evidenceScope"]["observingSinceMs"] = ms(started)
        return status

    def assert_old_failures(self, check: health.Check) -> None:
        self.assertFalse(check.ok)
        self.assertIn("the site holds no headline", check.detail)
        self.assertIn("the site has received no push since it started", check.detail)
        self.assertIn("the site's push store is empty", check.detail)

    def test_a_site_restarted_30_seconds_ago_is_waiting_not_failed(self) -> None:
        check = health.production_check(self.restarted(ago(seconds=30)), None, NOW)
        self.assertIsNone(check.ok)
        self.assertEqual(check.detail, "site restarted 0 min ago, waiting for the first push")

    def test_a_site_restarted_9_minutes_ago_is_still_waiting(self) -> None:
        check = health.production_check(self.restarted(ago(minutes=9)), None, NOW)
        self.assertIsNone(check.ok)
        self.assertIn("site restarted 9 min ago", check.detail)

    def test_a_site_restarted_11_minutes_ago_with_no_push_fails_as_before(self) -> None:
        self.assert_old_failures(health.production_check(self.restarted(ago(minutes=11)), None, NOW))

    def test_a_status_without_observing_since_fails_as_before(self) -> None:
        self.assert_old_failures(health.production_check(self.restarted(None), None, NOW))
        status = self.restarted(None)
        del status["evidenceScope"]
        self.assert_old_failures(health.production_check(status, None, NOW))
        status["evidenceScope"] = "process"
        self.assert_old_failures(health.production_check(status, None, NOW))

    def test_a_restarted_site_that_has_its_first_push_is_checked_as_normal(self) -> None:
        fresh = self.restarted(
            ago(minutes=3), newestTs=ms(ago(minutes=2)), held=900, lastPushAt=ms(ago(minutes=1)), pushedHeld=900,
        )
        self.assertTrue(health.production_check(fresh, None, NOW).ok)
        stale = self.restarted(
            ago(minutes=3), newestTs=ms(ago(minutes=90)), held=900, lastPushAt=ms(ago(minutes=1)), pushedHeld=900,
        )
        check = health.production_check(stale, None, NOW)
        self.assertFalse(check.ok, "a push has arrived, so the grace for a restart is over")
        self.assertIn("90 min", check.detail)


class DecideTests(unittest.TestCase):
    def checks(self, *failing: str) -> list[health.Check]:
        names = ("runs:reuters", "newest", "rail", "production")
        return [health.Check(name, name not in failing, f"{name} detail") for name in names]

    def posted(self, *failing: str, hours_ago: float = 1) -> dict:
        return {"posted_at": ago(hours=hours_ago), "posted_failing": sorted(failing)}

    def test_a_new_failure_is_posted(self) -> None:
        message, state = health.decide(self.posted(), self.checks("rail"), NOW)
        self.assertIn("rail detail", message)
        self.assertEqual(state["posted_failing"], ["rail"])

    def test_the_same_failure_is_not_posted_again(self) -> None:
        message, _ = health.decide(self.posted("rail"), self.checks("rail"), NOW)
        self.assertIsNone(message)

    def test_a_recovery_is_posted(self) -> None:
        message, state = health.decide(self.posted("rail"), self.checks(), NOW)
        self.assertIn("Recovered", message)
        self.assertIn("rail", message)
        self.assertEqual(state["posted_failing"], [])

    def test_a_daily_digest_is_posted_when_all_is_well(self) -> None:
        message, _ = health.decide(self.posted(hours_ago=25), self.checks(), NOW)
        self.assertIsNotNone(message)
        self.assertIn("all checks pass", message)

    def test_nothing_is_posted_on_a_quiet_good_day(self) -> None:
        message, _ = health.decide(self.posted(hours_ago=3), self.checks(), NOW)
        self.assertIsNone(message)

    def test_an_unknown_check_is_not_a_failure(self) -> None:
        checks = self.checks() + [health.Check("extra", None, "could not tell")]
        message, state = health.decide(self.posted(hours_ago=3), checks, NOW)
        self.assertIsNone(message)
        self.assertEqual(state["posted_failing"], [])

    def test_the_first_run_posts(self) -> None:
        message, _ = health.decide({}, self.checks(), NOW)
        self.assertIsNotNone(message)


class RunTests(unittest.TestCase):
    """The stage end to end: the store, rail.json, a fake status reader and a fake hub."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.settings = load({
            "NEWSFEED_DATA_DIR": str(self.data),
            "NEWSFEED_INGEST_URL": "https://provenance-online.com/api/news/ingest",
        })
        with Store(self.settings.news_db) as store:
            with store.write() as connection:
                connection.execute(
                    "INSERT INTO stories (story_id, outlet, primary_alias, url, headline,"
                    " published, first_seen_at, last_seen_at) VALUES ('a', 'npr', 'a',"
                    " 'https://example.com/a', 'a', ?, ?, ?)",
                    (ago(minutes=3),) * 3,
                )
        (self.data / "rail.json").write_text(json.dumps({"last_ok_at": ago(minutes=2), "held": 10}))
        self.posts: list[str] = []

    def run_stage(self, *, post_ok: bool = True, dry_run: bool = False) -> int:
        def post(text: str) -> bool:
            self.posts.append(text)
            return post_ok

        args = argparse.Namespace(dry_run=dry_run, no_production=False)
        with contextlib.redirect_stdout(io.StringIO()):
            return health.run(
                args, self.settings, post=post, now=NOW,
                read_status=lambda url: ({"signals": []}, None),
            )

    def test_a_healthy_first_run_posts_once_and_exits_0(self) -> None:
        self.assertEqual(self.run_stage(), 0)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.run_stage(), 0)
        self.assertEqual(len(self.posts), 1, "the second run has nothing new to say")
        saved = json.loads(self.settings.health_file.read_text())
        self.assertIn("newest", saved["checks"])

    def test_a_failing_check_exits_1(self) -> None:
        (self.data / "rail.json").write_text(json.dumps({"last_ok_at": ago(hours=2)}))
        self.assertEqual(self.run_stage(), 1)

    def test_a_post_that_did_not_land_is_tried_again(self) -> None:
        self.run_stage(post_ok=False)
        self.run_stage()
        self.assertEqual(len(self.posts), 2)

    def test_a_dry_run_posts_nothing_and_writes_nothing(self) -> None:
        self.run_stage(dry_run=True)
        self.assertEqual(self.posts, [])
        self.assertFalse(self.settings.health_file.exists())

    def test_a_deploy_restart_posts_nothing_and_exits_0(self) -> None:
        self.run_stage()
        self.assertEqual(len(self.posts), 1, "the first run posts the all-pass digest")
        restarted = {
            "evidenceScope": {"observingSinceMs": ms(ago(seconds=4))},
            "news": {"newestTs": None, "held": 0, "lastPushAt": None, "pushedHeld": 0},
        }
        args = argparse.Namespace(dry_run=False, no_production=False)
        with contextlib.redirect_stdout(io.StringIO()):
            code = health.run(
                args, self.settings, post=lambda text: self.posts.append(text) or True, now=NOW,
                read_status=lambda url: (restarted, None),
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posts), 1, "a restart is not a failure, so nothing new is posted")
        saved = json.loads(self.settings.health_file.read_text())
        self.assertIsNone(saved["checks"]["production"]["ok"])



class CliTests(unittest.TestCase):
    def test_the_stage_is_registered(self) -> None:
        from newsfeed.__main__ import build_parser

        args = build_parser().parse_args(["health", "--dry-run", "--no-production"])
        self.assertEqual(args.stage, "health")
        self.assertTrue(args.dry_run)

if __name__ == "__main__":
    unittest.main()
