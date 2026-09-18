"""Cluster tests (M7). Section 7.8 of docs/ARCHITECTURE.md.

The test that matters is LondonStabbingsTest. Section 7.8 names it as the one that decides whether
this stage is worth running, and it is written so that it cannot pass for the wrong reason: it
asserts the candidate gate DID offer the pair to the model before asserting the two stories stayed
apart. A gate that quietly rejected the pair would keep them apart too, and would be hiding a bug
rather than proving one absent.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from newsfeed import cluster
from newsfeed.extract import BudgetReached
from newsfeed.prompts import build_cluster_user
from newsfeed.cluster import (
    CLUSTER_WINDOW_HOURS,
    ClusterFacts,
    Placement,
    StoryFacts,
    VERDICT_DIFFERENT,
    VERDICT_FOUNDER,
    VERDICT_SAME,
    VERDICT_UNSURE,
    candidate_reasons,
    headline_overlap,
    headline_tokens,
    make_cluster_id,
    parse_verdict,
    pin_decision,
    place_story,
    shared_entities,
    within_window,
)

BASE = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)


def at(hours: float) -> str:
    return (BASE + timedelta(hours=hours)).isoformat()


def story(story_id: str, headline: str, **kwargs) -> StoryFacts:
    """A located, pinnable, physical-event story in London unless the caller says otherwise."""
    fields = dict(
        outlet="bbc",
        primary_alias=f"alias-{story_id}",
        published=at(0),
        country="GB",
        latitude=51.5074,
        longitude=-0.1278,
        place_precision="city",
        pinnable=True,
        is_physical_event=True,
    )
    fields.update(kwargs)
    return StoryFacts(story_id=story_id, headline=headline, **fields)


class ScriptedJudge:
    """Stands in for the model. Answers from a table and records every pair it was asked about."""

    def __init__(self, answers: dict[tuple[str, str], str], default: str = VERDICT_DIFFERENT):
        self.answers = answers
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject: StoryFacts, founder: StoryFacts) -> str:
        pair = (subject.story_id, founder.story_id)
        self.calls.append(pair)
        return self.answers.get(pair, self.default)


class LondonStabbingsTest(unittest.TestCase):
    """Two different stabbings in London on the same day must stay two clusters."""

    def setUp(self) -> None:
        self.first = story("s1", "Man stabbed in Croydon, London, police say", published=at(0))
        self.second = story("s2", "Teenager stabbed in London, police say", published=at(6))
        self.judge = ScriptedJudge({("s2", "s1"): VERDICT_DIFFERENT})

    def test_the_gate_does_offer_the_pair_to_the_model(self) -> None:
        """Guards the test below. If the gate rejects the pair, the real check never runs."""
        founder = ClusterFacts(make_cluster_id("bbc", "alias-s1"), self.first, [self.first])
        self.assertTrue(candidate_reasons(self.second, founder))

    def test_two_stabbings_stay_two_clusters(self) -> None:
        opened = ClusterFacts(make_cluster_id("bbc", "alias-s1"), self.first, [self.first])
        placed = place_story(self.second, [opened], self.judge)

        self.assertTrue(placed.founded)
        self.assertNotEqual(placed.cluster_id, opened.cluster_id)
        self.assertEqual(self.judge.calls, [("s2", "s1")], "the model must be asked exactly once")

    def test_the_same_stabbing_reported_twice_does_join(self) -> None:
        """The other half of the pair. A gate that refused everything would pass the test above."""
        agreeing = ScriptedJudge({("s2", "s1"): VERDICT_SAME})
        opened = ClusterFacts(make_cluster_id("bbc", "alias-s1"), self.first, [self.first])
        placed = place_story(self.second, [opened], agreeing)

        self.assertFalse(placed.founded)
        self.assertEqual(placed.cluster_id, opened.cluster_id)
        self.assertEqual(placed.compared_to, "s1")


class ChainingTest(unittest.TestCase):
    """A matches B and B matches C, but A and C are different events."""

    def test_comparison_is_against_the_founder_not_the_newest_member(self) -> None:
        founder = story("a", "Fire at a warehouse in east London", published=at(0))
        joined = story("b", "Fire at a London warehouse spreads", published=at(2))
        third = story("c", "Fire crews tackle a second London blaze", published=at(4))

        judge = ScriptedJudge({("b", "a"): VERDICT_SAME, ("c", "b"): VERDICT_SAME})
        open_cluster = ClusterFacts(make_cluster_id("bbc", "alias-a"), founder, [founder, joined])

        placed = place_story(third, [open_cluster], judge)

        self.assertTrue(placed.founded, "C matched B, not the founder, so it must not join")
        self.assertEqual(judge.calls, [("c", "a")], "C must be compared with A, never with B")


class VerdictTest(unittest.TestCase):
    def test_only_same_joins(self) -> None:
        founder = story("a", "Explosion reported in central Paris", country="FR")
        subject = story("b", "Explosion in Paris injures four", country="FR", published=at(1))
        opened = ClusterFacts("nf_test", founder, [founder])

        for verdict in (VERDICT_DIFFERENT, VERDICT_UNSURE):
            with self.subTest(verdict=verdict):
                judge = ScriptedJudge({("b", "a"): verdict})
                self.assertTrue(place_story(subject, [opened], judge).founded)

        agreeing = ScriptedJudge({("b", "a"): VERDICT_SAME})
        self.assertFalse(place_story(subject, [opened], agreeing).founded)

    def test_an_unrecognised_answer_is_unsure_never_same(self) -> None:
        for payload in ({}, {"verdict": ""}, {"verdict": "yes"}, {"verdict": None}):
            with self.subTest(payload=payload):
                self.assertEqual(parse_verdict(payload), VERDICT_UNSURE)
        self.assertEqual(parse_verdict({"verdict": " Same "}), VERDICT_SAME)
        self.assertEqual(parse_verdict({"verdict": "DIFFERENT"}), VERDICT_DIFFERENT)


class LiveBlogTest(unittest.TestCase):
    """Section 7.8 step 6. A live blog is many events in one document."""

    def test_a_live_blog_never_joins_a_cluster(self) -> None:
        founder = story("a", "Floods hit northern Spain", country="ES")
        blog = story("b", "Spain floods: latest updates", country="ES", published=at(1),
                     format_flags=("live",))
        judge = ScriptedJudge({}, default=VERDICT_SAME)

        placed = place_story(blog, [ClusterFacts("nf_test", founder, [founder])], judge)

        self.assertTrue(placed.founded)
        self.assertEqual(judge.calls, [], "a live blog must not cost a model call")

    def test_nothing_joins_a_live_blog(self) -> None:
        blog = story("a", "Spain floods: latest updates", country="ES", format_flags=("live",))
        subject = story("b", "Floods hit northern Spain", country="ES", published=at(1))
        judge = ScriptedJudge({}, default=VERDICT_SAME)

        placed = place_story(subject, [ClusterFacts("nf_test", blog, [blog])], judge)

        self.assertTrue(placed.founded)
        self.assertEqual(judge.calls, [])

    def test_a_live_blog_cluster_is_not_a_pin(self) -> None:
        blog = story("a", "Spain floods: latest updates", country="ES", format_flags=("live",))
        self.assertEqual(pin_decision(ClusterFacts("nf_x", blog, [blog])), (False, "live_blog"))


class CandidateGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.founder = story("a", "Bridge collapse in Genoa kills two", country="IT",
                             latitude=44.4056, longitude=8.9463, entities=("Morandi Bridge",))
        self.cluster = ClusterFacts("nf_test", self.founder, [self.founder])

    def test_a_different_country_is_never_a_candidate(self) -> None:
        other = story("b", "Bridge collapse in Genoa kills two", country="FR", published=at(1),
                      latitude=44.4056, longitude=8.9463)
        self.assertEqual(candidate_reasons(other, self.cluster), [])

    def test_a_story_with_no_country_is_never_a_candidate(self) -> None:
        other = story("b", "Bridge collapse in Genoa kills two", country=None, published=at(1))
        self.assertEqual(candidate_reasons(other, self.cluster), [])

    def test_outside_the_window_is_never_a_candidate(self) -> None:
        late = story("b", "Bridge collapse in Genoa kills two", country="IT",
                     published=at(CLUSTER_WINDOW_HOURS + 1), latitude=44.4056, longitude=8.9463)
        self.assertEqual(candidate_reasons(late, self.cluster), [])

    def test_place_alone_is_enough(self) -> None:
        nearby = story("b", "Two dead after a structure gave way", country="IT", published=at(1),
                       latitude=44.42, longitude=8.95)
        reasons = candidate_reasons(nearby, self.cluster)
        self.assertEqual(len(reasons), 1)
        self.assertIn("place", reasons[0])

    def test_an_entity_alone_is_enough(self) -> None:
        far = story("b", "Inquiry opens into the disaster", country="IT", published=at(1),
                    latitude=41.9028, longitude=12.4964, entities=("morandi bridge",))
        reasons = candidate_reasons(far, self.cluster)
        self.assertEqual(len(reasons), 1)
        self.assertIn("morandi bridge", reasons[0])

    def test_a_headline_alone_is_enough(self) -> None:
        far = story("b", "Genoa bridge collapse: two killed", country="IT", published=at(1),
                    latitude=41.9028, longitude=12.4964)
        reasons = candidate_reasons(far, self.cluster)
        self.assertEqual(len(reasons), 1)
        self.assertIn("headline", reasons[0])

    def test_a_distant_unrelated_story_in_the_same_country_is_not_a_candidate(self) -> None:
        unrelated = story("b", "Olive harvest starts early in Puglia", country="IT", published=at(1),
                          latitude=41.1171, longitude=16.8719)
        self.assertEqual(candidate_reasons(unrelated, self.cluster), [])


class WindowTest(unittest.TestCase):
    def test_the_boundary_is_inclusive(self) -> None:
        self.assertTrue(within_window(at(0), at(CLUSTER_WINDOW_HOURS)))
        self.assertFalse(within_window(at(0), at(CLUSTER_WINDOW_HOURS + 0.01)))

    def test_a_missing_time_fails_rather_than_passes(self) -> None:
        """A story whose time we had to invent must not join an event on the invented time."""
        self.assertFalse(within_window(None, at(0)))
        self.assertFalse(within_window(at(0), None))
        self.assertFalse(within_window("not a date", at(0)))

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        self.assertTrue(within_window("2026-09-17T09:00:00", "2026-09-17T11:00:00Z"))

    def test_order_does_not_matter(self) -> None:
        self.assertEqual(within_window(at(0), at(10)), within_window(at(10), at(0)))


class PinRuleTest(unittest.TestCase):
    def test_a_founder_and_a_close_member_is_a_pin(self) -> None:
        founder = story("a", "Blast in London")
        near = story("b", "Blast in London", latitude=51.52, longitude=-0.13)
        self.assertEqual(pin_decision(ClusterFacts("nf_x", founder, [founder, near])), (True, None))

    def test_one_far_member_vetoes_the_pin(self) -> None:
        founder = story("a", "Blast in London")
        near = story("b", "Blast in London", latitude=51.52, longitude=-0.13)
        far = story("c", "Blast in London", latitude=55.9533, longitude=-3.1883)
        decision = pin_decision(ClusterFacts("nf_x", founder, [founder, near, far]))
        self.assertEqual(decision, (False, "members_far_apart"))

    def test_an_unlocated_member_does_not_veto(self) -> None:
        founder = story("a", "Blast in London")
        floating = story("b", "Reaction to the blast", latitude=None, longitude=None)
        self.assertEqual(pin_decision(ClusterFacts("nf_x", founder, [founder, floating])), (True, None))

    def test_a_founder_that_is_not_an_event_is_not_a_pin(self) -> None:
        founder = story("a", "What the blast tells us", is_physical_event=False)
        self.assertEqual(
            pin_decision(ClusterFacts("nf_x", founder, [founder])), (False, "founder_not_an_event")
        )

    def test_a_region_founder_is_not_a_pin(self) -> None:
        founder = story("a", "Wildfires across Andalusia", place_precision="region", pinnable=False)
        self.assertEqual(
            pin_decision(ClusterFacts("nf_x", founder, [founder])),
            (False, "founder_place_not_pinnable"),
        )

    def test_an_unlocated_founder_is_not_a_pin(self) -> None:
        founder = story("a", "Blast reported", latitude=None, longitude=None)
        self.assertEqual(
            pin_decision(ClusterFacts("nf_x", founder, [founder])),
            (False, "founder_place_not_pinnable"),
        )


class ClusterIdTest(unittest.TestCase):
    def test_the_id_is_stable_and_shaped_as_the_contract_says(self) -> None:
        first = make_cluster_id("bbc", "bbc:world:abc123")
        self.assertEqual(first, make_cluster_id("bbc", "bbc:world:abc123"))
        self.assertTrue(first.startswith("nf_"))
        self.assertEqual(len(first), 15)
        self.assertTrue(all(c in "0123456789abcdef" for c in first[3:]))

    def test_a_different_founder_gives_a_different_id(self) -> None:
        self.assertNotEqual(make_cluster_id("bbc", "a"), make_cluster_id("bbc", "b"))
        self.assertNotEqual(make_cluster_id("bbc", "a"), make_cluster_id("pbs", "a"))

    def test_the_separator_cannot_be_forged_by_a_field(self) -> None:
        """Without a separator, the pairs (ab, c) and (a, bc) would collide."""
        self.assertNotEqual(make_cluster_id("ab", "c"), make_cluster_id("a", "bc"))


class TokenTest(unittest.TestCase):
    def test_stop_words_and_short_fragments_are_dropped(self) -> None:
        self.assertEqual(headline_tokens("The fire is in a mill"), {"fire", "mill"})

    def test_overlap_is_symmetric(self) -> None:
        a, b = "Fire at a London mill", "London mill fire kills two"
        self.assertEqual(headline_overlap(a, b), headline_overlap(b, a))

    def test_an_empty_headline_overlaps_nothing(self) -> None:
        self.assertEqual(headline_overlap("", "London mill fire"), 0.0)
        self.assertEqual(headline_overlap("the a an", "London mill fire"), 0.0)

    def test_entities_are_compared_case_folded(self) -> None:
        self.assertEqual(shared_entities(["Keir Starmer"], ["keir starmer "]), {"keir starmer"})
        self.assertEqual(shared_entities(["", "  "], ["Keir Starmer"]), set())


class PlacementOrderTest(unittest.TestCase):
    def test_the_first_same_wins_and_stops_the_calls(self) -> None:
        first = story("a", "Quake strikes Osaka", country="JP")
        second = story("b", "Quake strikes Osaka", country="JP")
        subject = story("c", "Osaka quake: buildings damaged", country="JP", published=at(1))
        judge = ScriptedJudge({("c", "a"): VERDICT_SAME, ("c", "b"): VERDICT_SAME})

        placed = place_story(
            subject,
            [ClusterFacts("nf_first", first, [first]), ClusterFacts("nf_second", second, [second])],
            judge,
        )

        self.assertEqual(placed.cluster_id, "nf_first")
        self.assertEqual(judge.calls, [("c", "a")], "the second cluster must not be paid for")

    def test_a_non_candidate_costs_no_call(self) -> None:
        founder = story("a", "Quake strikes Osaka", country="JP")
        subject = story("b", "Olive harvest starts early", country="IT", published=at(1))
        judge = ScriptedJudge({}, default=VERDICT_SAME)

        placed = place_story(subject, [ClusterFacts("nf_x", founder, [founder])], judge)
        self.assertTrue(placed.founded)
        self.assertEqual(judge.calls, [])

    def test_the_first_story_of_all_founds_a_cluster(self) -> None:
        placed = place_story(story("a", "Quake strikes Osaka"), [], ScriptedJudge({}))
        expected = Placement(make_cluster_id("bbc", "alias-a"), VERDICT_FOUNDER, True, None, 0)
        self.assertEqual(placed, expected)



class FakeCompletion:
    """The fields ModelJudge reads off a real Completion, and nothing else.

    Written against newsfeed.deepseek.Completion deliberately: the first real run of this stage
    failed with AttributeError because the judge read `.text` and the class calls it `.content`,
    and no test touched the judge at all. CompletionShapeTest below pins the names.
    """

    def __init__(self, content: str, truncated: bool = False):
        self.content = content
        self.model = "deepseek-flash"
        self.finish_reason = "length" if truncated else "stop"
        self.usage = None
        self.cost_usd = 0.0001
        self.latency_ms = 12
        self.attempts = 1
        self.truncated = truncated


class FakeClient:
    def __init__(self, replies: list[FakeCompletion]):
        self.replies = list(replies)
        self.model = "deepseek-flash"
        self.seen: list[tuple[str, str]] = []

    def complete(self, system, user, **kwargs):
        self.seen.append((system, user))
        return self.replies.pop(0)


class FakeStore:
    def __init__(self, spent: float = 0.0):
        self.spent = spent
        self.recorded: list[tuple] = []

    def spend_today(self) -> float:
        return self.spent

    def record_call(self, stage, model, outcome, **kwargs) -> None:
        self.recorded.append((stage, model, outcome, kwargs))


class CompletionShapeTest(unittest.TestCase):
    """The judge reads a real Completion, so the names it reads must exist on the real class."""

    def test_the_fake_carries_every_field_the_judge_reads(self) -> None:
        from newsfeed.deepseek import Completion

        real = set(Completion.__dataclass_fields__) | {"truncated"}
        for name in ("content", "model", "usage", "cost_usd", "latency_ms", "attempts", "truncated"):
            with self.subTest(field=name):
                self.assertIn(name, real, "ModelJudge reads this off a Completion")
                self.assertTrue(hasattr(FakeCompletion("{}"), name))


class ModelJudgeTest(unittest.TestCase):
    def judge_for(self, *replies: FakeCompletion, spent: float = 0.0):
        store = FakeStore(spent)
        client = FakeClient(list(replies))
        return cluster.ModelJudge(store, client, "cfg", budget_usd=3.0), store, client

    def test_a_clean_answer_is_returned_and_recorded(self) -> None:
        judge, store, client = self.judge_for(
            FakeCompletion('{"verdict": "same", "reason": "one fire"}')
        )
        verdict = judge(story("b", "Fire in Leeds"), story("a", "Leeds fire"))

        self.assertEqual(verdict, VERDICT_SAME)
        self.assertEqual(judge.calls, 1)
        self.assertAlmostEqual(judge.cost_usd, 0.0001)
        self.assertEqual(len(store.recorded), 1)
        self.assertIn("same", store.recorded[0][2])

    def test_unparsable_json_is_unsure_and_still_recorded(self) -> None:
        judge, store, _ = self.judge_for(FakeCompletion("not json at all"))
        self.assertEqual(judge(story("b", "x"), story("a", "y")), VERDICT_UNSURE)
        self.assertEqual(len(store.recorded), 1, "a call that happened must cost a visible row")

    def test_a_truncated_answer_says_so_in_the_recorded_outcome(self) -> None:
        judge, store, _ = self.judge_for(FakeCompletion('{"verdict": "sa', truncated=True))
        self.assertEqual(judge(story("b", "x"), story("a", "y")), VERDICT_UNSURE)
        self.assertIn("max_tokens", store.recorded[0][2])

    def test_a_json_array_is_unsure_not_a_crash(self) -> None:
        judge, _, _ = self.judge_for(FakeCompletion('["same"]'))
        self.assertEqual(judge(story("b", "x"), story("a", "y")), VERDICT_UNSURE)

    def test_the_budget_stops_the_call_before_it_is_paid_for(self) -> None:
        judge, store, client = self.judge_for(
            FakeCompletion('{"verdict": "same"}'), spent=3.0
        )
        with self.assertRaises(BudgetReached):
            judge(story("b", "x"), story("a", "y"))
        self.assertEqual(client.seen, [], "no request may be sent once the budget is gone")

    def test_the_founder_is_report_a_and_the_subject_is_report_b(self) -> None:
        """The prompt asks about A and B. Which is which has to be stable across runs."""
        judge, _, client = self.judge_for(FakeCompletion('{"verdict": "different"}'))
        judge(story("b", "Subject headline"), story("a", "Founder headline"))

        _, user = client.seen[0]
        self.assertLess(user.index("Founder headline"), user.index("Subject headline"))
        self.assertIn("Report A", user)
        self.assertIn("Report B", user)

    def test_the_article_text_is_never_sent(self) -> None:
        judge, _, client = self.judge_for(FakeCompletion('{"verdict": "different"}'))
        subject = story("b", "Subject headline", cluster_hint="a lorry hit a wall")
        judge(subject, story("a", "Founder headline"))

        _, user = client.seen[0]
        self.assertIn("a lorry hit a wall", user)
        self.assertLess(len(user), 2000, "the pair prompt must stay small: one call a candidate")


class PromptStoryTest(unittest.TestCase):
    def test_every_field_the_prompt_names_is_supplied(self) -> None:
        facts = story("a", "Headline", cluster_hint="hint", event_date="2026-09-17",
                      place_name="Croydon", entities=("Met Police",))
        shown = cluster.as_prompt_story(facts)
        self.assertEqual(
            set(shown), {"headline", "published", "event_date", "place", "cluster_hint", "key_entities"}
        )
        self.assertEqual(shown["place"], "Croydon")
        self.assertEqual(shown["key_entities"], ["Met Police"])

    def test_a_missing_field_reads_as_words_not_as_none(self) -> None:
        user = build_cluster_user(cluster.as_prompt_story(story("a", "H")), {})
        self.assertNotIn("None", user)
        self.assertIn("unknown", user)


class JsonListTest(unittest.TestCase):
    def test_a_malformed_column_does_not_stop_the_stage(self) -> None:
        self.assertEqual(cluster._json_list("not json"), [])
        self.assertEqual(cluster._json_list(None), [])
        self.assertEqual(cluster._json_list('["a", "b"]'), ["a", "b"])

    def test_non_string_members_are_read_as_text(self) -> None:
        self.assertEqual(cluster._json_list('["a", 2, null, {"x": 1}]'), ["a", "2"])


class RunOrderGuardTest(unittest.TestCase):
    """Cluster must refuse to run before resolve has placed the events it will read.

    This is not hypothetical. The first real run of this stage was done on a store where resolve
    had only ever been run with --dry-run, so `resolutions` was empty. Nothing failed: 2278 stories
    were clustered without a single coordinate, every one of the 153 event founders fell to
    `founder_place_not_pinnable`, and the rows were recorded as current under a config hash that
    covers the resolve CONFIG rather than whether resolve ever ran.
    """

    def setUp(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path

        from newsfeed import geonames
        from newsfeed.identity import now_utc
        from newsfeed.settings import Settings
        from newsfeed.store import Store

        self.sqlite3 = sqlite3
        self.Store = Store
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

        fixtures = Path(__file__).parent / "fixtures" / "geonames"
        connection = sqlite3.connect(self.settings.geonames_db)
        geonames.build_database(
            connection,
            (fixtures / "allCountries.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "alternateNamesV2.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "countryInfo.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "admin1Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "admin2Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
        )
        connection.close()

        self.extract_hash = cluster.extract_config_hash("deepseek-flash")
        now = now_utc()
        with Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as db:
                db.execute(
                    """INSERT INTO stories (story_id, outlet, primary_alias, headline, published,
                                            first_seen_at, last_seen_at, status)
                       VALUES ('st_1', 'bbc', 'st_1', 'Fire in Paris', ?, ?, ?, 'extracted')""",
                    (now, now, now),
                )
                db.execute(
                    """INSERT INTO extractions (story_id, config_hash, accepted, is_physical_event,
                                                place_name, place_country, place_kind, created_at)
                       VALUES ('st_1', ?, 1, 1, 'Paris', 'FR', 'city', ?)""",
                    (self.extract_hash, now),
                )

    def args(self, **over):
        fields = dict(limit=10, model="deepseek-flash", dry_run=False, allow_unresolved=False)
        fields.update(over)
        return argparse.Namespace(**fields)

    def placed_rows(self) -> int:
        with self.Store(self.settings.news_db) as store:
            store.migrate()
            return store.scalar("SELECT COUNT(*) FROM cluster_members")

    def test_an_unresolved_event_refuses_the_run(self) -> None:
        self.assertEqual(cluster.run(self.args(), self.settings), 2)
        self.assertEqual(self.placed_rows(), 0, "a refused run must write nothing")

    def test_the_refusal_names_the_command_that_fixes_it(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cluster.run(self.args(), self.settings)
        message = stderr.getvalue()
        self.assertIn("python -m newsfeed resolve", message)
        self.assertIn("1 physical events", message)

    def test_a_dry_run_is_refused_too(self) -> None:
        """A dry run that reports on a store cluster will not touch is worse than no estimate."""
        self.assertEqual(cluster.run(self.args(dry_run=True), self.settings), 2)

    def test_allow_unresolved_gets_past_it(self) -> None:
        """The escape hatch exists, and a dry run under it does not call the model."""
        code = cluster.run(self.args(dry_run=True, allow_unresolved=True), self.settings)
        self.assertEqual(code, 0)
        self.assertEqual(self.placed_rows(), 0)

    def test_a_resolved_event_does_not_trip_the_guard(self) -> None:
        from newsfeed import resolve as resolve_stage

        resolve_stage.run(argparse.Namespace(limit=10, story=None, dry_run=False), self.settings)
        code = cluster.run(self.args(dry_run=True), self.settings)
        self.assertEqual(code, 0, "resolve has placed it, so cluster may run")

    def test_a_non_event_never_trips_the_guard(self) -> None:
        """Only physical events with a place are resolve's job. World news has nothing to wait for."""
        with self.Store(self.settings.news_db) as store:
            store.migrate()
            with store.write() as db:
                db.execute(
                    "UPDATE extractions SET is_physical_event = 0, place_name = NULL"
                )
        self.assertEqual(cluster.run(self.args(dry_run=True), self.settings), 0)


