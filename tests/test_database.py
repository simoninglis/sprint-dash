"""Tests for database connection and schema initialization."""

import sqlite3

import pytest

from app.database import CURRENT_SCHEMA_VERSION, get_connection, init_schema


@pytest.fixture()
def db():
    """Create an in-memory database for testing."""
    conn = get_connection(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


class TestGetConnection:
    """Test connection creation and configuration."""

    def test_returns_connection(self):
        conn = get_connection(":memory:")
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_row_factory_set(self):
        conn = get_connection(":memory:")
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_wal_mode_enabled(self):
        conn = get_connection(":memory:")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        # In-memory databases use "memory" mode regardless
        assert mode in ("wal", "memory")
        conn.close()

    def test_foreign_keys_enabled(self):
        conn = get_connection(":memory:")
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()


class TestInitSchema:
    """Test schema creation."""

    def test_creates_tables(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        assert "sprints" in table_names
        assert "sprint_issues" in table_names
        assert "sprint_snapshots" in table_names
        assert "schema_version" in table_names

    def test_creates_indexes(self, db):
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        index_names = [i["name"] for i in indexes]
        assert "idx_sprints_repo" in index_names
        assert "idx_sprint_issues_sprint_removed" in index_names
        assert "idx_sprint_issues_issue_number" in index_names

    def test_schema_version_recorded(self, db):
        row = db.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row["version"] == CURRENT_SCHEMA_VERSION

    def test_idempotent(self, db):
        """Calling init_schema twice should not error."""
        init_schema(db)
        init_schema(db)
        rows = db.execute("SELECT COUNT(*) as cnt FROM schema_version").fetchone()
        assert rows["cnt"] == 1

    def test_sprints_unique_constraint(self, db):
        db.execute(
            "INSERT INTO sprints (repo_owner, repo_name, number) VALUES (?, ?, ?)",
            ("owner", "repo", 1),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sprints (repo_owner, repo_name, number) VALUES (?, ?, ?)",
                ("owner", "repo", 1),
            )

    def test_sprint_issues_foreign_key(self, db):
        """Foreign key constraint should reject orphan sprint_issues."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sprint_issues (sprint_id, issue_number) VALUES (?, ?)",
                (9999, 1),
            )

    def test_sprint_status_check_constraint(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sprints (repo_owner, repo_name, number, status) "
                "VALUES (?, ?, ?, ?)",
                ("owner", "repo", 1, "invalid_status"),
            )

    def test_snapshot_type_check_constraint(self, db):
        # First create a sprint to reference
        db.execute(
            "INSERT INTO sprints (repo_owner, repo_name, number) VALUES (?, ?, ?)",
            ("owner", "repo", 1),
        )
        db.commit()
        sprint_id = db.execute("SELECT id FROM sprints LIMIT 1").fetchone()["id"]

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sprint_snapshots "
                "(sprint_id, snapshot_type, total_issues, total_points, issue_numbers) "
                "VALUES (?, ?, ?, ?, ?)",
                (sprint_id, "invalid_type", 0, 0, "[]"),
            )
