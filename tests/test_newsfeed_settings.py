"""Settings: the .env file, the data directory default and the refusal of synced folders."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsfeed import settings  # noqa: E402


class ReadEnvFileTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        folder = Path(tempfile.mkdtemp())
        path = folder / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_pairs_and_skips_comments(self) -> None:
        path = self.write("# a comment\n\nDEEPSEEK_API_KEY=sk-123\nexport OTHER = spaced \n")
        self.assertEqual(
            settings.read_env_file(path), {"DEEPSEEK_API_KEY": "sk-123", "OTHER": "spaced"}
        )

    def test_strips_matching_quotes(self) -> None:
        path = self.write('A="one two"\nB=\'three\'\nC="unmatched\n')
        self.assertEqual(settings.read_env_file(path), {"A": "one two", "B": "three", "C": '"unmatched'})

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(settings.read_env_file(Path(tempfile.mkdtemp()) / "none"), {})

    def test_line_without_equals_names_the_line(self) -> None:
        path = self.write("GOOD=1\nnonsense\n")
        with self.assertRaises(settings.SettingsError) as caught:
            settings.read_env_file(path)
        self.assertIn(":2", str(caught.exception))

    def test_real_environment_wins_over_the_file(self) -> None:
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-environment"}):
            with mock.patch.object(settings, "read_env_file", return_value={"DEEPSEEK_API_KEY": "from-file"}):
                self.assertEqual(settings.environment()["DEEPSEEK_API_KEY"], "from-environment")


class SyncedFolderTests(unittest.TestCase):
    def test_refuses_every_synced_folder_shape(self) -> None:
        for path in (
            "/home/sam/OneDrive/NewsScraper/data",
            "/home/sam/OneDrive - Acme Ltd/data",
            "/home/sam/Dropbox/data",
            "/Users/sam/Google Drive/My Drive/data",
            "C:/Users/sam/iCloud Drive/data",
        ):
            with self.subTest(path=path):
                with self.assertRaises(settings.SettingsError) as caught:
                    settings.check_data_dir(Path(path))
                self.assertIn("NEWSFEED_DATA_DIR", str(caught.exception))

    def test_allows_a_folder_that_only_looks_like_one(self) -> None:
        # "dropbox-notes" is a component of its own, not a Dropbox root.
        for path in ("/home/sam/dropboxnotes/data", "/srv/onedriveish/data"):
            with self.subTest(path=path):
                self.assertTrue(settings.check_data_dir(Path(path)).is_absolute())

    def test_load_refuses_a_synced_data_dir(self) -> None:
        with self.assertRaises(settings.SettingsError):
            settings.load({"NEWSFEED_DATA_DIR": "/home/sam/OneDrive/NewsScraper"})


class LoadTests(unittest.TestCase):
    def test_defaults(self) -> None:
        loaded = settings.load({})
        self.assertEqual(loaded.daily_budget_usd, settings.DEFAULT_DAILY_BUDGET_USD)
        self.assertIsNone(loaded.deepseek_api_key)
        self.assertTrue(loaded.data_dir.is_absolute())

    def test_database_paths_sit_in_the_data_dir(self) -> None:
        folder = tempfile.mkdtemp()
        loaded = settings.load({"NEWSFEED_DATA_DIR": folder})
        self.assertEqual(loaded.news_db.parent, Path(folder))
        self.assertEqual(loaded.news_db.name, "news.sqlite3")
        self.assertEqual(loaded.geonames_db.name, "geonames.sqlite3")
        self.assertEqual(loaded.health_file.name, "health.json")

    def test_budget_must_be_a_number(self) -> None:
        with self.assertRaises(settings.SettingsError):
            settings.load({"NEWSFEED_DAILY_BUDGET_USD": "cheap"})
        with self.assertRaises(settings.SettingsError):
            settings.load({"NEWSFEED_DAILY_BUDGET_USD": "-1"})

    def test_require_names_the_variable(self) -> None:
        loaded = settings.load({})
        with self.assertRaises(settings.SettingsError) as caught:
            loaded.require("deepseek_api_key")
        self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))
        self.assertEqual(settings.load({"DEEPSEEK_API_KEY": "sk-1"}).require("deepseek_api_key"), "sk-1")

    def test_blank_values_read_as_unset(self) -> None:
        self.assertIsNone(settings.load({"DEEPSEEK_API_KEY": "   "}).deepseek_api_key)


if __name__ == "__main__":
    unittest.main()
