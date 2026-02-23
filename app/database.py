"""SQLite database connection manager for sprint data."""

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Schema version — bump when adding migrations
CURRENT_SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'in_progress', 'completed', 'cancelled')),
    start_date TEXT,
    end_date TEXT,
    goal TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repo_owner, repo_name, number)
);

CREATE TABLE IF NOT EXISTS sprint_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id INTEGER NOT NULL REFERENCES sprints(id) ON DELETE CASCADE,
    issue_number INTEGER NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('migration', 'manual', 'rollover')),
    UNIQUE (sprint_id, issue_number, added_at)
);

CREATE TABLE IF NOT EXISTS sprint_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id INTEGER NOT NULL REFERENCES sprints(id) ON DELETE CASCADE,
    snapshot_type TEXT NOT NULL CHECK (snapshot_type IN ('start', 'end')),
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    total_issues INTEGER NOT NULL,
    total_points INTEGER NOT NULL,
    issue_numbers TEXT NOT NULL,
    UNIQUE (sprint_id, snapshot_type)
);

CREATE INDEX IF NOT EXISTS idx_sprints_repo
    ON sprints(repo_owner, repo_name);

CREATE INDEX IF NOT EXISTS idx_sprint_issues_sprint_removed
    ON sprint_issues(sprint_id, removed_at);

CREATE INDEX IF NOT EXISTS idx_sprint_issues_issue_number
    ON sprint_issues(issue_number);

-- Enforce at most one in_progress sprint per repo (DB-level safety net)
CREATE UNIQUE INDEX IF NOT EXISTS idx_sprints_single_active
    ON sprints(repo_owner, repo_name)
    WHERE status = 'in_progress';
"""


def get_db_path() -> str:
    """Get database path from environment or default."""
    return os.getenv("SPRINT_DASH_DB", "/data/sprint-dash.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Create a new SQLite connection with proper settings.

    Args:
        db_path: Database file path. None uses env/default.
                 ":memory:" for in-memory database (testing).

    Returns:
        Configured sqlite3.Connection with WAL mode and foreign keys.
    """
    path = db_path if db_path is not None else get_db_path()

    # Ensure parent directory exists for file-based databases
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't exist.

    Idempotent — safe to call on every startup.
    """
    conn.executescript(SCHEMA_SQL)

    # Record schema version if not already present
    existing = conn.execute(
        "SELECT version FROM schema_version WHERE version = ?",
        (CURRENT_SCHEMA_VERSION,),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION,),
        )
        conn.commit()

    logger.info("Database schema initialized (version %d)", CURRENT_SCHEMA_VERSION)


# Module-level connection singleton
_connection: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Get the module-level database connection (created on first call).

    Initializes schema on first connection.
    """
    global _connection
    if _connection is None:
        _connection = get_connection()
        init_schema(_connection)
    return _connection


def close_db() -> None:
    """Close the module-level database connection."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
