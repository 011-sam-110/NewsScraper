"""The DeepSeek client: the retry rules of section 7.6, against recorded answers, and the prices.

Every answer here is synthetic, as section 8.2 requires, because this repo is public. No test in
this file touches the network.
"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import deepseek  # noqa: E402
from newsfeed.deepseek import (  # noqa: E402
    BalanceError,
    Client,
    FatalError,
    TransientError,
    Usage,
    cost_usd,
    is_peak,
)

FIXTURES = Path(__file__).parent / "fixtures" / "newsfeed"


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


class ScriptedTransport:
    """Answers a queued list of (status, headers, body). Records every request it was given."""

    def __init__(self, *answers: tuple[int, dict[str, str], bytes] | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes | None):
        self.requests.append(
            {"url": url, "headers": headers, "payload": json.loads(body) if body else None}
        )
        if not self.answers:
            raise AssertionError("the client asked for more answers than the test scripted")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(name: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, fixture(name)


def fail(status: int, name: str, headers: dict[str, str] | None = None):
    return status, headers or {}, fixture(name)


def client(*answers, **kwargs) -> tuple[Client, ScriptedTransport, list[float]]:
    """A client whose sleeps are recorded instead of taken, so the tests run instantly."""
    slept: list[float] = []
    transport = ScriptedTransport(*answers)
    made = Client(
        "sk-test-key",
        transport=transport,
        sleep=slept.append,
        jitter=lambda: 0.5,  # fixed jitter, so backoff is predictable
        clock=lambda: datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),  # a peak hour
        **kwargs,
    )
    return made, transport, slept


class RequestShapeTests(unittest.TestCase):
    def test_the_request_matches_the_design(self) -> None:
        made, transport, _ = client(ok("chat_success"))
        made.complete("system text, mentioning json", "the article")
        sent = transport.requests[0]
        self.assertEqual(sent["url"], deepseek.CHAT_URL)
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-test-key")
        payload = sent["payload"]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["max_tokens"], 800)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])

    def test_the_system_message_comes_first_and_never_varies(self) -> None:
        # Caching only pays if nothing that varies sits before the article (section 7.6).
        made, transport, _ = client(ok("chat_success"), ok("chat_success"))
        made.complete("fixed system", "article one")
        made.complete("fixed system", "article two")
        first, second = transport.requests
        self.assertEqual(first["payload"]["messages"][0], second["payload"]["messages"][0])
        self.assertNotEqual(first["payload"]["messages"][1], second["payload"]["messages"][1])

    def test_a_client_with_no_key_is_refused_before_any_request(self) -> None:
        with self.assertRaises(FatalError):
            Client("")

    def test_json_object_can_be_turned_off(self) -> None:
        made, transport, _ = client(ok("chat_success"))
        made.complete("s", "u", json_object=False)
        self.assertNotIn("response_format", transport.requests[0]["payload"])


class SuccessTests(unittest.TestCase):
    def test_an_accepted_answer_carries_everything_the_llm_calls_row_needs(self) -> None:
        made, _, slept = client(ok("chat_success"))
        answer = made.complete("s", "u")
        self.assertIn("attack_or_violent_crime", answer.content)
        self.assertEqual(answer.model, "deepseek-flash")
        self.assertEqual(answer.finish_reason, "stop")
        self.assertEqual(answer.attempts, 1)
        self.assertFalse(answer.truncated)
        self.assertEqual(slept, [])
        self.assertEqual(answer.usage.cache_hit_tokens, 1024)
        self.assertEqual(answer.usage.cache_miss_tokens, 176)
        self.assertGreater(answer.cost_usd, 0)

    def test_the_model_string_the_answer_carries_is_the_one_recorded(self) -> None:
        body = json.loads(fixture("chat_success"))
        body["model"] = "deepseek-flash-260910"
        made, _, _ = client((200, {}, json.dumps(body).encode()))
        self.assertEqual(made.complete("s", "u").model, "deepseek-flash-260910")

    def test_an_answer_without_cache_fields_is_priced_as_all_miss(self) -> None:
        made, _, _ = client(ok("chat_no_cache_fields"))
        usage = made.complete("s", "u").usage
        self.assertEqual(usage.cache_miss_tokens, 500)
        self.assertEqual(usage.cache_hit_tokens, 0)

    def test_models_lists_the_ids(self) -> None:
        made, _, _ = client(ok("models"))
        self.assertEqual(made.models(), ["deepseek-flash", "deepseek-v4-pro"])


class EmptyContentTests(unittest.TestCase):
    def test_empty_content_is_retried_and_then_accepted(self) -> None:
        made, transport, slept = client(
            ok("chat_empty_content"), ok("chat_empty_content"), ok("chat_success")
        )
        answer = made.complete("s", "u")
        self.assertEqual(answer.attempts, 3)
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(len(slept), 2)

    def test_empty_content_every_time_leaves_the_work_for_the_next_run(self) -> None:
        made, _, _ = client(*[ok("chat_empty_content")] * 4)
        with self.assertRaises(TransientError) as caught:
            made.complete("s", "u")
        self.assertIn("empty content", str(caught.exception))

    def test_whitespace_only_content_counts_as_empty(self) -> None:
        body = json.loads(fixture("chat_success"))
        body["choices"][0]["message"]["content"] = "   \n  "
        made, _, _ = client((200, {}, json.dumps(body).encode()), ok("chat_success"))
        self.assertEqual(made.complete("s", "u").attempts, 2)


class LengthTests(unittest.TestCase):
    def test_a_cut_off_answer_is_retried_once_with_max_tokens_doubled(self) -> None:
        made, transport, _ = client(ok("chat_length"), ok("chat_success"))
        answer = made.complete("s", "u", max_tokens=800)
        self.assertEqual([r["payload"]["max_tokens"] for r in transport.requests], [800, 1600])
        self.assertEqual(answer.finish_reason, "stop")

    def test_cut_off_twice_is_handed_back_for_the_schema_check_to_reject(self) -> None:
        made, transport, _ = client(ok("chat_length"), ok("chat_length"))
        answer = made.complete("s", "u")
        self.assertTrue(answer.truncated)
        self.assertEqual(len(transport.requests), 2)


class FatalTests(unittest.TestCase):
    def test_402_stops_every_model_stage(self) -> None:
        made, transport, _ = client(fail(402, "error_402"))
        with self.assertRaises(BalanceError) as caught:
            made.complete("s", "u")
        self.assertIn("Insufficient Balance", str(caught.exception))
        self.assertEqual(caught.exception.status, 402)
        self.assertEqual(len(transport.requests), 1)  # never retried

    def test_a_balance_error_is_also_a_fatal_error(self) -> None:
        # So a stage that catches FatalError to stop and alert also stops on 402.
        self.assertTrue(issubclass(BalanceError, FatalError))

    def test_401_is_fatal_and_not_retried(self) -> None:
        made, transport, _ = client(fail(401, "error_401"))
        with self.assertRaises(FatalError) as caught:
            made.complete("s", "u")
        self.assertNotIsInstance(caught.exception, BalanceError)
        self.assertEqual(len(transport.requests), 1)

    def test_400_and_422_are_fatal(self) -> None:
        for status in (400, 422):
            with self.subTest(status=status):
                made, _, _ = client(fail(status, "error_401"))
                with self.assertRaises(FatalError):
                    made.complete("s", "u")


class RetryTests(unittest.TestCase):
    def test_429_then_success(self) -> None:
        made, transport, slept = client(fail(429, "error_429"), ok("chat_success"))
        self.assertEqual(made.complete("s", "u").attempts, 2)
        self.assertEqual(len(slept), 1)

    def test_503_then_success(self) -> None:
        made, _, slept = client(fail(503, "error_503"), fail(503, "error_503"), ok("chat_success"))
        self.assertEqual(made.complete("s", "u").attempts, 3)
        self.assertEqual(len(slept), 2)

    def test_five_failures_leave_the_work_for_the_next_run(self) -> None:
        made, transport, _ = client(*[fail(503, "error_503")] * 5)
        with self.assertRaises(TransientError) as caught:
            made.complete("s", "u")
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(caught.exception.status, 503)

    def test_a_connection_error_is_retried(self) -> None:
        made, _, slept = client(OSError("connection reset"), ok("chat_success"))
        self.assertEqual(made.complete("s", "u").attempts, 2)
        self.assertEqual(len(slept), 1)

    def test_a_timeout_is_retried_and_then_gives_up(self) -> None:
        made, _, _ = client(*[TimeoutError("timed out")] * 5)
        with self.assertRaises(TransientError):
            made.complete("s", "u")

    def test_200_that_is_not_json_is_retried(self) -> None:
        page = b"<html>Just a moment...</html>"
        made, _, _ = client((200, {}, page), ok("chat_success"))
        self.assertEqual(made.complete("s", "u").attempts, 2)

    def test_backoff_grows_and_is_capped(self) -> None:
        made, _, _ = client(ok("chat_success"))
        delays = [made.backoff(n) for n in range(1, 8)]
        self.assertEqual(delays, sorted(delays))
        self.assertLessEqual(max(delays), deepseek.BACKOFF_CAP_SECONDS)

    def test_retry_after_is_honoured(self) -> None:
        made, _, slept = client(
            fail(429, "error_429", {"Retry-After": "7"}), ok("chat_success")
        )
        made.complete("s", "u")
        self.assertEqual(slept, [7.0])

    def test_a_nonsense_retry_after_falls_back_to_the_backoff(self) -> None:
        made, _, slept = client(
            fail(429, "error_429", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), ok("chat_success")
        )
        made.complete("s", "u")
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)


class PriceTests(unittest.TestCase):
    def test_peak_hours_are_weekday_0100_to_0400_and_0600_to_1000_utc(self) -> None:
        tuesday = lambda hour: datetime(2026, 9, 15, hour, tzinfo=timezone.utc)  # noqa: E731
        self.assertFalse(is_peak(tuesday(0)))
        self.assertTrue(is_peak(tuesday(1)))
        self.assertTrue(is_peak(tuesday(3)))
        self.assertFalse(is_peak(tuesday(4)))
        self.assertFalse(is_peak(tuesday(5)))
        self.assertTrue(is_peak(tuesday(6)))
        self.assertTrue(is_peak(tuesday(9)))
        self.assertFalse(is_peak(tuesday(10)))

    def test_the_weekend_is_always_off_peak(self) -> None:
        self.assertFalse(is_peak(datetime(2026, 9, 13, 8, tzinfo=timezone.utc)))  # Sunday
        self.assertFalse(is_peak(datetime(2026, 9, 12, 8, tzinfo=timezone.utc)))  # Saturday

    def test_off_peak_is_half_of_peak(self) -> None:
        usage = Usage(prompt_tokens=1000, completion_tokens=200, cache_hit_tokens=800, cache_miss_tokens=200)
        peak = cost_usd("deepseek-flash", usage, datetime(2026, 9, 15, 8, tzinfo=timezone.utc))
        off = cost_usd("deepseek-flash", usage, datetime(2026, 9, 15, 5, tzinfo=timezone.utc))
        self.assertAlmostEqual(off * 2, peak, places=10)

    def test_a_cache_hit_is_far_cheaper_than_a_miss(self) -> None:
        when = datetime(2026, 9, 15, 8, tzinfo=timezone.utc)
        hit = cost_usd("deepseek-flash", Usage(cache_hit_tokens=1_000_000), when)
        miss = cost_usd("deepseek-flash", Usage(cache_miss_tokens=1_000_000), when)
        self.assertAlmostEqual(hit, 0.006)
        self.assertAlmostEqual(miss, 0.30)

    def test_an_unknown_model_is_priced_at_the_dearest_known_rate(self) -> None:
        # A model this build has never seen must not be able to spend past the daily cap by
        # pricing as free.
        self.assertEqual(deepseek.prices_for("deepseek-v5"), deepseek.PRICES["deepseek-v4-pro"])

    def test_a_story_costs_about_what_the_design_expects(self) -> None:
        # Section 13 estimates roughly $0.0008 a story at peak rates.
        usage = Usage(prompt_tokens=1500, completion_tokens=200, cache_hit_tokens=1200, cache_miss_tokens=300)
        peak = cost_usd("deepseek-flash", usage, datetime(2026, 9, 15, 8, tzinfo=timezone.utc))
        self.assertLess(peak, 0.0008)


if __name__ == "__main__":
    unittest.main()
