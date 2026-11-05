"""The pin verification layer (docs/ARCHITECTURE.md section 7.9): a fresh DeepSeek call judges every
headline pin, and a second fresh call, the critic, settles any flag.

Every model answer here is a recorded one (tests/fixtures/verify/answers.json): no test calls DeepSeek.
The rules these tests hold:

- Verification only removes pins. It never adds one.
- It fails closed: no budget, a 402, an error or two bad answers mean no pin on that run.
- The critic runs at most once per story, and a story with a final verdict is never asked again under
  the same hashes and the same input.
- The decision table: no flag is "verified"; a flag and critic A is "kept after review"; critic B is
  "overruled"; critic "unclear" is "split, dropped". Only the first two send a pin.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import config, geonames, headline_places, prompts, verify  # noqa: E402
from newsfeed.deepseek import BalanceError, Completion, TransientError, Usage  # noqa: E402
from newsfeed.extract import BudgetReached  # noqa: E402
from newsfeed.identity import now_utc  # noqa: E402
from newsfeed.settings import Settings  # noqa: E402
from newsfeed.store import STATUS_FEED, Store  # noqa: E402

ANSWERS = json.loads((Path(__file__).parent / "fixtures" / "verify" / "answers.json").read_text(encoding="utf-8"))
GEONAMES = Path(__file__).parent / "fixtures" / "geonames"
PARIS_FRANCE = 2988507


def answer(case: str, part: str = "verify") -> str:
    return json.dumps(ANSWERS[case][part], ensure_ascii=False)


def critic(case: str) -> str:
    if case == "critic_unclear":
        return json.dumps(ANSWERS[case], ensure_ascii=False)
    return answer(case, "critic")


class FakeClient:
    """Hands back recorded answers in order and counts every call by kind. Never touches a network."""

    model = "deepseek-flash"

    def __init__(self, verify_answers=(), critic_answers=(), error: Exception | None = None,
                 cost: float = 0.001) -> None:
        self.queues = {"verify": list(verify_answers), "critic": list(critic_answers)}
        self.calls: list[tuple[str, str]] = []
        self.error = error
        self.cost = cost

    def count(self, kind: str) -> int:
        return sum(1 for called, _ in self.calls if called == kind)

    def complete(self, system: str, user: str, max_tokens: int = 0, temperature: float = 0.0,
                 json_object: bool = True) -> Completion:
        kind = "verify" if system == prompts.VERIFY_SYSTEM else "critic"
        self.assertion_system(system)
        self.calls.append((kind, user))
        if self.error is not None:
            raise self.error
        content = self.queues[kind].pop(0)
        return Completion(content=content, model="deepseek-flash", finish_reason="stop",
                          usage=Usage(prompt_tokens=900, completion_tokens=700, cache_miss_tokens=900),
                          cost_usd=self.cost, latency_ms=6000, attempts=1)

    @staticmethod
    def assertion_system(system: str) -> None:
        if system not in (prompts.VERIFY_SYSTEM, prompts.ADJUDICATE_SYSTEM):
            raise AssertionError("the verify stage sent a system prompt that is not one of its two")


def write_gazetteer(path: Path, names_rule: bool = True) -> None:
    connection = sqlite3.connect(path)
    geonames.build_database(
        connection,
        (GEONAMES / "allCountries.sample.txt").read_text(encoding="utf-8").splitlines(),
        (GEONAMES / "alternateNamesV2.sample.txt").read_text(encoding="utf-8").splitlines(),
        (GEONAMES / "countryInfo.sample.txt").read_text(encoding="utf-8").splitlines(),
        (GEONAMES / "admin1Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
        (GEONAMES / "admin2Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
    )
    if names_rule:
        geonames.record_build(connection, [])
    connection.close()


# ------------------------------------------------------------------------------------------------
# The decision table, on recorded answers.
# ------------------------------------------------------------------------------------------------


class ParseTests(unittest.TestCase):
    def test_a_recorded_answer_parses(self) -> None:
        parsed = verify.parse_verify_answer(answer("verified_summit"))
        self.assertEqual(parsed["kind"], "pin")

    def test_a_bad_answer_is_no_answer(self) -> None:
        self.assertIsNone(verify.parse_verify_answer("not json"))
        self.assertIsNone(verify.parse_verify_answer("[]"))
        self.assertIsNone(verify.parse_verify_answer(json.dumps({"kind": "no_pin", "verdict": "agree"})))
        broken = json.loads(answer("verified_summit"))
        del broken["map_place"]
        self.assertIsNone(verify.parse_verify_answer(json.dumps(broken)), "a missing rule is never read as agree")

    def test_a_critic_answer_needs_a_decision(self) -> None:
        self.assertEqual(verify.parse_critic_answer(critic("kept_airport"))["decision"], "A")
        self.assertEqual(verify.parse_critic_answer(critic("critic_unclear"))["decision"], "unclear")
        self.assertIsNone(verify.parse_critic_answer(json.dumps({"decision": "maybe"})))
        self.assertIsNone(verify.parse_critic_answer("{"))


class FlagTests(unittest.TestCase):
    def points(self, case: str) -> list[str]:
        return [p["point"] for p in verify.disputed(verify.parse_verify_answer(answer(case)))]

    def test_full_agreement_is_no_flag(self) -> None:
        self.assertEqual(self.points("verified_summit"), [])
        self.assertEqual(self.points("fallback_kept_plant"), [])

    def test_each_disagreement_is_a_flag(self) -> None:
        self.assertEqual(self.points("kept_airport"), ["event_place"])
        self.assertEqual(self.points("overruled_namesake"), ["map_place"])
        self.assertEqual(self.points("overruled_date"), ["happened"])

    def test_unsure_is_a_flag(self) -> None:
        unsure = json.loads(answer("verified_summit"))
        unsure["precision"]["verdict"] = "unsure"
        self.assertEqual([p["point"] for p in verify.disputed(unsure)], ["precision"])

    def test_a_category_flag_needs_every_acceptable_category_to_be_unpinnable(self) -> None:
        # Round 1 dropped two right pins over society against politics. Two categories that both fit are
        # not an error; code decides what can pin.
        self.assertIn("category", self.points("overruled_court"), "courts_and_justice alone cannot pin")
        mixed = json.loads(answer("verified_summit"))
        mixed["category"]["acceptable"] = ["courts_and_justice", "crime_and_policing"]
        self.assertEqual(verify.disputed(mixed), [])
        mixed["category"]["acceptable"] = ["opinion_analysis"]
        self.assertEqual([p["point"] for p in verify.disputed(mixed)], ["category"])


class DecisionTableTests(unittest.TestCase):
    def decide(self, case: str, critic_case: str | None) -> str:
        points = verify.disputed(verify.parse_verify_answer(answer(case)))
        decision = verify.parse_critic_answer(critic(critic_case)) if critic_case else None
        return verify.decide(points, decision)

    def test_verified(self) -> None:
        self.assertEqual(self.decide("verified_summit", None), verify.VERIFIED)

    def test_kept_after_review(self) -> None:
        self.assertEqual(self.decide("kept_airport", "kept_airport"), verify.KEPT)

    def test_overruled(self) -> None:
        self.assertEqual(self.decide("overruled_namesake", "overruled_namesake"), verify.OVERRULED)
        self.assertEqual(self.decide("overruled_date", "overruled_date"), verify.OVERRULED)

    def test_split_is_dropped(self) -> None:
        self.assertEqual(self.decide("overruled_namesake", "critic_unclear"), verify.SPLIT)

    def test_a_flag_with_no_critic_answer_is_not_run(self) -> None:
        self.assertEqual(self.decide("overruled_namesake", None), verify.NOT_RUN)

    def test_only_verified_and_kept_send_a_pin(self) -> None:
        self.assertEqual(verify.PASSING, frozenset({verify.VERIFIED, verify.KEPT}))
        self.assertEqual(set(verify.OUTCOMES),
                         {verify.VERIFIED, verify.KEPT, verify.OVERRULED, verify.SPLIT, verify.NOT_RUN})


# ------------------------------------------------------------------------------------------------
# The stage end to end: a store, a gazetteer, recorded answers, and what headline-places sends after.
# ------------------------------------------------------------------------------------------------


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            data_dir=Path(tempfile.mkdtemp()), deepseek_api_key="sk-test", ingest_url=None,
            ingest_secret=None, daily_budget_usd=1.0, telegram_bot_token=None, telegram_chat_id=None,
            deadman_url=None, proxy=None,
        )
        write_gazetteer(self.settings.geonames_db)
        self.extract_hash = config.extract_config_hash()

    def args(self, **over) -> argparse.Namespace:
        values = {"hours": 48, "limit": 30, "model": "deepseek-flash", "dry_run": False}
        values.update(over)
        return argparse.Namespace(**values)

    def seed(self, story_id: str, *, name: str = "Paris", within: str | None = None, country: str = "FR",
             kind: str = "city", category: str = "politics_and_diplomacy", description: str | None = None) -> None:
        now = now_utc()
        with Store(self.settings.news_db) as store:
            with store.write() as db:
                db.execute(
                    """INSERT INTO stories (story_id, outlet, primary_alias, url, source_url, headline, description,
                                            published, first_seen_at, last_seen_at, status)
                       VALUES (?, 'bbc-world', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (story_id, story_id, f"https://example.org/{story_id}", f"https://example.org/{story_id}",
                     f"Leaders meet in {name}", description or f"Leaders met in {name} on Thursday.",
                     now, now, now, STATUS_FEED),
                )
                db.execute(
                    """INSERT INTO extractions (story_id, config_hash, accepted, is_physical_event, category,
                                                place_name, place_within, place_country, place_kind, place_quote,
                                                created_at)
                       VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)""",
                    (story_id, self.extract_hash, category, name, within, country, kind,
                     f"Leaders met in {name}", now),
                )

    def run_verify(self, client: FakeClient, **over) -> int:
        return verify.run(self.args(**over), self.settings, client=client)

    def sent(self) -> list[dict]:
        data, _ = headline_places.build_snapshot(self.settings, 48, "deepseek-flash")
        return data["items"]

    def rows(self) -> list[dict]:
        with Store(self.settings.news_db) as store:
            return [dict(row) for row in store.query("SELECT * FROM verifications ORDER BY story_id")]

    def calls_recorded(self) -> list[str]:
        with Store(self.settings.news_db) as store:
            return [row["stage"] for row in store.query("SELECT stage FROM llm_calls ORDER BY call_id")]

    # The table, through the stage --------------------------------------------------------------

    def test_a_verified_pin_is_sent(self) -> None:
        self.seed("st_a")
        client = FakeClient([answer("verified_summit")])
        self.assertEqual(self.run_verify(client), 0)
        self.assertEqual([row["outcome"] for row in self.rows()], [verify.VERIFIED])
        self.assertEqual([item["place"]["id"] for item in self.sent()], [PARIS_FRANCE])
        self.assertEqual(self.calls_recorded(), ["verify"])

    def test_kept_after_review_is_sent(self) -> None:
        self.seed("st_a")
        client = FakeClient([answer("kept_airport")], [critic("kept_airport")])
        self.run_verify(client)
        self.assertEqual(self.rows()[0]["outcome"], verify.KEPT)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.calls_recorded(), ["verify", "adjudicate"])

    def test_overruled_and_split_are_not_sent(self) -> None:
        self.seed("st_a")
        self.seed("st_b")
        client = FakeClient([answer("overruled_namesake"), answer("overruled_namesake")],
                            [critic("overruled_namesake"), critic("critic_unclear")])
        self.run_verify(client)
        self.assertEqual(sorted(row["outcome"] for row in self.rows()), [verify.OVERRULED, verify.SPLIT])
        self.assertEqual(self.sent(), [])

    def test_the_row_keeps_the_claim_both_answers_and_the_flags(self) -> None:
        self.seed("st_a")
        self.run_verify(FakeClient([answer("overruled_namesake")], [critic("overruled_namesake")]))
        row = self.rows()[0]
        self.assertIn('CLAIM, KIND "pin"', row["claim"])
        self.assertEqual(json.loads(row["verify_answer"])["map_place"]["verdict"], "disagree")
        self.assertEqual([p["point"] for p in json.loads(row["flags"])], ["map_place"])
        self.assertEqual(json.loads(row["critic_answer"])["decision"], "B")
        self.assertEqual(row["geonames_id"], PARIS_FRANCE)

    # Never asked twice; the critic at most once ---------------------------------------------------

    def test_a_story_with_a_verdict_is_never_asked_again(self) -> None:
        self.seed("st_a")
        self.run_verify(FakeClient([answer("verified_summit")]))
        again = FakeClient()
        self.run_verify(again)
        self.assertEqual(again.calls, [])
        self.assertEqual(len(self.sent()), 1)

    def test_the_critic_runs_once_even_when_it_cannot_decide(self) -> None:
        self.seed("st_a")
        first = FakeClient([answer("overruled_namesake")], [critic("critic_unclear")])
        self.run_verify(first)
        self.assertEqual((first.count("verify"), first.count("critic")), (1, 1))
        later = FakeClient()
        self.run_verify(later)
        self.assertEqual(later.calls, [], "a split is final: no second critic, no fourth call")

    def test_a_changed_text_is_checked_again_and_the_old_verdict_does_not_send(self) -> None:
        self.seed("st_a")
        self.run_verify(FakeClient([answer("verified_summit")]))
        with Store(self.settings.news_db) as store:
            with store.write() as db:
                db.execute("UPDATE stories SET description = 'Talks were cancelled.' WHERE story_id = 'st_a'")
        self.assertEqual(self.sent(), [], "the verdict was for another text")
        again = FakeClient([answer("verified_summit")])
        self.run_verify(again)
        self.assertEqual(again.count("verify"), 1)
        self.assertEqual(len(self.sent()), 1)

    # Fails closed -------------------------------------------------------------------------------

    def test_no_verification_no_pin(self) -> None:
        self.seed("st_a")
        self.assertEqual(self.sent(), [])

    def test_no_budget_makes_no_call_and_sends_nothing(self) -> None:
        self.seed("st_a")
        broke = Settings(**{**self.settings.__dict__, "daily_budget_usd": 0.0})
        client = FakeClient([answer("verified_summit")])
        self.assertEqual(verify.run(self.args(), broke, client=client), 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.sent(), [])

    def test_the_budget_running_out_before_the_critic_is_no_pin(self) -> None:
        self.seed("st_a")
        tight = Settings(**{**self.settings.__dict__, "daily_budget_usd": 0.0015})
        client = FakeClient([answer("kept_airport")], [critic("kept_airport")], cost=0.002)
        verify.run(self.args(), tight, client=client)
        self.assertEqual((client.count("verify"), client.count("critic")), (1, 0))
        self.assertEqual(self.rows()[0]["outcome"], verify.NOT_RUN)
        self.assertEqual(self.sent(), [])

    def test_a_402_stops_the_stage_with_no_pin(self) -> None:
        self.seed("st_a")
        self.seed("st_b")
        client = FakeClient(error=BalanceError("DeepSeek answered 402: Insufficient Balance", 402))
        self.assertEqual(self.run_verify(client), 1)
        self.assertEqual(len(client.calls), 1, "the stage stops at the first 402")
        self.assertEqual([row["outcome"] for row in self.rows()], [verify.NOT_RUN])
        self.assertEqual(self.sent(), [])

    def test_an_unreachable_api_is_no_pin_and_is_tried_next_run(self) -> None:
        self.seed("st_a")
        self.assertEqual(self.run_verify(FakeClient(error=TransientError("unreachable"))), 0)
        self.assertEqual(self.sent(), [])
        self.run_verify(FakeClient([answer("verified_summit")]))
        self.assertEqual(len(self.sent()), 1)

    def test_two_bad_answers_are_no_pin_this_run(self) -> None:
        self.seed("st_a")
        client = FakeClient(["not json", '{"kind": "pin"}'])
        self.run_verify(client)
        self.assertEqual(client.count("verify"), 2, "one retry, then stop")
        self.assertEqual(client.count("critic"), 0)
        row = self.rows()[0]
        self.assertEqual((row["outcome"], row["attempts"]), (verify.NOT_RUN, 1))
        self.assertEqual(self.sent(), [])

    def test_one_bad_answer_then_a_good_one_is_used(self) -> None:
        self.seed("st_a")
        client = FakeClient(["not json", answer("verified_summit")])
        self.run_verify(client)
        self.assertEqual(self.rows()[0]["outcome"], verify.VERIFIED)
        self.assertEqual(len(self.sent()), 1)

    def test_two_bad_critic_answers_are_no_pin(self) -> None:
        self.seed("st_a")
        client = FakeClient([answer("kept_airport")], ["{}", "still not a decision"])
        self.run_verify(client)
        self.assertEqual(client.count("critic"), 2)
        self.assertEqual(self.rows()[0]["outcome"], verify.NOT_RUN)
        self.assertEqual(self.sent(), [])

    def test_bad_answers_are_given_up_after_the_last_run(self) -> None:
        self.seed("st_a")
        for _ in range(verify.MAX_BAD_RUNS):
            self.run_verify(FakeClient(["no", "no"]))
        last = FakeClient([answer("verified_summit")])
        self.run_verify(last)
        self.assertEqual(last.calls, [], "a story that answers badly every run stops costing money")
        self.assertEqual(self.sent(), [])

    def test_a_checked_pin_on_another_place_does_not_send(self) -> None:
        self.seed("st_a")
        self.run_verify(FakeClient([answer("verified_summit")]))
        with Store(self.settings.news_db) as store:
            with store.write() as db:
                db.execute("UPDATE verifications SET geonames_id = 1 WHERE story_id = 'st_a'")
        self.assertEqual(self.sent(), [])

    # What is never asked ------------------------------------------------------------------------

    def test_a_court_or_opinion_story_is_dropped_with_no_model_call(self) -> None:
        self.seed("st_court", category="courts_and_justice")
        self.seed("st_opinion", category="opinion_analysis")
        client = FakeClient()
        self.run_verify(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.sent(), [])

    def test_no_place_is_never_asked_about(self) -> None:
        self.seed("st_nowhere", name="Atlantis")
        client = FakeClient()
        self.run_verify(client)
        self.assertEqual(client.calls, [], "verification only removes pins: a story with no pin has nothing to check")

    def test_a_dry_run_calls_nothing_and_writes_nothing(self) -> None:
        self.seed("st_a")
        client = FakeClient()
        self.assertEqual(self.run_verify(client, dry_run=True), 0)
        self.assertEqual((client.calls, self.rows()), ([], []))

    # The venue fallback -------------------------------------------------------------------------

    def test_a_venue_geonames_lacks_is_pinned_on_its_city_at_area_after_the_verifier(self) -> None:
        self.seed("st_plant", name="Atlantis Steel Works", within="Paris", kind="venue")
        client = FakeClient([answer("fallback_kept_plant")])
        self.run_verify(client)
        claim = self.rows()[0]["claim"]
        self.assertIn("The venue is not in GeoNames", claim)
        self.assertIn("Pin precision: area", claim)
        items = self.sent()
        self.assertEqual([(item["place"]["id"], item["place"]["precision"]) for item in items], [(PARIS_FRANCE, "area")])
        self.assertEqual(set(items[0]["place"]), {"id", "name", "lat", "lon", "precision"})

    def test_a_venue_fallback_the_critic_overrules_is_not_sent(self) -> None:
        self.seed("st_airport", name="Atlantis Executive Airport", within="Paris", kind="venue")
        self.run_verify(FakeClient([answer("fallback_overruled_outside_city")],
                                   [critic("fallback_overruled_outside_city")]))
        self.assertEqual(self.sent(), [])

    def test_a_city_that_climbs_is_still_no_place(self) -> None:
        self.seed("st_suburb", name="Atlantis", within="Paris", kind="city")
        client = FakeClient()
        self.run_verify(client)
        self.assertEqual(client.calls, [])

    # Counts ------------------------------------------------------------------------------------

    def test_status_and_health_count_the_outcomes_per_day(self) -> None:
        from newsfeed import status

        self.seed("st_a")
        self.seed("st_b")
        self.run_verify(FakeClient([answer("verified_summit"), answer("overruled_namesake")],
                                   [critic("overruled_namesake")]))
        today = now_utc()[:10]
        with Store(self.settings.news_db) as store:
            counts = verify.outcome_counts(store)
            report = status.collect(store)
        self.assertEqual(counts[today], {verify.VERIFIED: 1, verify.OVERRULED: 1})
        self.assertEqual(report["verifications"], counts)


