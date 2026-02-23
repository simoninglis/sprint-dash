"""Tests for JSON API v1 endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.database import init_schema


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Create a temp database and monkeypatch get_db everywhere."""
    import sqlite3

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)

    # Monkeypatch get_db in all modules that import it
    monkeypatch.setattr("app.api_v1.get_db", lambda: conn)
    monkeypatch.setattr("app.api.get_db", lambda: conn)
    monkeypatch.setattr("app.database._connection", conn)
    return conn


@pytest.fixture()
def client(db):
    """Create a TestClient."""
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def owner():
    return "testowner"


@pytest.fixture()
def repo():
    return "testrepo"


def _url(owner, repo, path):
    return f"/{owner}/{repo}/api/v1{path}"


# --- Sprint list ---


class TestListSprints:
    def test_empty(self, client, owner, repo):
        resp = client.get(_url(owner, repo, "/sprints"))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_sprints(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"),
            json={"number": 1, "goal": "first"},
        )
        client.post(
            _url(owner, repo, "/sprints"),
            json={"number": 2, "goal": "second"},
        )
        resp = client.get(_url(owner, repo, "/sprints"))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Ordered by number DESC
        assert data[0]["number"] == 2
        assert data[1]["number"] == 1

    def test_filter_by_status(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, f"/sprints/{1}/start"), json={}
        )
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 2}
        )

        resp = client.get(
            _url(owner, repo, "/sprints"), params={"status": "planned"}
        )
        data = resp.json()
        assert len(data) == 1
        assert data[0]["number"] == 2


# --- Sprint detail ---


class TestGetSprint:
    def test_get_sprint(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"),
            json={"number": 5, "goal": "demo"},
        )
        resp = client.get(_url(owner, repo, "/sprints/5"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["number"] == 5
        assert data["goal"] == "demo"
        assert data["issues"] == []
        assert data["issue_count"] == 0
        assert data["start_snapshot"] is None
        assert data["end_snapshot"] is None

    def test_not_found(self, client, owner, repo):
        resp = client.get(_url(owner, repo, "/sprints/99"))
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_with_issues_and_snapshot(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 3}
        )
        client.post(
            _url(owner, repo, "/sprints/3/issues"),
            json={"issues": [10, 20]},
        )
        client.post(
            _url(owner, repo, "/sprints/3/start"), json={}
        )
        resp = client.get(_url(owner, repo, "/sprints/3"))
        data = resp.json()
        assert data["issues"] == [10, 20]
        assert data["issue_count"] == 2
        assert data["start_snapshot"] is not None
        assert data["start_snapshot"]["total_issues"] == 2


# --- Sprint current ---


class TestCurrentSprint:
    def test_no_current(self, client, owner, repo):
        resp = client.get(_url(owner, repo, "/sprints/current"))
        assert resp.status_code == 404

    def test_current(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 5}
        )
        client.post(
            _url(owner, repo, "/sprints/5/start"), json={}
        )
        resp = client.get(_url(owner, repo, "/sprints/current"))
        assert resp.status_code == 200
        assert resp.json()["number"] == 5


# --- Sprint create ---


