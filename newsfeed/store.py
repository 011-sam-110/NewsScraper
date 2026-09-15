"""The SQLite store: connections, numbered migrations, story identity writes and work leases.

Section 7.3 of docs/ARCHITECTURE.md sets the rules this module exists to keep:

- WAL, a 30 second busy timeout and foreign keys on, so two stages can run at once.
- isolation_level=None with an explicit BEGIN IMMEDIATE for every write, so two stages can never
  deadlock while upgrading a read lock to a write lock.
- No transaction is ever held across an HTTP request or a model call. Callers claim work and
  commit, then call out, then write the result in a new transaction.
- Claimed work carries a lease, so a crashed stage's work can be claimed again after 10 minutes.
- Every schema change is a numbered migration recorded in schema_version.

One Store is shared between the scrape threads. Each thread gets its own connection, because a
sqlite3 connection may not be used from another thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .identity import now_utc

BUSY_TIMEOUT_MS = 30_000
LEASE_SECONDS = 600

# Status values, in the only order a story moves through them (section 7.4).
STATUS_SCRAPED = "scraped"
STATUS_EXTRACTED = "extracted"
STATUS_RESOLVED = "resolved"
STATUS_NO_PLACE = "no_place"
STATUS_CLUSTERED = "clustered"

# Migration 1 is the M1 store: what the scrape stage writes and the health stage reads. The AI
# layer's tables (extractions, places, clusters, llm_calls, publishes, alerts) arrive as their own
# numbered migrations with the milestone that first writes them, so no column is guessed early.
MIGRATION_1 = """
CREATE TABLE stories (
    story_id        TEXT PRIMARY KEY,
    outlet          TEXT NOT NULL,
    primary_alias   TEXT NOT NULL,
    url             TEXT,
    source_url      TEXT,
    headline        TEXT,
    description     TEXT,
    published       TEXT,
    published_raw   TEXT,
    updated         TEXT,
    updated_raw     TEXT,
    authors         TEXT NOT NULL DEFAULT '[]',
    word_count      INTEGER,
    thumbnail       TEXT,
    categories      TEXT NOT NULL DEFAULT '{}',
    format_flags    TEXT NOT NULL DEFAULT '[]',
    text_hash       TEXT,
    text_fetched_at TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'scraped',
    lease_owner     TEXT,
    lease_until     TEXT
);
CREATE INDEX stories_status ON stories(status, published DESC);
CREATE INDEX stories_outlet_seen ON stories(outlet, last_seen_at DESC);
CREATE INDEX stories_url ON stories(url);

CREATE TABLE story_aliases (
    outlet        TEXT NOT NULL,
    alias         TEXT NOT NULL,
    story_id      TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (outlet, alias)
);
CREATE INDEX story_aliases_story ON story_aliases(story_id);

CREATE TABLE story_sections (
    story_id      TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
    section       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (story_id, section)
);
CREATE INDEX story_sections_section ON story_sections(section);

CREATE TABLE story_texts (
    story_id   TEXT PRIMARY KEY REFERENCES stories(story_id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    text_hash  TEXT NOT NULL,
    word_count INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE outlet_health (
    outlet               TEXT PRIMARY KEY,
    last_run_at          TEXT,
    last_success_at      TEXT,
    last_new_story_at    TEXT,
    last_error           TEXT,
    last_error_at        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE TABLE scrape_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    sections    TEXT NOT NULL DEFAULT '[]',
    rows_seen   INTEGER NOT NULL DEFAULT 0,
    new_stories INTEGER NOT NULL DEFAULT 0,
    errors      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX scrape_runs_finished ON scrape_runs(finished_at DESC);
"""

# Migration 2 is the extract stage (M5): what the model said about each story, and what every model
# call cost. extractions is keyed by (story_id, config_hash) so that running again with the same
# configuration costs nothing, and a configuration change is a new row rather than a lost one.
MIGRATION_2 = """
CREATE TABLE extractions (
    story_id          TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
    config_hash       TEXT NOT NULL,
    text_hash         TEXT,
    accepted          INTEGER NOT NULL DEFAULT 0,
    failure_reason    TEXT,
    is_physical_event INTEGER,
    not_event_reason  TEXT,
    category          TEXT,
    event_date        TEXT,
    place_name        TEXT,
    place_within      TEXT,
    place_country     TEXT,
    place_kind        TEXT,
    place_quote       TEXT,
    other_places      TEXT NOT NULL DEFAULT '[]',
    key_entities      TEXT NOT NULL DEFAULT '[]',
    cluster_hint      TEXT,
    raw               TEXT,
    model             TEXT,
    schema_retried    INTEGER NOT NULL DEFAULT 0,
    outlet_tags       TEXT NOT NULL DEFAULT '[]',
    checks            TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (story_id, config_hash)
);
CREATE INDEX extractions_config ON extractions(config_hash, accepted);
CREATE INDEX extractions_category ON extractions(category);

CREATE TABLE llm_calls (
    call_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stage              TEXT NOT NULL,
    story_id           TEXT,
    model              TEXT NOT NULL,
    config_hash        TEXT,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    latency_ms         INTEGER,
    attempts           INTEGER,
    outcome            TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX llm_calls_created ON llm_calls(created_at);
CREATE INDEX llm_calls_stage ON llm_calls(stage, created_at);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, MIGRATION_1), (2, MIGRATION_2))
SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection with the pragmas section 7.3 requires. Autocommit; writers use BEGIN IMMEDIATE."""
    connection = sqlite3.connect(
        str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def statements(script: str) -> list[str]:
    """One SQL statement per entry.

    sqlite3.executescript commits any open transaction before it runs, which would break the
    migration's BEGIN IMMEDIATE, so migrations are applied statement by statement.
    sqlite3.complete_statement understands a trigger's BEGIN ... END, so a later migration may
    contain one.
    """
    found: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            found.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise RuntimeError(f"A migration ends with an incomplete statement: {buffer.strip()[:80]!r}")
    return found


def migrate(connection: sqlite3.Connection) -> int:
    """Apply every migration this build knows and return the resulting version.

    A database written by a newer build is refused rather than used, because a missing column would
    otherwise surface as a confusing failure in the middle of a run.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        current = row["version"] or 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"The store is at schema version {current} and this build knows {SCHEMA_VERSION}. "
                "Update the checkout before running a stage."
            )
        for version, script in MIGRATIONS:
            if version > current:
                for statement in statements(script):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, now_utc())
                )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


