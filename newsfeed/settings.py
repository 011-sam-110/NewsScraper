"""Settings for the pipeline: environment variables, an optional .env file and the data directory.

Every stage reads its settings here, so a bad configuration fails once, early, with one message.

The data directory check is not a nicety. This checkout lives under OneDrive on Sam's machine, and
file sync corrupts a SQLite write-ahead log, so a data directory inside a synced folder is refused
rather than allowed to destroy the store later.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

# A path component equal to one of these, or starting with one followed by a space, is a synced
# folder. Matching whole components keeps a folder called "dropbox-notes" out of the refusal.
SYNC_FOLDERS = ("onedrive", "dropbox", "google drive", "googledrive", "gdrive", "my drive", "icloud drive")

DEFAULT_DAILY_BUDGET_USD = 3.0


class SettingsError(RuntimeError):
    """A setting is missing or refused. The message says what to change."""


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=VALUE lines from a .env file. Blank lines and # comments are skipped.

    Values may be quoted. A real environment variable always wins over the file, so a one-off
    override on the command line does not need the file edited.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            raise SettingsError(f"{path}:{number}: expected KEY=VALUE, found {line!r}")
        name, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """The process environment with .env filling the gaps. Pass env in tests."""
    if env is not None:
        return dict(env)
    merged = read_env_file()
    merged.update(os.environ)
    return merged


def default_data_dir() -> Path:
    """Where the databases live when NEWSFEED_DATA_DIR is not set.

    Windows uses %LOCALAPPDATA%, which is never synced. Everywhere else follows the XDG default.
    """
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "NewsScraper"
        return Path.home() / "AppData" / "Local" / "NewsScraper"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "newsscraper"


def synced_component(path: Path) -> str | None:
    """The name of the first synced folder on the path, or None. Used to refuse a data directory."""
    for part in path.parts:
        name = part.lower()
        for marker in SYNC_FOLDERS:
            if name == marker or name.startswith(marker + " ") or name.startswith(marker + "-"):
                return part
    return None


def check_data_dir(path: Path) -> Path:
    """Refuse a data directory inside a synced folder. File sync corrupts SQLite write-ahead logs."""
    resolved = path.expanduser()
    # resolve() would follow a symlink out of a synced folder and hide the problem, so check the
    # written path as well as the resolved one.
    for candidate in (resolved, resolved.resolve() if resolved.exists() else resolved.absolute()):
        found = synced_component(candidate)
        if found:
            raise SettingsError(
                f"NEWSFEED_DATA_DIR is inside the synced folder {found!r} ({candidate}). "
                "File sync corrupts SQLite write-ahead logs. Point NEWSFEED_DATA_DIR at a local "
                f"folder, for example {default_data_dir()}."
            )
    return resolved.absolute()


@dataclass(frozen=True)
class Settings:
    """Everything the stages read. Built by load(); never mutated afterwards."""

    data_dir: Path
    deepseek_api_key: str | None
    ingest_url: str | None
    ingest_secret: str | None
    daily_budget_usd: float
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    deadman_url: str | None
    proxy: str | None

    @property
    def news_db(self) -> Path:
        return self.data_dir / "news.sqlite3"

    @property
    def geonames_db(self) -> Path:
        return self.data_dir / "geonames.sqlite3"

    @property
    def health_file(self) -> Path:
        return self.data_dir / "health.json"

    def require(self, field: str) -> str:
        """The value of a setting a stage cannot run without, or a SettingsError naming it."""
        value = getattr(self, field)
        if not value:
            variable = {
                "deepseek_api_key": "DEEPSEEK_API_KEY",
                "ingest_url": "NEWSFEED_INGEST_URL",
                "ingest_secret": "NEWSFEED_INGEST_SECRET",
            }.get(field, field.upper())
            raise SettingsError(f"{variable} is not set. Put it in {ENV_FILE} or the environment.")
        return str(value)

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def load(env: dict[str, str] | None = None) -> Settings:
    """Read the settings. Raises SettingsError for a data directory inside a synced folder."""
    values = environment(env)

    written = values.get("NEWSFEED_DATA_DIR", "").strip()
    data_dir = check_data_dir(Path(written) if written else default_data_dir())

    budget_text = values.get("NEWSFEED_DAILY_BUDGET_USD", "").strip()
    try:
        budget = float(budget_text) if budget_text else DEFAULT_DAILY_BUDGET_USD
    except ValueError as error:
        raise SettingsError(f"NEWSFEED_DAILY_BUDGET_USD is not a number: {budget_text!r}") from error
    if budget < 0:
        raise SettingsError("NEWSFEED_DAILY_BUDGET_USD cannot be negative.")

    def optional(name: str) -> str | None:
        return values.get(name, "").strip() or None

    return Settings(
        data_dir=data_dir,
        deepseek_api_key=optional("DEEPSEEK_API_KEY"),
        ingest_url=optional("NEWSFEED_INGEST_URL"),
        ingest_secret=optional("NEWSFEED_INGEST_SECRET"),
        daily_budget_usd=budget,
        telegram_bot_token=optional("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=optional("TELEGRAM_CHAT_ID"),
        deadman_url=optional("NEWSFEED_DEADMAN_URL"),
        proxy=optional("NEWS_SCRAPER_PROXY"),
    )