class TestCreateSprint:
    def test_create_basic(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints"),
            json={"number": 10},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["number"] == 10
        assert data["status"] == "planned"

    def test_create_with_options(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints"),
            json={
                "number": 11,
                "start_date": "2026-03-01",
                "end_date": "2026-03-15",
                "goal": "test goal",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["start_date"] == "2026-03-01"
        assert data["goal"] == "test goal"

    def test_create_duplicate(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"


# --- Sprint update ---


class TestUpdateSprint:
    def test_update_goal(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.put(
            _url(owner, repo, "/sprints/1"),
            json={"goal": "new goal"},
        )
        assert resp.status_code == 200
        assert resp.json()["goal"] == "new goal"

    def test_update_not_found(self, client, owner, repo):
        resp = client.put(
            _url(owner, repo, "/sprints/99"),
            json={"goal": "x"},
        )
        assert resp.status_code == 404

    def test_update_no_fields(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.put(
            _url(owner, repo, "/sprints/1"), json={}
        )
        # Returns current sprint (no-op update)
        assert resp.status_code == 200
        assert resp.json()["number"] == 1


# --- Sprint start ---


class TestStartSprint:
    def test_start(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/start"), json={}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"

    def test_start_with_date(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/start"),
            json={"start_date": "2026-03-01"},
        )
        assert resp.status_code == 200
        assert resp.json()["start_date"] == "2026-03-01"

    def test_start_not_found(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints/99/start"), json={}
        )
        assert resp.status_code == 404

    def test_start_already_started(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/start"), json={}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/start"), json={}
        )
        assert resp.status_code == 400


# --- Sprint close ---


class TestCloseSprint:
    def test_close(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/start"), json={}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/close"), json={}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_close_with_carry_over(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/start"), json={}
        )
        client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10, 20]},
        )
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 2}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/close"),
            json={"carry_over_to": 2},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["carried_over"]["to_sprint"] == 2
        assert set(data["carried_over"]["issues"]) == {10, 20}

    def test_close_not_in_progress(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/close"), json={}
        )
        assert resp.status_code == 400


# --- Sprint cancel ---


class TestCancelSprint:
    def test_cancel_planned(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/cancel")
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_in_progress(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/start"), json={}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/cancel")
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["snapshot"] == "end"


# --- Issue list ---


class TestListIssues:
    def test_list(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10, 20, 30]},
        )
        resp = client.get(_url(owner, repo, "/sprints/1/issues"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["sprint"] == 1
        assert data["issues"] == [10, 20, 30]
        assert data["count"] == 3

    def test_not_found(self, client, owner, repo):
        resp = client.get(_url(owner, repo, "/sprints/99/issues"))
        assert resp.status_code == 404


# --- Issue add ---


class TestAddIssues:
    def test_add(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10, 20]},
        )
        assert resp.status_code == 200
        assert resp.json()["added"] == [10, 20]

    def test_add_sprint_not_found(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints/99/issues"),
            json={"issues": [10]},
        )
        assert resp.status_code == 404


# --- Issue remove ---


class TestRemoveIssue:
    def test_remove(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10]},
        )
        resp = client.delete(_url(owner, repo, "/sprints/1/issues/10"))
        assert resp.status_code == 204

    def test_remove_not_found(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.delete(_url(owner, repo, "/sprints/1/issues/99"))
        assert resp.status_code == 404


# --- Issue move ---


class TestMoveIssues:
    def test_move(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 2}
        )
        client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10, 20]},
        )
        resp = client.post(
            _url(owner, repo, "/issues/move"),
            json={"issues": [10, 20], "from_sprint": 1, "to_sprint": 2},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["moved"] == [10, 20]
        assert data["from_sprint"] == 1
        assert data["to_sprint"] == 2

    def test_move_not_in_source(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 2}
        )
        resp = client.post(
            _url(owner, repo, "/issues/move"),
            json={"issues": [99], "from_sprint": 1, "to_sprint": 2},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["code"] == "lifecycle_error"
        assert 99 in data["missing"]


# --- Input validation ---


class TestInputValidation:
    def test_negative_sprint_number(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints"), json={"number": -1}
        )
        assert resp.status_code == 422

    def test_invalid_date_format(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints"),
            json={"number": 1, "start_date": "not-a-date"},
        )
        assert resp.status_code == 422

    def test_invalid_date_value(self, client, owner, repo):
        resp = client.post(
            _url(owner, repo, "/sprints"),
            json={"number": 1, "start_date": "2026-02-30"},
        )
        assert resp.status_code == 422

    def test_empty_issues_list(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/issues"), json={"issues": []}
        )
        assert resp.status_code == 422

    def test_negative_issue_number(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        resp = client.post(
            _url(owner, repo, "/sprints/1/issues"), json={"issues": [-5]}
        )
        assert resp.status_code == 422

    def test_move_source_sprint_not_found(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 2}
        )
        resp = client.post(
            _url(owner, repo, "/issues/move"),
            json={"issues": [10], "from_sprint": 99, "to_sprint": 2},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_move_deduplicates(self, client, owner, repo):
        """Duplicate issue IDs in move request are deduplicated."""
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 2}
        )
        client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10]},
        )
        resp = client.post(
            _url(owner, repo, "/issues/move"),
            json={"issues": [10, 10], "from_sprint": 1, "to_sprint": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["moved"] == [10]

    def test_move_destination_sprint_not_found(self, client, owner, repo):
        client.post(
            _url(owner, repo, "/sprints"), json={"number": 1}
        )
        client.post(
            _url(owner, repo, "/sprints/1/issues"),
            json={"issues": [10]},
        )
        resp = client.post(
            _url(owner, repo, "/issues/move"),
            json={"issues": [10], "from_sprint": 1, "to_sprint": 99},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