class HealthCountTests(unittest.TestCase):
    def test_health_json_carries_the_day_outcome_counts(self) -> None:
        from datetime import datetime, timezone

        from newsfeed import health

        settings = Settings(
            data_dir=Path(tempfile.mkdtemp()), deepseek_api_key=None, ingest_url=None, ingest_secret=None,
            daily_budget_usd=1.0, telegram_bot_token=None, telegram_chat_id=None, deadman_url=None, proxy=None,
        )
        now = datetime.now(timezone.utc)
        with Store(settings.news_db) as store:
            with store.write() as db:
                db.execute(
                    """INSERT INTO stories (story_id, outlet, primary_alias, first_seen_at, last_seen_at, status)
                       VALUES ('st_a', 'bbc-world', 'st_a', ?, ?, 'feed')""", (now_utc(), now_utc()))
                db.execute(
                    """INSERT INTO verifications (story_id, extract_hash, resolve_hash, verify_hash, input_hash,
                                                  claim, outcome, created_at, updated_at)
                       VALUES ('st_a', 'e', 'r', 'v', 'i', 'claim', 'verified', ?, ?)""", (now_utc(), now_utc()))
        health.run(argparse.Namespace(dry_run=False, no_production=True), settings, post=lambda text: True, now=now)
        saved = json.loads(settings.health_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["verifications"], {now_utc()[:10]: {"verified": 1}})