class Store:
    """One SQLite database, shared between threads. Each thread gets its own connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.owner = f"{uuid.uuid4().hex[:8]}"

    @property
    def connection(self) -> sqlite3.Connection:
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = connect(self.path)
            self._local.connection = existing
            with self._lock:
                self._connections.append(existing)
        return existing

    def migrate(self) -> int:
        return migrate(self.connection)

    def close(self) -> None:
        """Close every connection this store opened, from whichever thread is shutting down."""
        with self._lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def __enter__(self) -> Store:
        self.migrate()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, opened with BEGIN IMMEDIATE and never held across a network call."""
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, parameters).fetchall()

    def one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, parameters).fetchone()

    def scalar(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        row = self.one(sql, parameters)
        return None if row is None else row[0]

    # Story identity ------------------------------------------------------------------

    def find_story(self, outlet: str, aliases: Sequence[str]) -> str | None:
        """The story any of these aliases already points to, or None.

        When aliases point at more than one story, the earliest one wins. v1 never merges stories:
        repointing an alias could orphan work a later stage has already done against the other id,
        and a duplicate story costs far less than a lost extraction.
        """
        if not aliases:
            return None
        placeholders = ",".join("?" * len(aliases))
        rows = self.connection.execute(
            f"""SELECT a.story_id AS story_id, s.first_seen_at AS first_seen_at
                  FROM story_aliases a JOIN stories s ON s.story_id = a.story_id
                 WHERE a.outlet = ? AND a.alias IN ({placeholders})
                 ORDER BY s.first_seen_at, a.story_id LIMIT 1""",
            (outlet, *aliases),
        ).fetchall()
        return rows[0]["story_id"] if rows else None

    def story(self, story_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM stories WHERE story_id = ?", (story_id,))

    def has_section(self, story_id: str, section: str) -> bool:
        return (
            self.one(
                "SELECT 1 FROM story_sections WHERE story_id = ? AND section = ?", (story_id, section)
            )
            is not None
        )

    # Leases --------------------------------------------------------------------------

    def claim_stories(
        self, status: str, limit: int, owner: str | None = None, lease_seconds: int = LEASE_SECONDS
    ) -> list[str]:
        """Claim up to limit stories in this status, newest first, and return their ids.

        The claim commits before the caller makes any model or HTTP call, so no transaction is open
        while the network is slow. A crashed stage's claim expires after lease_seconds.
        """
        held_by = owner or self.owner
        until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = now_utc()
        with self.write() as connection:
            rows = connection.execute(
                """SELECT story_id FROM stories
                    WHERE status = ?
                      AND (lease_until IS NULL OR lease_until < ?)
                    ORDER BY COALESCE(published, first_seen_at) DESC
                    LIMIT ?""",
                (status, now, limit),
            ).fetchall()
            claimed = [row["story_id"] for row in rows]
            if claimed:
                placeholders = ",".join("?" * len(claimed))
                connection.execute(
                    f"UPDATE stories SET lease_owner = ?, lease_until = ? WHERE story_id IN ({placeholders})",
                    (held_by, until, *claimed),
                )
        return claimed

    def release_stories(self, story_ids: Sequence[str], status: str | None = None) -> None:
        """Drop the lease on these stories, and optionally move them to their next status."""
        if not story_ids:
            return
        placeholders = ",".join("?" * len(story_ids))
        with self.write() as connection:
            if status is None:
                connection.execute(
                    f"UPDATE stories SET lease_owner = NULL, lease_until = NULL "
                    f"WHERE story_id IN ({placeholders})",
                    tuple(story_ids),
                )
            else:
                connection.execute(
                    f"UPDATE stories SET lease_owner = NULL, lease_until = NULL, status = ? "
                    f"WHERE story_id IN ({placeholders})",
                    (status, *story_ids),
                )

    # Outlet health -------------------------------------------------------------------

    def start_outlet_run(self, outlet: str) -> None:
        now = now_utc()
        with self.write() as connection:
            connection.execute(
                """INSERT INTO outlet_health (outlet, last_run_at, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(outlet) DO UPDATE SET last_run_at = excluded.last_run_at,
                                                     updated_at = excluded.updated_at""",
                (outlet, now, now),
            )

    def finish_outlet_run(self, outlet: str, error: str | None, new_stories: int) -> None:
        """Record what one outlet's run did. A failure counts up; a success clears the count."""
        now = now_utc()
        with self.write() as connection:
            connection.execute(
                "INSERT INTO outlet_health (outlet, updated_at) VALUES (?, ?) ON CONFLICT(outlet) DO NOTHING",
                (outlet, now),
            )
            if error:
                connection.execute(
                    """UPDATE outlet_health
                          SET last_error = ?, last_error_at = ?,
                              consecutive_failures = consecutive_failures + 1, updated_at = ?
                        WHERE outlet = ?""",
                    (error, now, now, outlet),
                )
            else:
                connection.execute(
                    """UPDATE outlet_health
                          SET last_success_at = ?, consecutive_failures = 0, updated_at = ?
                        WHERE outlet = ?""",
                    (now, now, outlet),
                )
            if new_stories > 0:
                connection.execute(
                    "UPDATE outlet_health SET last_new_story_at = ?, updated_at = ? WHERE outlet = ?",
                    (now, now, outlet),
                )

    # Model spend ---------------------------------------------------------------------

    def record_call(
        self,
        stage: str,
        model: str,
        outcome: str,
        usage: Any = None,
        cost_usd: float = 0.0,
        story_id: str | None = None,
        config_hash: str | None = None,
        latency_ms: int | None = None,
        attempts: int | None = None,
    ) -> None:
        """One row per model call, including the ones that failed.

        A failed call still costs time and can still cost money, and the health stage counts them,
        so nothing is recorded only on success.
        """
        with self.write() as connection:
            connection.execute(
                """INSERT INTO llm_calls (stage, story_id, model, config_hash, prompt_tokens,
                                          completion_tokens, cache_hit_tokens, cache_miss_tokens,
                                          cost_usd, latency_ms, attempts, outcome, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stage, story_id, model, config_hash,
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                    getattr(usage, "cache_hit_tokens", 0),
                    getattr(usage, "cache_miss_tokens", 0),
                    float(cost_usd), latency_ms, attempts, outcome, now_utc(),
                ),
            )

    def spend_today(self, day: str | None = None) -> float:
        """What the model has cost so far this UTC day, which is what the budget caps."""
        today = day or now_utc()[:10]
        return float(
            self.scalar(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE substr(created_at, 1, 10) = ?",
                (today,),
            )
            or 0.0
        )

    def served_model_disagreements(self, config_hash: str) -> list[str]:
        """Model strings seen under one config hash. More than one means a point release slipped in."""
        return [
            row["model"]
            for row in self.query(
                "SELECT DISTINCT model FROM extractions WHERE config_hash = ? AND model IS NOT NULL "
                "ORDER BY model",
                (config_hash,),
            )
        ]

    # Runs ----------------------------------------------------------------------------

    def start_run(self, sections: Sequence[str]) -> int:
        with self.write() as connection:
            cursor = connection.execute(
                "INSERT INTO scrape_runs (started_at, sections) VALUES (?, ?)",
                (now_utc(), json.dumps(list(sections))),
            )
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, rows_seen: int, new_stories: int, errors: dict[str, str]) -> None:
        with self.write() as connection:
            connection.execute(
                """UPDATE scrape_runs SET finished_at = ?, rows_seen = ?, new_stories = ?, errors = ?
                    WHERE run_id = ?""",
                (now_utc(), rows_seen, new_stories, json.dumps(errors, ensure_ascii=False), run_id),
            )

    def data_as_of(self) -> str | None:
        """The newest completed scrape run, which is what a snapshot's dataAsOf reports."""
        return self.scalar(
            "SELECT finished_at FROM scrape_runs WHERE finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1"
        )
