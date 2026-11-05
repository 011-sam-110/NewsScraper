"""The Chrome flags the scheduled Reuters run depends on actually reach Chrome.

The failure this guards is not a parser that mis-splits a string. It is a parser that
works perfectly while nothing passes its output to playwright, which looks identical
from outside until a browser window appears on someone's screen every hour.
"""

import sys
import types
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import reuters  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_unset_is_no_flags(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(reuters.extra_chrome_args(), [])

    def test_blank_is_no_flags(self) -> None:
        with mock.patch.dict("os.environ", {"NEWS_SCRAPER_CHROME_ARGS": "   "}):
            self.assertEqual(reuters.extra_chrome_args(), [])

    def test_several_flags_split_on_spaces(self) -> None:
        with mock.patch.dict(
            "os.environ", {"NEWS_SCRAPER_CHROME_ARGS": "--window-position=-32000,-32000 --mute-audio"}
        ):
            self.assertEqual(
                reuters.extra_chrome_args(),
                ["--window-position=-32000,-32000", "--mute-audio"],
            )

    def test_a_quoted_flag_keeps_its_space(self) -> None:
        # shlex, not str.split: a flag whose value contains a space must survive whole.
        with mock.patch.dict("os.environ", {"NEWS_SCRAPER_CHROME_ARGS": '--user-agent="Mozilla 5.0"'}):
            self.assertEqual(reuters.extra_chrome_args(), ["--user-agent=Mozilla 5.0"])


class WiringTests(unittest.TestCase):
    """Drive get_browser_session against a fake playwright and read the real launch call."""

    def setUp(self) -> None:
        reuters._browser_session.clear()
        self.addCleanup(reuters._browser_session.clear)

        self.launch = mock.MagicMock()
        page = mock.MagicMock()
        page.evaluate.return_value = True  # window.Fusion is there, so no browser check.
        self.launch.return_value.new_context.return_value.new_page.return_value = page

        fake = types.ModuleType("playwright.sync_api")
        fake.Error = RuntimeError
        fake.sync_playwright = lambda: mock.MagicMock(
            start=lambda: mock.MagicMock(chromium=mock.MagicMock(launch=self.launch))
        )
        package = types.ModuleType("playwright")
        package.sync_api = fake
        patcher = mock.patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def launched_args(self) -> list[str]:
        reuters.get_browser_session()
        return list(self.launch.call_args.kwargs["args"])

    def test_the_extra_flags_are_passed_to_chrome(self) -> None:
        with mock.patch.dict("os.environ", {"NEWS_SCRAPER_CHROME_ARGS": "--window-position=-32000,-32000"}):
            self.assertIn("--window-position=-32000,-32000", self.launched_args())

    def test_the_automation_flag_is_still_passed(self) -> None:
        # The extra flags must be added to the anti-detection flag, never replace it.
        with mock.patch.dict("os.environ", {"NEWS_SCRAPER_CHROME_ARGS": "--mute-audio"}):
            self.assertIn("--disable-blink-features=AutomationControlled", self.launched_args())

    def test_no_setting_leaves_chrome_as_it_was(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.launched_args(), ["--disable-blink-features=AutomationControlled"])
            self.assertIsNone(self.launch.call_args.kwargs["proxy"])


class ProfileAndProxyTests(unittest.TestCase):
    """The logged-in profile (NEWS_SCRAPER_CHROME_PROFILE) and when the Chrome session uses the proxy."""

    PROXY = "http://someone:not-a-real-password@gateway.example.invalid:8080"

    def setUp(self) -> None:
        reuters._browser_session.clear()
        self.addCleanup(reuters._browser_session.clear)
        self.launch = mock.MagicMock()
        self.persistent = mock.MagicMock()
        page = mock.MagicMock()
        page.evaluate.return_value = True
        self.launch.return_value.new_context.return_value.new_page.return_value = page
        self.persistent.return_value.pages = [page]
        chromium = mock.MagicMock(launch=self.launch, launch_persistent_context=self.persistent)
        fake = types.ModuleType("playwright.sync_api")
        fake.Error = RuntimeError
        fake.sync_playwright = lambda: mock.MagicMock(start=lambda: mock.MagicMock(chromium=chromium))
        package = types.ModuleType("playwright")
        package.sync_api = fake
        patcher = mock.patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_profile_opens_a_persistent_context_in_that_folder(self) -> None:
        with mock.patch.dict("os.environ", {"NEWS_SCRAPER_CHROME_PROFILE": "/data/chrome-reuters"}, clear=True):
            reuters.get_browser_session()
        self.launch.assert_not_called()
        self.assertEqual(self.persistent.call_args.args[0], "/data/chrome-reuters")
        kwargs = self.persistent.call_args.kwargs
        self.assertEqual(kwargs["channel"], "chrome")
        self.assertFalse(kwargs["headless"])
        self.assertIn("--disable-blink-features=AutomationControlled", kwargs["args"])

    def test_the_logged_in_profile_stays_off_the_rotating_proxy_by_default(self) -> None:
        env = {"NEWS_SCRAPER_CHROME_PROFILE": "/data/chrome-reuters", "NEWS_PROXY_URL": self.PROXY}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(reuters.reuters_proxy_policy(), "off")
            reuters.get_browser_session()
        self.assertIsNone(self.persistent.call_args.kwargs["proxy"])

    def test_without_a_profile_the_first_session_is_direct_and_a_block_retries_through_the_proxy(self) -> None:
        direct, proxied = mock.MagicMock(), mock.MagicMock()
        # The page check passes (True), then the in-page request: 403 directly, 200 through the proxy.
        direct.evaluate.side_effect = [True, {"status": 403, "body": ""}]
        proxied.evaluate.side_effect = [True, {"status": 200, "body": "ok"}]
        self.launch.return_value.new_context.return_value.new_page.side_effect = [direct, proxied]
        with mock.patch.dict("os.environ", {"NEWS_PROXY_URL": self.PROXY}, clear=True):
            self.assertEqual(reuters.reuters_proxy_policy(), "retry")
            reuters.get_browser_session()
            self.assertIsNone(self.launch.call_args_list[0].kwargs["proxy"])
            self.assertEqual(reuters.fetch_in_browser("https://www.reuters.com/x", "application/json"), "ok")
        self.assertEqual(len(self.launch.call_args_list), 2)
        proxy = self.launch.call_args_list[1].kwargs["proxy"]
        self.assertEqual(proxy["server"], "http://gateway.example.invalid:8080")
        self.assertEqual(proxy["password"], "not-a-real-password")

    def test_always_puts_the_first_session_through_the_proxy(self) -> None:
        env = {"NEWS_PROXY_URL": self.PROXY, "NEWS_SCRAPER_REUTERS_PROXY": "always"}
        with mock.patch.dict("os.environ", env, clear=True):
            reuters.get_browser_session()
        self.assertEqual(self.launch.call_args.kwargs["proxy"]["server"], "http://gateway.example.invalid:8080")


if __name__ == "__main__":
    unittest.main()