# ------------------------------------------------------------------------------------------------
# Hashes and the migration.
# ------------------------------------------------------------------------------------------------


class HashTests(unittest.TestCase):
    def test_the_extract_hash_does_not_move(self) -> None:
        # No extract prompt or setting changes, so no stored answer is thrown away and nothing is paid twice.
        self.assertEqual(config.extract_config_hash(), "3e17b96e25b937c5")

    def test_the_verify_hash_covers_both_prompts_the_model_and_the_effort(self) -> None:
        components = config.verify_components()
        self.assertEqual(components["model"], "deepseek-flash")
        self.assertEqual(components["thinking"], "enabled")
        self.assertEqual(components["reasoning_effort"], "max")
        self.assertEqual(components["verify_system"], prompts.VERIFY_SYSTEM)
        self.assertEqual(components["adjudicate_system"], prompts.ADJUDICATE_SYSTEM)
        self.assertEqual(components["critic_calls_max"], 1)
        self.assertIn("CLAIM, KIND", components["pin_claim_example"])
        self.assertIn("not in GeoNames", components["fallback_claim_example"])
        self.assertRegex(config.verify_config_hash(), r"^[0-9a-f]{16}$")

    def test_a_prompt_change_moves_the_verify_hash(self) -> None:
        from unittest.mock import patch

        before = config.verify_config_hash()
        with patch.object(prompts, "VERIFY_SYSTEM", prompts.VERIFY_SYSTEM + " "):
            self.assertNotEqual(config.verify_config_hash(), before)
        with patch.object(prompts, "ADJUDICATE_SYSTEM", prompts.ADJUDICATE_SYSTEM + " "):
            self.assertNotEqual(config.verify_config_hash(), before)
        self.assertNotEqual(config.verify_config_hash("deepseek-other"), before)

    def test_the_placement_hash_moves_with_every_part(self) -> None:
        from unittest.mock import patch

        from newsfeed import taxonomy

        base = config.placement_config_hash("e", "r", "v")
        self.assertRegex(base, r"^[0-9a-f]{16}$")
        self.assertNotEqual(config.placement_config_hash("e2", "r", "v"), base)
        self.assertNotEqual(config.placement_config_hash("e", "r2", "v"), base)
        self.assertNotEqual(config.placement_config_hash("e", "r", "v2"), base)
        flipped = dict(taxonomy.pin_possible_map(), courts_and_justice=True)
        with patch.object(taxonomy, "pin_possible_map", return_value=flipped):
            self.assertNotEqual(config.placement_config_hash("e", "r", "v"), base)
        with patch.object(headline_places, "FALLBACK_KINDS", frozenset({"venue", "street"})):
            self.assertNotEqual(config.placement_config_hash("e", "r", "v"), base)

    def test_the_cluster_hash_moves_with_the_resolve_hash(self) -> None:
        self.assertNotEqual(config.cluster_config_hash("r1", "e"), config.cluster_config_hash("r2", "e"))

    def test_the_snapshot_carries_the_placement_hash(self) -> None:
        settings = Settings(
            data_dir=Path(tempfile.mkdtemp()), deepseek_api_key=None, ingest_url=None, ingest_secret=None,
            daily_budget_usd=1.0, telegram_bot_token=None, telegram_chat_id=None, deadman_url=None, proxy=None,
        )
        write_gazetteer(settings.geonames_db)
        data, hashes = headline_places.build_snapshot(settings, 48, "deepseek-flash")
        self.assertEqual(data["configHash"], hashes.placement)
        self.assertEqual(hashes.extract, config.extract_config_hash())
        self.assertEqual(hashes.placement, config.placement_config_hash(hashes.extract, hashes.resolve, hashes.verify))
        self.assertNotEqual(data["configHash"], hashes.extract)