class ShuffledRerunTest(unittest.TestCase):
    """Section 14, M7: cluster ids must be identical after a shuffled rerun.

    The claim is about the STAGE, so the stage is what runs here. Rows are inserted in a shuffled
    order into two separate stores and the whole of `cluster` is run over each. If the stage ever
    starts placing stories in the order it reads them rather than in publication order, the two
    runs disagree and this fails.

    The judge is scripted rather than live. Determinism of the model is a separate promise
    (temperature 0) and mixing the two would mean a flaky test could never say which broke.
    """

    STORIES = [
        # (id, outlet, headline, hours after base, country, lat, lon)
        ("st_a", "bbc", "Fire at a Croydon warehouse in London", 0, "GB", 51.3762, -0.0982),
        ("st_b", "guardian", "London warehouse fire in Croydon spreads", 2, "GB", 51.3762, -0.0982),
        ("st_c", "reuters", "Man stabbed in Camden, London", 5, "GB", 51.5390, -0.1426),
        ("st_d", "pbs", "Flooding hits Valencia", 7, "ES", 39.4699, -0.3763),
        ("st_e", "bbc", "Valencia flooding forces evacuations", 9, "ES", 39.4699, -0.3763),
        ("st_f", "nyt", "Quake felt across Osaka", 40, "JP", 34.6937, 135.5023),
    ]

    # Only the two genuine duplicates are the same event. Held as unordered pairs on purpose: a
    # judge that answers `same` for (b, a) but `different` for (a, b) hides which story founded the
    # cluster, so test_the_founder_is_the_earliest_story_not_the_first_inserted would pass whatever
    # the stage did. A real model does not care which of two reports it is shown first either.
    SAME_PAIRS = {frozenset({"st_a", "st_b"}), frozenset({"st_d", "st_e"})}

    def build_store(self, order: list[int]) -> "object":
        import tempfile
        from pathlib import Path

        from newsfeed import geonames
        from newsfeed.identity import now_utc
        from newsfeed.settings import Settings
        from newsfeed.store import Store

        settings = Settings(
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
        fixtures = Path(__file__).parent / "fixtures" / "geonames"
        connection = sqlite3.connect(settings.geonames_db)
        geonames.build_database(
            connection,
            (fixtures / "allCountries.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "alternateNamesV2.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "countryInfo.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "admin1Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
            (fixtures / "admin2Codes.sample.txt").read_text(encoding="utf-8").splitlines(),
        )
        connection.close()

        extract_hash = cluster.extract_config_hash("deepseek-flash")
        resolve_hash = self.resolve_hash(settings)
        now = now_utc()
        with Store(settings.news_db) as store:
            store.migrate()
            with store.write() as db:
                for index in order:
                    sid, outlet, headline, hours, country, lat, lon = self.STORIES[index]
                    db.execute(
                        """INSERT INTO stories (story_id, outlet, primary_alias, headline,
                                                published, first_seen_at, last_seen_at, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'extracted')""",
                        (sid, outlet, sid, headline, at(hours), now, now),
                    )
                    db.execute(
                        """INSERT INTO extractions (story_id, config_hash, accepted,
                                                    is_physical_event, place_name, place_country,
                                                    place_kind, key_entities, cluster_hint,
                                                    created_at)
                           VALUES (?, ?, 1, 1, 'Somewhere', ?, 'city', '[]', ?, ?)""",
                        (sid, extract_hash, country, headline, now),
                    )
                    db.execute(
                        """INSERT INTO resolutions (story_id, config_hash, extract_hash, resolved,
                                                    latitude, longitude, place_precision, pinnable,
                                                    reason, created_at)
                           VALUES (?, ?, ?, 1, ?, ?, 'city', 1, 'matched', ?)""",
                        (sid, resolve_hash, extract_hash, lat, lon, now),
                    )
        return settings

    def resolve_hash(self, settings) -> str:
        from newsfeed.geonames import Gazetteer, build_hash

        gazetteer = Gazetteer.open(settings.geonames_db)
        value = cluster.resolve_config_hash(build_hash(gazetteer.connection))
        gazetteer.connection.close()
        return value

    def judge(self, subject: StoryFacts, founder: StoryFacts) -> str:
        pair = frozenset({subject.story_id, founder.story_id})
        return VERDICT_SAME if pair in self.SAME_PAIRS else VERDICT_DIFFERENT

    def placement_of(self, order: list[int]) -> dict[str, str]:
        from newsfeed.store import Store

        settings = self.build_store(order)
        args = argparse.Namespace(
            limit=100, model="deepseek-flash", dry_run=False, allow_unresolved=False
        )
        code = cluster.run(args, settings, judge=self.judge)
        self.assertEqual(code, 0)
        with Store(settings.news_db) as store:
            store.migrate()
            rows = store.query("SELECT story_id, cluster_id FROM cluster_members")
        return {row["story_id"]: row["cluster_id"] for row in rows}

    def test_two_shuffles_give_the_same_cluster_ids(self) -> None:
        forward = self.placement_of([0, 1, 2, 3, 4, 5])
        shuffled = self.placement_of([5, 2, 4, 0, 3, 1])
        reversed_order = self.placement_of([5, 4, 3, 2, 1, 0])

        self.assertEqual(forward, shuffled)
        self.assertEqual(forward, reversed_order)
        self.assertEqual(len(forward), len(self.STORIES))

    def test_the_duplicates_merged_and_nothing_else_did(self) -> None:
        """Guards the test above: six ids that all differ would also compare equal."""
        placed = self.placement_of([0, 1, 2, 3, 4, 5])

        self.assertEqual(placed["st_a"], placed["st_b"], "the Croydon fire is one event")
        self.assertEqual(placed["st_d"], placed["st_e"], "the Valencia flood is one event")
        self.assertNotEqual(placed["st_a"], placed["st_c"], "a fire and a stabbing are not one")
        self.assertEqual(len(set(placed.values())), 4)

    def test_the_founder_is_the_earliest_story_not_the_first_inserted(self) -> None:
        """Which story founds a cluster decides its id, so it must not depend on insert order."""
        placed = self.placement_of([1, 0, 4, 3, 5, 2])
        self.assertEqual(placed["st_a"], make_cluster_id("bbc", "st_a"))
        self.assertEqual(placed["st_d"], make_cluster_id("pbs", "st_d"))

if __name__ == "__main__":
    unittest.main()


class NeverPinnedCategoryTests(unittest.TestCase):
    """A court proceeding is never a pin, however well it resolves. Section 3.

    This rule exists because of a measurement, not a hunch. On 2026-09-18 the store held 72 pins
    and 8 of them were `courts_and_justice`: a sentencing in Los Angeles for crimes in Syria, a
    trial in Paris, a plea in Miami, judges convening in Brasilia. The clearest was an acquittal in
    Malaysia pinned at the school, carrying the ORIGINAL stabbing's quote as its evidence, which is
    section 3's GDELT example almost word for word.

    Every one of those stories genuinely happened somewhere: a courtroom is a place and a hearing
    is an event. What makes them wrong is that the pin claims the story is about that place, and it
    is not. The story is about something that happened somewhere else, some time ago.
    """

    def facts(self, **kwargs) -> ClusterFacts:
        founder = story("s1", "A headline", **kwargs)
        return ClusterFacts("nf_x", founder, [founder])

    def test_a_court_story_is_world_news_with_a_reason(self) -> None:
        self.assertEqual(
            pin_decision(self.facts(category="courts_and_justice")),
            (False, "founder_category_never_pins"),
        )

    def test_the_same_story_in_another_category_still_pins(self) -> None:
        """Without this the first test would pass for any reason at all, including a broken
        fixture that could never pin in the first place."""
        self.assertEqual(pin_decision(self.facts(category="attack_or_violent_crime")), (True, None))
        self.assertEqual(pin_decision(self.facts(category="conflict")), (True, None))

    def test_a_missing_category_does_not_block_a_pin(self) -> None:
        """An extraction from before the category existed must not be silently unpinnable."""
        self.assertEqual(pin_decision(self.facts(category=None)), (True, None))
        self.assertEqual(pin_decision(self.facts(category="")), (True, None))

    def test_the_rule_reads_the_founder_not_a_member(self) -> None:
        """The pin claims to be about the founder, so the founder decides. A court report that
        joins a cluster founded on the attack itself must not veto the attack's pin."""
        founder = story("s1", "Man stabbed in Westminster", category="attack_or_violent_crime")
        court = story("s2", "Man charged over Westminster stabbing", category="courts_and_justice")
        self.assertEqual(pin_decision(ClusterFacts("nf_x", founder, [founder, court])), (True, None))

    def test_an_opinion_piece_is_not_a_pin_either(self) -> None:
        """`opinion_analysis` was already marked unpinnable in the taxonomy and nothing read the
        flag, so an opinion piece that resolved cleanly would have been pinned. Section 3 names
        commentary in the same breath as verdicts."""
        self.assertEqual(
            pin_decision(self.facts(category="opinion_analysis")),
            (False, "founder_category_never_pins"),
        )

    def test_the_rule_reads_the_taxonomy_rather_than_a_second_list(self) -> None:
        """`pin_possible` was declared and read by nothing. A separate list in this module would
        have left the dead flag in place and given two answers to one question."""
        from newsfeed.taxonomy import CATEGORIES, can_pin

        for entry in CATEGORIES:
            with self.subTest(category=entry.id):
                self.assertEqual(can_pin(entry.id), entry.pin_possible)

    def test_the_pin_flags_are_in_the_config_hash(self) -> None:
        """A rule that changes is_pin and not the hash is the worst of both: stored clusters keep
        the old answer, new ones get the new one, and nothing reports the disagreement."""
        from newsfeed.config import cluster_components

        components = cluster_components("resolve-hash", "extract-hash")
        self.assertIs(components["pin_possible"]["courts_and_justice"], False)
        self.assertIs(components["pin_possible"]["conflict"], True)

    def test_flipping_a_flag_moves_the_hash(self) -> None:
        """The guard that makes the one above mean something."""
        from unittest.mock import patch

        from newsfeed import taxonomy
        from newsfeed.config import cluster_config_hash

        before = cluster_config_hash("r", "e")
        flipped = dict(taxonomy.pin_possible_map(), conflict=False)
        with patch.object(taxonomy, "pin_possible_map", return_value=flipped):
            self.assertNotEqual(cluster_config_hash("r", "e"), before)
