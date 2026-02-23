"""Tests for sd-cli (app.cli)."""

import json

import pytest

from app.cli import main


@pytest.fixture()
def db_path(tmp_path):
    """Return a temp database path."""
    return str(tmp_path / "test.db")


@pytest.fixture()
def cli(db_path):
    """Return a helper that calls main() with common flags."""

    def run(*args: str, json_mode: bool = False) -> None:
        base = ["--db", db_path, "--owner", "testowner", "--repo", "testrepo"]
        if json_mode:
            base.append("--json")
        main([*base, *args])

    return run


# --- Sprint list ---


class TestSprintList:
    def test_empty(self, cli, capsys):
        cli("sprint", "list")
        assert "No sprints found" in capsys.readouterr().out

    def test_lists_sprints(self, cli, capsys):
        cli("sprint", "create", "1", "--goal", "first")
        cli("sprint", "create", "2", "--goal", "second")
        capsys.readouterr()  # clear create output

        cli("sprint", "list")
        out = capsys.readouterr().out
        assert "1" in out
        assert "2" in out
        assert "first" in out
        assert "second" in out

    def test_filter_by_status(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        cli("sprint", "create", "2")
        capsys.readouterr()

        cli("sprint", "list", "--status", "planned")
        out = capsys.readouterr().out
        assert "2" in out
        assert "in_progress" not in out

    def test_json_output(self, cli, capsys):
        cli("sprint", "create", "1", "--goal", "test")
        capsys.readouterr()

        cli("sprint", "list", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["number"] == 1
        assert data[0]["goal"] == "test"


# --- Sprint show ---


class TestSprintShow:
    def test_show_sprint(self, cli, capsys):
        cli("sprint", "create", "5", "--goal", "demo")
        cli("issue", "add", "5", "10", "20")
        capsys.readouterr()

        cli("sprint", "show", "5")
        out = capsys.readouterr().out
        assert "Sprint 5" in out
        assert "demo" in out
        assert "#10" in out
        assert "#20" in out

    def test_show_not_found(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "show", "99")

    def test_show_json(self, cli, capsys):
        cli("sprint", "create", "3")
        cli("issue", "add", "3", "7")
        capsys.readouterr()

        cli("sprint", "show", "3", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["number"] == 3
        assert data["issues"] == [7]
        assert data["issue_count"] == 1


# --- Sprint create ---


class TestSprintCreate:
    def test_create_basic(self, cli, capsys):
        cli("sprint", "create", "10")
        out = capsys.readouterr().out
        assert "number=10" in out
        assert "status=planned" in out

    def test_create_with_options(self, cli, capsys):
        cli(
            "sprint",
            "create",
            "11",
            "--start",
            "2026-03-01",
            "--end",
            "2026-03-15",
            "--goal",
            "test goal",
        )
        out = capsys.readouterr().out
        assert "2026-03-01" in out
        assert "2026-03-15" in out
        assert "test goal" in out

    def test_create_duplicate(self, cli):
        cli("sprint", "create", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "create", "1")

    def test_create_json(self, cli, capsys):
        cli("sprint", "create", "5", "--goal", "hi", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["number"] == 5
        assert data["goal"] == "hi"

    def test_create_rejects_bad_date_format(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "create", "7", "--start", "2026-3-9")

    def test_create_rejects_invalid_date(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "create", "7", "--start", "2026-13-01")


# --- Sprint update ---


class TestSprintUpdate:
    def test_update_goal(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("sprint", "update", "1", "--goal", "new goal")
        out = capsys.readouterr().out
        assert "new goal" in out

    def test_update_not_found(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "update", "99", "--goal", "x")

    def test_update_no_fields(self, cli):
        cli("sprint", "create", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "update", "1")


# --- Sprint start ---


class TestSprintStart:
    def test_start_sprint(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("sprint", "start", "1")
        out = capsys.readouterr().out
        assert "started" in out

    def test_start_with_date(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("sprint", "start", "1", "--start", "2026-03-01")
        out = capsys.readouterr().out
        assert "2026-03-01" in out

    def test_start_already_started(self, cli):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "start", "1")

    def test_start_captures_snapshot(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("issue", "add", "1", "10", "20")
        capsys.readouterr()

        cli("sprint", "start", "1", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["snapshot"] == "start"
        # Snapshot captures issues present at start time
        assert data["issues"] == [10, 20]

    def test_start_not_found(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "start", "99")


# --- Sprint close ---


class TestSprintClose:
    def test_close_sprint(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        capsys.readouterr()

        cli("sprint", "close", "1")
        out = capsys.readouterr().out
        assert "closed" in out

    def test_close_with_carry_over(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        cli("issue", "add", "1", "10", "20")
        cli("sprint", "create", "2")
        capsys.readouterr()

        cli("sprint", "close", "1", "--carry-over-to", "2")
        out = capsys.readouterr().out
        assert "Carried over 2 issues" in out

    def test_close_carry_over_json(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        cli("issue", "add", "1", "5")
        cli("sprint", "create", "2")
        capsys.readouterr()

        cli("sprint", "close", "1", "--carry-over-to", "2", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "completed"
        assert data["carried_over"]["to_sprint"] == 2
        assert data["carried_over"]["issues"] == [5]

    def test_close_already_completed(self, cli):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        cli("sprint", "close", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "close", "1")

    def test_close_carry_over_target_not_found(self, cli, db_path):
        """Failed carry-over target should NOT close the sprint."""
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "close", "1", "--carry-over-to", "99")

        # Sprint should still be in_progress (not closed)
        from app.database import get_connection, init_schema
        from app.sprint_store import SprintStore

        conn = get_connection(db_path)
        init_schema(conn)
        store = SprintStore(conn, "testowner", "testrepo")
        sprint = store.get_sprint(1)
        conn.close()
        assert sprint is not None
        assert sprint["status"] == "in_progress"

    def test_close_self_carry_over_rejected(self, cli):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "close", "1", "--carry-over-to", "1")


# --- Sprint cancel ---


class TestSprintCancel:
    def test_cancel_planned(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("sprint", "cancel", "1")
        out = capsys.readouterr().out
        assert "cancelled" in out

    def test_cancel_in_progress(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        cli("issue", "add", "1", "10")
        capsys.readouterr()

        cli("sprint", "cancel", "1")
        out = capsys.readouterr().out
        assert "cancelled" in out
        assert "snapshot" in out.lower()

    def test_cancel_json(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        capsys.readouterr()

        cli("sprint", "cancel", "1", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "cancelled"
        assert data["snapshot"] == "end"

    def test_cancel_already_completed(self, cli):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        cli("sprint", "close", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "cancel", "1")


# --- Sprint current ---


class TestSprintCurrent:
    def test_current_sprint(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "start", "1")
        capsys.readouterr()

        cli("sprint", "current")
        assert "1" in capsys.readouterr().out

    def test_no_current_sprint(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("sprint", "current")

    def test_current_json(self, cli, capsys):
        cli("sprint", "create", "5")
        cli("sprint", "start", "5")
        capsys.readouterr()

        cli("sprint", "current", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["current_sprint"] == 5


# --- Issue list ---


class TestIssueList:
    def test_list_issues(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("issue", "add", "1", "10", "20", "30")
        capsys.readouterr()

        cli("issue", "list", "1")
        out = capsys.readouterr().out
        assert "10 20 30" in out

    def test_list_empty(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("issue", "list", "1")
        assert "no issues" in capsys.readouterr().out

    def test_list_not_found(self, cli):
        with pytest.raises(SystemExit, match="1"):
            cli("issue", "list", "99")

    def test_list_json(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("issue", "add", "1", "5", "10")
        capsys.readouterr()

        cli("issue", "list", "1", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["sprint"] == 1
        assert data["issues"] == [5, 10]
        assert data["count"] == 2


# --- Issue add ---


class TestIssueAdd:
    def test_add_issues(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("issue", "add", "1", "10", "20")
        out = capsys.readouterr().out
        assert "#10" in out
        assert "#20" in out

    def test_add_json(self, cli, capsys):
        cli("sprint", "create", "1")
        capsys.readouterr()

        cli("issue", "add", "1", "5", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["added"] == [5]
        assert data["failed"] == []


# --- Issue remove ---


class TestIssueRemove:
    def test_remove_issues(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("issue", "add", "1", "10", "20")
        capsys.readouterr()

        cli("issue", "remove", "1", "10")
        out = capsys.readouterr().out
        assert "#10" in out

    def test_remove_not_found(self, cli):
        cli("sprint", "create", "1")
        with pytest.raises(SystemExit, match="1"):
            cli("issue", "remove", "1", "99")

    def test_remove_json(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("issue", "add", "1", "10")
        capsys.readouterr()

        cli("issue", "remove", "1", "10", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["removed"] == [10]


# --- Issue move ---


class TestIssueMove:
    def test_move_issues(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "create", "2")
        cli("issue", "add", "1", "10", "20")
        capsys.readouterr()

        cli("issue", "move", "1", "10", "20", "--to", "2")
        out = capsys.readouterr().out
        assert "#10" in out
        assert "#20" in out

    def test_move_json(self, cli, capsys):
        cli("sprint", "create", "1")
        cli("sprint", "create", "2")
        cli("issue", "add", "1", "10")
        capsys.readouterr()

        cli("issue", "move", "1", "10", "--to", "2", json_mode=True)
        data = json.loads(capsys.readouterr().out)
        assert data["moved"] == [10]
        assert data["from_sprint"] == 1
        assert data["to_sprint"] == 2

    def test_move_not_in_source(self, cli):
        cli("sprint", "create", "1")
        cli("sprint", "create", "2")
        with pytest.raises(SystemExit, match="1"):
            cli("issue", "move", "1", "99", "--to", "2")


# --- Batch ---


class TestBatch:
    def test_batch_create_and_add(self, db_path, capsys, monkeypatch):
        import io

        ops = json.dumps(
            [
                {
                    "command": "sprint create",
                    "args": {"number": 10, "goal": "batch test"},
                },
                {"command": "issue add", "args": {"sprint": 10, "issues": [1, 2, 3]}},
            ]
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(ops))
        main(["--db", db_path, "--owner", "o", "--repo", "r", "--json", "batch"])
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 2
        assert len(data["results"]) == 2
        assert len(data["errors"]) == 0
        assert data["results"][0]["ok"] is True
        assert data["results"][1]["result"]["added"] == [1, 2, 3]

    def test_batch_with_error(self, db_path, capsys, monkeypatch):
        import io

        ops = json.dumps(
            [
                {"command": "sprint create", "args": {"number": 10}},
                {"command": "unknown cmd", "args": {}},
            ]
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(ops))
        with pytest.raises(SystemExit, match="1"):
            main(["--db", db_path, "--owner", "o", "--repo", "r", "--json", "batch"])
        data = json.loads(capsys.readouterr().out)
        assert len(data["results"]) == 1
        assert len(data["errors"]) == 1
        assert data["errors"][0]["error"] == "Unknown command: unknown cmd"

    def test_batch_malformed_ops(self, db_path, capsys, monkeypatch):
        import io

        ops = json.dumps(
            [
                {"command": "sprint create", "args": {"number": 10}},
                "not a dict",
                42,
            ]
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(ops))
        with pytest.raises(SystemExit, match="1"):
            main(["--db", db_path, "--owner", "o", "--repo", "r", "--json", "batch"])
        data = json.loads(capsys.readouterr().out)
        assert len(data["results"]) == 1
        assert len(data["errors"]) == 2
        assert "Expected dict" in data["errors"][0]["error"]
        assert "Expected dict" in data["errors"][1]["error"]

    def test_batch_malformed_args(self, db_path, capsys, monkeypatch):
        import io

        ops = json.dumps(
            [
                {"command": "sprint create", "args": "not-a-dict"},
            ]
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(ops))
        with pytest.raises(SystemExit, match="1"):
            main(["--db", db_path, "--owner", "o", "--repo", "r", "--json", "batch"])
        data = json.loads(capsys.readouterr().out)
        assert len(data["errors"]) == 1
        assert "Expected args" in data["errors"][0]["error"]

    def test_batch_start_and_close(self, db_path, capsys, monkeypatch):
        import io

        ops = json.dumps(
            [
                {"command": "sprint create", "args": {"number": 5}},
                {"command": "issue add", "args": {"sprint": 5, "issues": [10, 20]}},
                {
                    "command": "sprint start",
                    "args": {"number": 5, "start": "2026-03-01"},
                },
                {"command": "sprint close", "args": {"number": 5}},
            ]
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(ops))
        main(["--db", db_path, "--owner", "o", "--repo", "r", "--json", "batch"])
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 4
        assert len(data["errors"]) == 0
        # Verify start result
        assert data["results"][2]["result"]["status"] == "in_progress"
        # Verify close result
        assert data["results"][3]["result"]["status"] == "completed"


# --- Missing args ---


class TestMissingArgs:
    def test_no_command(self):
        with pytest.raises(SystemExit):
            main(["--db", ":memory:", "--owner", "x", "--repo", "y"])

    def test_missing_owner_repo(self, db_path, monkeypatch):
        monkeypatch.delenv("GITEA_OWNER", raising=False)
        monkeypatch.delenv("GITEA_REPO", raising=False)
        with pytest.raises(SystemExit, match="1"):
            main(["--db", db_path, "sprint", "list"])