class MigrationTests(unittest.TestCase):
    def test_migration_5_adds_the_verifications_table(self) -> None:
        from newsfeed import store as store_module

        self.assertEqual(store_module.SCHEMA_VERSION, 5)
        with Store(Path(tempfile.mkdtemp()) / "news.sqlite3") as store:
            columns = [row["name"] for row in store.query("PRAGMA table_info(verifications)")]
            keys = [row["name"] for row in store.query("PRAGMA table_info(verifications)") if row["pk"]]
        for column in ("story_id", "extract_hash", "resolve_hash", "verify_hash", "input_hash", "geonames_id",
                       "fallback", "claim", "verify_answer", "flags", "critic_answer", "outcome", "reason",
                       "attempts", "cost_usd", "created_at", "updated_at"):
            self.assertIn(column, columns)
        self.assertEqual(keys, ["story_id", "extract_hash", "resolve_hash", "verify_hash"])

    def test_a_store_at_version_4_moves_to_5_and_keeps_its_rows(self) -> None:
        from newsfeed import store as store_module

        path = Path(tempfile.mkdtemp()) / "news.sqlite3"
        connection = store_module.connect(path)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for version, script in store_module.MIGRATIONS[:4]:
            for statement in store_module.statements(script):
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version VALUES (?, ?)", (version, now_utc()))
        connection.execute(
            """INSERT INTO stories (story_id, outlet, primary_alias, first_seen_at, last_seen_at)
               VALUES ('st_kept', 'bbc', 'st_kept', ?, ?)""", (now_utc(), now_utc()))
        connection.execute("COMMIT")
        connection.close()
        with Store(path) as store:
            self.assertEqual(store.scalar("SELECT MAX(version) FROM schema_version"), 5)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM stories"), 1)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM verifications"), 0)


class GuardTests(unittest.TestCase):
    def test_the_checker_raises_before_a_call_when_the_budget_is_spent(self) -> None:
        with Store(Path(tempfile.mkdtemp()) / "news.sqlite3") as store:
            client = FakeClient([answer("verified_summit")])
            checker = verify.Checker(store, client, budget_usd=0.0, verify_hash="v")
            with self.assertRaises(BudgetReached):
                checker.ask(prompts.VERIFY_SYSTEM, "user", verify.parse_verify_answer, verify.STAGE, "st_a")
            self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
