"""Tests for SprintDashClient (HTTP client against TestClient transport)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.database import init_schema
from app.http_client import SprintDashClient, SprintDashError


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "test.db")
    conn = _sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = _sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    monkeypatch.setattr("app.api_v1.get_db", lambda: conn)
    monkeypatch.setattr("app.api.get_db", lambda: conn)
    monkeypatch.setattr("app.database._connection", conn)
    return conn


@pytest.fixture()
def test_client(db):
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def sd_client(test_client):
    """SprintDashClient using TestClient as transport."""
    client = SprintDashClient(
        "http://testserver", "testowner", "testrepo", transport=test_client._transport
    )
    yield client
    client.close()


class TestListSprints:
    def test_empty(self, sd_client):
        assert sd_client.list_sprints() == []

    def test_list_and_filter(self, sd_client):
        sd_client.create_sprint(1, goal="first")
        sd_client.create_sprint(2, goal="second")
        sd_client.start_sprint(1)

        all_sprints = sd_client.list_sprints()
        assert len(all_sprints) == 2

        planned = sd_client.list_sprints(status="planned")
        assert len(planned) == 1
        assert planned[0]["number"] == 2


class TestGetSprint:
    def test_found(self, sd_client):
        sd_client.create_sprint(5, goal="demo")
        sprint = sd_client.get_sprint(5)
        assert sprint is not None
        assert sprint["number"] == 5
        assert sprint["goal"] == "demo"
        assert sprint["issues"] == []

    def test_not_found(self, sd_client):
        assert sd_client.get_sprint(99) is None


class TestCreateSprint:
    def test_basic(self, sd_client):
        result = sd_client.create_sprint(10)
        assert result["number"] == 10
        assert result["status"] == "planned"

    def test_with_options(self, sd_client):
        result = sd_client.create_sprint(
            11, start_date="2026-03-01", end_date="2026-03-15", goal="test"
        )
        assert result["start_date"] == "2026-03-01"
        assert result["goal"] == "test"

    def test_duplicate(self, sd_client):
        sd_client.create_sprint(1)
        with pytest.raises(SprintDashError) as exc_info:
            sd_client.create_sprint(1)
        assert exc_info.value.status == 409


class TestUpdateSprint:
    def test_update(self, sd_client):
        sd_client.create_sprint(1)
        result = sd_client.update_sprint(1, goal="updated")
        assert result is not None
        assert result["goal"] == "updated"

    def test_not_found(self, sd_client):
        assert sd_client.update_sprint(99, goal="x") is None


class TestStartSprint:
    def test_start(self, sd_client):
        sd_client.create_sprint(1)
        result = sd_client.start_sprint(1)
        assert result["status"] == "in_progress"

    def test_start_with_date(self, sd_client):
        sd_client.create_sprint(1)
        result = sd_client.start_sprint(1, start_date="2026-03-01")
        assert result["start_date"] == "2026-03-01"

    def test_not_found(self, sd_client):
        with pytest.raises(SprintDashError) as exc_info:
            sd_client.start_sprint(99)
        assert exc_info.value.status == 404


class TestCloseSprint:
    def test_close(self, sd_client):
        sd_client.create_sprint(1)
        sd_client.start_sprint(1)
        result = sd_client.close_sprint(1)
        assert result["status"] == "completed"

    def test_close_with_carry_over(self, sd_client):
        sd_client.create_sprint(1)
        sd_client.start_sprint(1)
        sd_client.add_issue(1, 10)
        sd_client.add_issue(1, 20)
        sd_client.create_sprint(2)

        result = sd_client.close_sprint(1, carry_over_to=2)
        assert result["status"] == "completed"
        assert result["carried_over"]["to_sprint"] == 2


class TestCancelSprint:
    def test_cancel(self, sd_client):
        sd_client.create_sprint(1)
        result = sd_client.cancel_sprint(1)
        assert result["status"] == "cancelled"


class TestCurrentSprint:
    def test_no_current(self, sd_client):
        assert sd_client.get_current_sprint_number() is None

    def test_current(self, sd_client):
        sd_client.create_sprint(5)
        sd_client.start_sprint(5)
        assert sd_client.get_current_sprint_number() == 5


class TestIssueOperations:
    def test_add_and_list(self, sd_client):
        sd_client.create_sprint(1)
        assert sd_client.add_issue(1, 10)
        assert sd_client.add_issue(1, 20)
        issues = sd_client.get_issue_numbers(1)
        assert issues == [10, 20]

    def test_remove(self, sd_client):
        sd_client.create_sprint(1)
        sd_client.add_issue(1, 10)
        assert sd_client.remove_issue(1, 10)
        assert sd_client.get_issue_numbers(1) == []

    def test_remove_not_found(self, sd_client):
        sd_client.create_sprint(1)
        assert not sd_client.remove_issue(1, 99)

    def test_move(self, sd_client):
        sd_client.create_sprint(1)
        sd_client.create_sprint(2)
        sd_client.add_issue(1, 10)
        assert sd_client.move_issue(10, 1, 2)
        assert sd_client.get_issue_numbers(1) == []
        assert sd_client.get_issue_numbers(2) == [10]

    def test_get_issue_numbers_not_found(self, sd_client):
        assert sd_client.get_issue_numbers(99) == []


class TestGetSnapshot:
    def test_snapshot_via_get_sprint(self, sd_client):
        sd_client.create_sprint(1)
        sd_client.add_issue(1, 10)
        sd_client.start_sprint(1)

        snap = sd_client.get_snapshot(1, "start")
        assert snap is not None
        assert snap["total_issues"] == 1

    def test_no_snapshot(self, sd_client):
        sd_client.create_sprint(1)
        assert sd_client.get_snapshot(1, "start") is None

    def test_snapshot_sprint_not_found(self, sd_client):
        assert sd_client.get_snapshot(99, "start") is None


class TestConnectionErrors:
    """Test that transport/network errors are wrapped as SprintDashError."""

    def test_connection_refused(self):
        """Client connecting to unreachable host raises SprintDashError."""
        client = SprintDashClient(
            "http://127.0.0.1:1",  # port 1 — nothing listening
            "owner",
            "repo",
        )
        with pytest.raises(SprintDashError) as exc_info:
            client.list_sprints()
        assert exc_info.value.code == "connection_error"
        assert exc_info.value.status == 0
        client.close()

    def test_connection_error_wraps_httpx(self):
        """Verify the underlying httpx.RequestError is chained."""
        client = SprintDashClient(
            "http://127.0.0.1:1", "o", "r"
        )
        with pytest.raises(SprintDashError) as exc_info:
            client.get_sprint(1)
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, httpx.RequestError)
        client.close()


class TestApiValidation:
    """Test that API validates inputs via Pydantic constraints."""

    def test_negative_sprint_number(self, sd_client):
        """Creating a sprint with negative number is rejected."""
        with pytest.raises(SprintDashError) as exc_info:
            sd_client.create_sprint(-1)
        assert exc_info.value.status == 422

    def test_invalid_date_format(self, sd_client):
        """Creating a sprint with bad date is rejected."""
        with pytest.raises(SprintDashError) as exc_info:
            sd_client.create_sprint(1, start_date="not-a-date")
        assert exc_info.value.status == 422
