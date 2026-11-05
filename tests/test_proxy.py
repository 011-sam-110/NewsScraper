"""The proxy layer: direct first, one retry through the gateway on a block, and the password never in a log.

The fake proxy URL below carries a password that no real system uses. Every test asserts that this
string cannot reach an exception message, a cause chain, a log line or the store.
"""

import contextlib
import io
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import common  # noqa: E402

SECRET = "pR0xyS3cretValue"
USER = "user-sampo-cc-gb"
PROXY = f"http://{USER}:{SECRET}@thehub.example.invalid:8080"


class FakeResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.url, self.body = url, body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self) -> bytes:
        return self.body


def http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, "blocked", {}, io.BytesIO(b""))


def everything_printed(error: BaseException) -> str:
    """What a traceback would show: the message and every chained cause and context."""
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


class ProxyRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {common.PROXY_ENV: PROXY})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(common.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_direct_answer_never_touches_the_proxy(self) -> None:
        calls = []

        def fake_open(request, via_proxy=False):
            calls.append(via_proxy)
            return FakeResponse(request.full_url, b"ok")

        with mock.patch.object(common, "open_url", side_effect=fake_open):
            self.assertEqual(common.http_get("https://feeds.example.org/rss"), b"ok")
        self.assertEqual(calls, [False])

    def test_a_block_is_retried_once_through_the_proxy(self) -> None:
        calls = []

        def fake_open(request, via_proxy=False):
            calls.append(via_proxy)
            if not via_proxy:
                raise http_error(request.full_url, 403)
            return FakeResponse(request.full_url, b"through")

        with mock.patch.object(common, "open_url", side_effect=fake_open), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(common.http_get("https://feeds.example.org/rss"), b"through")
        self.assertEqual(calls, [False, True])

    def test_a_not_found_is_not_retried(self) -> None:
        calls = []

        def fake_open(request, via_proxy=False):
            calls.append(via_proxy)
            raise http_error(request.full_url, 404)

        with mock.patch.object(common, "open_url", side_effect=fake_open):
            with self.assertRaises(common.FetchError) as caught:
                common.http_get("https://feeds.example.org/rss")
        self.assertEqual(calls, [False])
        self.assertEqual(caught.exception.status, 404)

    def test_no_proxy_configured_means_no_retry(self) -> None:
        calls = []

        def fake_open(request, via_proxy=False):
            calls.append(via_proxy)
            raise http_error(request.full_url, 429)

        with mock.patch.dict(os.environ, {common.PROXY_ENV: ""}), \
                mock.patch.object(common, "open_url", side_effect=fake_open):
            with self.assertRaises(common.FetchError):
                common.http_get("https://feeds.example.org/rss")
        self.assertEqual(calls, [False])


class GzipBodyTest(unittest.TestCase):
    """The Independent answers with a gzip body and Content-Encoding: gzip that nobody asked for
    (measured from provenance-1, 2026-11-05). urllib does not unpack it, so the feed read as
    "not valid XML: line 1, column 0"."""

    def test_a_gzip_body_is_unpacked(self) -> None:
        import gzip

        packed = FakeResponse("https://www.independent.co.uk/news/world/rss", gzip.compress(b"<rss/>"))
        packed.headers = {"Content-Encoding": "gzip"}
        with mock.patch.object(common, "open_url", return_value=packed):
            self.assertEqual(common.http_get("https://www.independent.co.uk/news/world/rss"), b"<rss/>")

    def test_a_plain_body_is_left_alone(self) -> None:
        with mock.patch.object(common, "open_url", return_value=FakeResponse("https://x.example", b"<rss/>")):
            self.assertEqual(common.http_get("https://x.example"), b"<rss/>")


class RedactionTest(unittest.TestCase):
    """The password must not reach a log, an exception, its cause chain, or the store."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {common.PROXY_ENV: PROXY})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(common.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def failing_proxy(self, request, via_proxy=False):
        if not via_proxy:
            raise http_error(request.full_url, 403)
        # What urllib and requests do: the proxy URL, password and all, in the error text.
        raise URLError(f"Tunnel connection failed via {PROXY}: 407 Proxy Authentication Required")

    def test_redact_removes_url_credentials_and_the_proxy_secrets(self) -> None:
        text = f"error from {PROXY} and again {SECRET} and {USER}"
        cleaned = common.redact(text)
        self.assertNotIn(SECRET, cleaned)
        self.assertNotIn(USER, cleaned)
        self.assertIn("thehub.example.invalid", cleaned)
        self.assertEqual(common.redact("https://a:b@host.example/x"), "https://***:***@host.example/x")

    def test_a_proxy_failure_keeps_the_password_out_of_the_exception_and_its_causes(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(common, "open_url", side_effect=self.failing_proxy), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(common.FetchError) as caught:
                common.http_get("https://feeds.example.org/rss")
        self.assertNotIn(SECRET, everything_printed(caught.exception))
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertNotIn(USER, everything_printed(caught.exception))

    def test_log_lines_are_redacted(self) -> None:
        out = io.StringIO()
        common.log(f"something about {PROXY}", out)
        self.assertNotIn(SECRET, out.getvalue())

    def test_a_scrape_error_reaches_the_store_without_the_password(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from newsfeed.store import Store  # noqa: PLC0415

        class Source:
            NAME = "examplefeed"
            SECTIONS = {"feed": "https://feeds.example.org/rss"}

            @staticmethod
            def list_page(section, page, size):
                return [] if page else common.parse_rss(common.http_get(Source.SECTIONS[section]))

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(common, "open_url", side_effect=self.failing_proxy), \
                contextlib.redirect_stderr(io.StringIO()) as stderr, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            totals, errors = common.scrape(Path(folder), [(Source, "feed")], 8, 0, 1, False)
            with Store(Path(folder) / "news.sqlite3") as store:
                store.start_outlet_run("examplefeed")
                store.finish_outlet_run("examplefeed", errors.get("examplefeed"), 0)
                stored = store.scalar("SELECT last_error FROM outlet_health WHERE outlet = ?", ("examplefeed",))
        self.assertIn("examplefeed", errors)
        for where in (errors["examplefeed"], stored or "", stderr.getvalue(), stdout.getvalue()):
            self.assertNotIn(SECRET, where)
            self.assertNotIn(USER, where)


if __name__ == "__main__":
    unittest.main()
