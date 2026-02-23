"""Tests for SprintStore CRUD operations."""

import sqlite3

import pytest

from app.database import get_connection, init_schema
from app.sprint_store import SprintStore


@pytest.fixture()
def db():
    """Create an in-memory database with schema."""
    conn = get_connection(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    """Create a SprintStore scoped to test repo."""
    return SprintStore(db, "singlis", "deckengine")


def _create_sprint_in_status(store: SprintStore, number: int, status: str) -> dict:
    """Test helper: create a sprint and set it to a given status via SQL.

    Bypasses lifecycle validation — for test setup only.
    """
    result = store.create_sprint(number)
    if status != "planned":
        store.conn.execute(
            "UPDATE sprints SET status = ? WHERE repo_owner = ? AND repo_name = ? AND number = ?",
            (status, store.repo_owner, store.repo_name, number),
        )
        store.conn.commit()
        result = store.get_sprint(number)  # type: ignore[assignment]
    return result  # type: ignore[return-value]


class TestCreateSprint:
    def test_create_basic(self, store):
        result = store.create_sprint(47)
        assert result["number"] == 47
        assert result["status"] == "planned"
        assert result["repo_owner"] == "singlis"
        assert result["repo_name"] == "deckengine"

    def test_create_with_all_fields(self, store):
        result = store.create_sprint(
            48,
            start_date="2026-03-09",
            end_date="2026-03-22",
            goal="Ship auth module",
        )
        assert result["status"] == "planned"
        assert result["start_date"] == "2026-03-09"
        assert result["end_date"] == "2026-03-22"
        assert result["goal"] == "Ship auth module"

    def test_create_rejects_workflow_status(self, store):
        with pytest.raises(ValueError, match="Cannot create sprint"):
            store.create_sprint(48, status="in_progress")
        with pytest.raises(ValueError, match="Cannot create sprint"):
            store.create_sprint(48, status="completed")

    def test_create_duplicate_raises(self, store):
        store.create_sprint(47)
        with pytest.raises(sqlite3.IntegrityError):
            store.create_sprint(47)

    def test_repo_scoping(self, db):
        """Different repos can have same sprint number."""
        store_a = SprintStore(db, "singlis", "repo-a")
        store_b = SprintStore(db, "singlis", "repo-b")
        store_a.create_sprint(1)
        store_b.create_sprint(1)
        assert store_a.get_sprint(1) is not None
        assert store_b.get_sprint(1) is not None


class TestGetSprint:
    def test_get_existing(self, store):
        store.create_sprint(47)
        result = store.get_sprint(47)
        assert result is not None
        assert result["number"] == 47

    def test_get_nonexistent(self, store):
        assert store.get_sprint(999) is None

    def test_get_wrong_repo(self, db):
        store_a = SprintStore(db, "singlis", "repo-a")
        store_b = SprintStore(db, "singlis", "repo-b")
        store_a.create_sprint(1)
        assert store_b.get_sprint(1) is None


class TestUpdateSprint:
    def test_update_rejects_all_status_changes(self, store):
        """No status changes allowed via update_sprint — must use workflow methods."""
        store.create_sprint(47)
        for status in ("planned", "in_progress", "completed", "cancelled"):
            with pytest.raises(ValueError, match="Cannot change status"):
                store.update_sprint(47, status=status)

    def test_update_rejects_backward_transition(self, store):
        """Completed/cancelled sprints cannot be reverted to planned."""
        _create_sprint_in_status(store, 47, "completed")
        with pytest.raises(ValueError, match="Cannot update completed sprint"):
            store.update_sprint(47, status="planned")

        _create_sprint_in_status(store, 48, "cancelled")
        with pytest.raises(ValueError, match="Cannot update cancelled sprint"):
            store.update_sprint(48, status="planned")

    def test_update_multiple_fields(self, store):
        store.create_sprint(47)
        result = store.update_sprint(
            47,
            start_date="2026-03-09",
            goal="New goal",
        )
        assert result["start_date"] == "2026-03-09"
        assert result["goal"] == "New goal"

    def test_update_completed_sprint_rejects_metadata_change(self, store):
        """Completed sprints are fully frozen — no metadata changes."""
        _create_sprint_in_status(store, 47, "completed")
        with pytest.raises(ValueError, match="Cannot update completed sprint"):
            store.update_sprint(47, goal="new goal")

    def test_update_cancelled_sprint_rejects_metadata_change(self, store):
        """Cancelled sprints are fully frozen — no metadata changes."""
        _create_sprint_in_status(store, 47, "cancelled")
        with pytest.raises(ValueError, match="Cannot update cancelled sprint"):
            store.update_sprint(47, start_date="2026-04-01")

    def test_update_nonexistent(self, store):
        assert store.update_sprint(999, goal="x") is None

    def test_update_works_for_non_status_fields(self, store):
        """update_sprint still works for dates and goal."""
        store.create_sprint(47)
        result = store.update_sprint(47, goal="new goal", start_date="2026-03-01")
        assert result["goal"] == "new goal"
        assert result["start_date"] == "2026-03-01"

    def test_update_ignores_unknown_fields(self, store):
        store.create_sprint(47)
        result = store.update_sprint(47, bogus="value")
        assert result is not None
        assert result["number"] == 47

    def test_update_sets_updated_at(self, store):
        store.create_sprint(47)
        updated = store.update_sprint(47, goal="changed")
        # updated_at should be set (not None/empty)
        assert updated["updated_at"] is not None
        assert len(updated["updated_at"]) > 0


class TestListSprints:
    def test_list_all(self, store):
        _create_sprint_in_status(store, 45, "completed")
        _create_sprint_in_status(store, 46, "completed")
        _create_sprint_in_status(store, 47, "in_progress")
        result = store.list_sprints()
        assert len(result) == 3
        # Ordered by number descending
        assert result[0]["number"] == 47
        assert result[1]["number"] == 46
        assert result[2]["number"] == 45

    def test_list_by_status(self, store):
        _create_sprint_in_status(store, 45, "completed")
        _create_sprint_in_status(store, 46, "completed")
        _create_sprint_in_status(store, 47, "in_progress")
        result = store.list_sprints(status="completed")
        assert len(result) == 2
        assert all(r["status"] == "completed" for r in result)

    def test_list_empty(self, store):
        assert store.list_sprints() == []


class TestGetCurrentSprintNumber:
    def test_returns_in_progress_sprint(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        assert store.get_current_sprint_number() == 47

    def test_returns_none_when_no_in_progress(self, store):
        _create_sprint_in_status(store, 45, "completed")
        store.create_sprint(46)
        assert store.get_current_sprint_number() is None

    def test_only_one_in_progress_allowed_at_db_level(self, store):
        """The partial unique index prevents multiple in_progress sprints."""
        _create_sprint_in_status(store, 46, "in_progress")
        store.create_sprint(47)
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "UPDATE sprints SET status = 'in_progress' "
                "WHERE repo_owner = ? AND repo_name = ? AND number = ?",
                (store.repo_owner, store.repo_name, 47),
            )


class TestAddIssue:
    def test_add_issue(self, store):
        store.create_sprint(47)
        assert store.add_issue(47, 123) is True
        assert 123 in store.get_issue_numbers(47)

    def test_add_to_nonexistent_sprint(self, store):
        assert store.add_issue(999, 123) is False

    def test_add_with_source(self, store):
        store.create_sprint(47)
        store.add_issue(47, 123, source="migration")
        # Verify source is stored
        sprint = store.get_sprint(47)
        row = store.conn.execute(
            "SELECT source FROM sprint_issues WHERE sprint_id = ? AND issue_number = ?",
            (sprint["id"], 123),
        ).fetchone()
        assert row["source"] == "migration"

    def test_add_multiple_issues(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        store.add_issue(47, 300)
        numbers = store.get_issue_numbers(47)
        assert numbers == [100, 200, 300]


class TestRemoveIssue:
    def test_remove_issue(self, store):
        store.create_sprint(47)
        store.add_issue(47, 123)
        assert store.remove_issue(47, 123) is True
        assert 123 not in store.get_issue_numbers(47)

    def test_remove_nonexistent_issue(self, store):
        store.create_sprint(47)
        assert store.remove_issue(47, 999) is False

    def test_remove_from_nonexistent_sprint(self, store):
        assert store.remove_issue(999, 123) is False

    def test_remove_is_soft_delete(self, store):
        """Removed issues should still have a row with removed_at set."""
        store.create_sprint(47)
        store.add_issue(47, 123)
        store.remove_issue(47, 123)

        sprint = store.get_sprint(47)
        row = store.conn.execute(
            "SELECT removed_at FROM sprint_issues WHERE sprint_id = ? AND issue_number = ?",
            (sprint["id"], 123),
        ).fetchone()
        assert row["removed_at"] is not None

    def test_double_remove(self, store):
        store.create_sprint(47)
        store.add_issue(47, 123)
        assert store.remove_issue(47, 123) is True
        assert store.remove_issue(47, 123) is False


class TestGetIssueNumbers:
    def test_empty_sprint(self, store):
        store.create_sprint(47)
        assert store.get_issue_numbers(47) == []

    def test_excludes_removed(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        store.remove_issue(47, 100)
        assert store.get_issue_numbers(47) == [200]

    def test_nonexistent_sprint(self, store):
        assert store.get_issue_numbers(999) == []


class TestGetAllAssignedNumbers:
    def test_across_sprints(self, store):
        store.create_sprint(46)
        store.create_sprint(47)
        store.add_issue(46, 100)
        store.add_issue(47, 200)
        store.add_issue(47, 300)
        assert store.get_all_assigned_numbers() == {100, 200, 300}

    def test_excludes_removed(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        store.remove_issue(47, 100)
        assert store.get_all_assigned_numbers() == {200}

    def test_empty(self, store):
        assert store.get_all_assigned_numbers() == set()


class TestCarryOver:
    def test_carry_over(self, store):
        store.create_sprint(47)
        store.create_sprint(48)
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        store.add_issue(47, 300)

        carried = store.carry_over(47, 48, [100, 200])
        assert carried == [100, 200]

        # Removed from source
        assert store.get_issue_numbers(47) == [300]

        # Added to target
        assert store.get_issue_numbers(48) == [100, 200]

    def test_carry_over_with_source_rollover(self, store):
        store.create_sprint(47)
        store.create_sprint(48)
        store.add_issue(47, 100)
        store.carry_over(47, 48, [100])

        sprint_48 = store.get_sprint(48)
        row = store.conn.execute(
            "SELECT source FROM sprint_issues WHERE sprint_id = ? AND issue_number = ?",
            (sprint_48["id"], 100),
        ).fetchone()
        assert row["source"] == "rollover"

    def test_carry_over_source_not_found(self, store):
        """Carry-over raises ValueError if source sprint missing."""
        store.create_sprint(48)
        with pytest.raises(ValueError, match="Source sprint"):
            store.carry_over(999, 48, [100])

    def test_carry_over_target_not_found(self, store):
        """Carry-over raises ValueError if target sprint missing."""
        store.create_sprint(47)
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="Target sprint"):
            store.carry_over(47, 999, [100])
        # Source issue should be preserved (rolled back)
        assert 100 in store.get_issue_numbers(47)

    def test_carry_over_skips_missing_issues(self, store):
        """Issues not in source sprint are silently skipped."""
        store.create_sprint(47)
        store.create_sprint(48)
        store.add_issue(47, 100)
        carried = store.carry_over(47, 48, [100, 200, 300])
        assert carried == [100]
        assert store.get_issue_numbers(48) == [100]

    def test_duplicate_add_is_noop(self, store):
        """Adding the same issue twice should not create duplicate rows."""
        store.create_sprint(47)
        store.add_issue(47, 100)
        store.add_issue(47, 100)  # No-op
        assert store.get_issue_numbers(47) == [100]

    def test_add_after_remove_creates_new_row(self, store):
        """Re-adding a removed issue creates a new active row."""
        store.create_sprint(47)
        store.add_issue(47, 100)
        store.remove_issue(47, 100)
        assert store.get_issue_numbers(47) == []
        store.add_issue(47, 100)
        assert store.get_issue_numbers(47) == [100]


class TestMoveIssue:
    def test_move_between_sprints(self, store):
        store.create_sprint(47)
        store.create_sprint(48)
        store.add_issue(47, 100)

        assert store.move_issue(100, 47, 48) is True
        assert store.get_issue_numbers(47) == []
        assert store.get_issue_numbers(48) == [100]

    def test_move_not_in_source(self, store):
        store.create_sprint(47)
        store.create_sprint(48)
        assert store.move_issue(100, 47, 48) is False
        # Connection should not be left in a transaction
        assert not store.conn.in_transaction

    def test_move_source_not_found(self, store):
        store.create_sprint(48)
        assert store.move_issue(100, 999, 48) is False

    def test_move_target_not_found(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        assert store.move_issue(100, 47, 999) is False
        # Issue should still be in source (no partial mutation)
        assert 100 in store.get_issue_numbers(47)

    def test_move_already_in_target(self, store):
        """Move when issue already exists in target should not duplicate."""
        store.create_sprint(47)
        store.create_sprint(48)
        store.add_issue(47, 100)
        store.add_issue(48, 100)

        assert store.move_issue(100, 47, 48) is True
        assert store.get_issue_numbers(47) == []
        assert store.get_issue_numbers(48) == [100]  # No duplicate

    def test_carry_over_skips_existing_in_target(self, store):
        """Carry-over should not create duplicates when issue already in target."""
        store.create_sprint(47)
        store.create_sprint(48)
        store.add_issue(47, 100)
        store.add_issue(48, 100)  # Already in target

        carried = store.carry_over(47, 48, [100])
        assert carried == [100]
        assert store.get_issue_numbers(48) == [100]  # Still just one


class TestStartSprint:
    def test_start_basic(self, store):
        store.create_sprint(47)
        result = store.start_sprint(47, start_date="2026-03-09")
        assert result["status"] == "in_progress"
        assert result["start_date"] == "2026-03-09"
        assert result["snapshot"] == "start"
        sprint = store.get_sprint(47)
        assert sprint["status"] == "in_progress"
        assert sprint["start_date"] == "2026-03-09"

    def test_start_captures_snapshot(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        store.start_sprint(47, start_date="2026-03-09")
        snap = store.get_snapshot(47, "start")
        assert snap is not None
        assert snap["total_issues"] == 2
        assert snap["issue_numbers"] == [100, 200]

    def test_start_already_in_progress(self, store):
        store.create_sprint(47)
        store.start_sprint(47, start_date="2026-03-09")
        with pytest.raises(ValueError, match="must be planned"):
            store.start_sprint(47, start_date="2026-03-10")

    def test_start_not_found(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.start_sprint(99, start_date="2026-03-09")

    def test_start_returns_issues(self, store):
        store.create_sprint(47)
        store.add_issue(47, 10)
        store.add_issue(47, 20)
        result = store.start_sprint(47, start_date="2026-03-09")
        assert result["issues"] == [10, 20]

    def test_start_rejects_when_another_in_progress(self, store):
        """Only one sprint can be in_progress at a time."""
        store.create_sprint(47)
        store.create_sprint(48)
        store.start_sprint(47, start_date="2026-03-09")
        with pytest.raises(ValueError, match="Sprint 47 is already in progress"):
            store.start_sprint(48, start_date="2026-03-16")
        # Sprint 48 should remain planned
        assert store.get_sprint(48)["status"] == "planned"

    def test_start_allowed_after_closing_previous(self, store):
        """Can start a new sprint after closing the current one."""
        store.create_sprint(47)
        store.create_sprint(48)
        store.start_sprint(47, start_date="2026-03-09")
        store.close_sprint(
            47,
            end_date="2026-03-22",
            total_issues=0,
            total_points=0,
            issue_numbers=[],
        )
        # Now 48 can be started
        result = store.start_sprint(48, start_date="2026-03-23")
        assert result["status"] == "in_progress"


class TestCloseSprint:
    def test_close_basic(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        result = store.close_sprint(
            47,
            end_date="2026-03-22",
            total_issues=1,
            total_points=3,
            issue_numbers=[100],
        )
        assert result["status"] == "completed"
        assert result["end_date"] == "2026-03-22"
        sprint = store.get_sprint(47)
        assert sprint["status"] == "completed"
        assert sprint["end_date"] == "2026-03-22"

    def test_close_captures_end_snapshot(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        store.close_sprint(
            47,
            end_date="2026-03-22",
            total_issues=1,
            total_points=3,
            issue_numbers=[100],
        )
        snap = store.get_snapshot(47, "end")
        assert snap is not None
        assert snap["total_issues"] == 1
        assert snap["total_points"] == 3
        assert snap["issue_numbers"] == [100]

    def test_close_with_carry_over(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.create_sprint(48)
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        result = store.close_sprint(
            47,
            end_date="2026-03-22",
            total_issues=2,
            total_points=0,
            issue_numbers=[100, 200],
            carry_over_to=48,
            carry_over_issues=[100],
        )
        assert result["carried_over"]["to_sprint"] == 48
        assert result["carried_over"]["issues"] == [100]
        assert store.get_issue_numbers(48) == [100]

    def test_close_not_found(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.close_sprint(
                999,
                end_date="2026-03-22",
                total_issues=0,
                total_points=0,
                issue_numbers=[],
            )

    def test_close_already_completed(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.close_sprint(
            47, end_date="2026-03-22", total_issues=0, total_points=0, issue_numbers=[]
        )
        with pytest.raises(ValueError, match="completed"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=0,
                total_points=0,
                issue_numbers=[],
            )

    def test_close_cancelled_sprint(self, store):
        _create_sprint_in_status(store, 47, "cancelled")
        with pytest.raises(ValueError, match="cancelled"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=0,
                total_points=0,
                issue_numbers=[],
            )

    def test_close_self_carry_over(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="same sprint"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=1,
                total_points=0,
                issue_numbers=[100],
                carry_over_to=47,
                carry_over_issues=[100],
            )
        # Sprint should NOT be closed
        assert store.get_sprint(47)["status"] == "in_progress"

    def test_close_carry_over_target_not_found(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="not found"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=1,
                total_points=0,
                issue_numbers=[100],
                carry_over_to=99,
                carry_over_issues=[100],
            )
        # Sprint should NOT be closed (atomic rollback)
        assert store.get_sprint(47)["status"] == "in_progress"
        # End snapshot should NOT exist
        assert store.get_snapshot(47, "end") is None
        # Issue should still be in source sprint
        assert 100 in store.get_issue_numbers(47)

    def test_close_carry_over_to_completed_sprint(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        _create_sprint_in_status(store, 48, "completed")
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="completed"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=1,
                total_points=0,
                issue_numbers=[100],
                carry_over_to=48,
                carry_over_issues=[100],
            )
        assert store.get_sprint(47)["status"] == "in_progress"

    def test_close_with_carry_over_success(self, store):
        """Verify all three operations (snapshot, carry-over, status) succeed atomically."""
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        store.create_sprint(48)
        store.close_sprint(
            47,
            end_date="2026-03-22",
            total_issues=1,
            total_points=0,
            issue_numbers=[100],
            carry_over_to=48,
            carry_over_issues=[100],
        )
        assert store.get_sprint(47)["status"] == "completed"
        assert store.get_snapshot(47, "end") is not None
        assert store.get_issue_numbers(48) == [100]

    def test_close_rollback_on_mid_operation_error(self, store):
        """Verify rollback when carry-over target doesn't exist.

        This tests the atomic rollback path: snapshot + carry-over are
        attempted inside a savepoint; if carry-over target validation
        fails pre-savepoint, nothing is changed.
        """
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        store.add_issue(47, 200)

        with pytest.raises(ValueError, match="not found"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=2,
                total_points=0,
                issue_numbers=[100, 200],
                carry_over_to=99,
                carry_over_issues=[100],
            )

        # Everything should be unchanged
        assert store.get_sprint(47)["status"] == "in_progress"
        assert store.get_snapshot(47, "end") is None
        assert store.get_issue_numbers(47) == [100, 200]

    def test_close_carry_over_issues_required(self, store):
        """carry_over_to without carry_over_issues should be rejected."""
        _create_sprint_in_status(store, 47, "in_progress")
        store.create_sprint(48)
        with pytest.raises(ValueError, match="carry_over_issues required"):
            store.close_sprint(
                47,
                end_date="2026-03-22",
                total_issues=0,
                total_points=0,
                issue_numbers=[],
                carry_over_to=48,
                carry_over_issues=None,
            )
        assert store.get_sprint(47)["status"] == "in_progress"

    def test_close_with_empty_carry_over_list(self, store):
        """carry_over_to with empty list should close cleanly (no issues to move)."""
        _create_sprint_in_status(store, 49, "in_progress")
        store.create_sprint(50)
        result = store.close_sprint(
            49,
            end_date="2026-03-22",
            total_issues=0,
            total_points=0,
            issue_numbers=[],
            carry_over_to=50,
            carry_over_issues=[],
        )
        assert result["status"] == "completed"
        assert result["carried_over"]["issues"] == []
        assert store.get_sprint(49)["status"] == "completed"


class TestMoveIssueGuards:
    def test_move_self_transfer_rejected(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        assert store.move_issue(100, 47, 47) is False
        assert store.get_issue_numbers(47) == [100]

    def test_move_to_completed_sprint_rejected(self, store):
        store.create_sprint(47)
        _create_sprint_in_status(store, 48, "completed")
        store.add_issue(47, 100)
        assert store.move_issue(100, 47, 48) is False
        assert store.get_issue_numbers(47) == [100]

    def test_move_to_cancelled_sprint_rejected(self, store):
        store.create_sprint(47)
        _create_sprint_in_status(store, 48, "cancelled")
        store.add_issue(47, 100)
        assert store.move_issue(100, 47, 48) is False
        assert store.get_issue_numbers(47) == [100]


class TestCarryOverGuards:
    def test_carry_over_self_transfer_rejected(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="same sprint"):
            store.carry_over(47, 47, [100])
        assert store.get_issue_numbers(47) == [100]

    def test_carry_over_to_completed_sprint_rejected(self, store):
        store.create_sprint(47)
        _create_sprint_in_status(store, 48, "completed")
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="completed"):
            store.carry_over(47, 48, [100])
        assert store.get_issue_numbers(47) == [100]

    def test_carry_over_to_cancelled_sprint_rejected(self, store):
        store.create_sprint(47)
        _create_sprint_in_status(store, 48, "cancelled")
        store.add_issue(47, 100)
        with pytest.raises(ValueError, match="cancelled"):
            store.carry_over(47, 48, [100])
        assert store.get_issue_numbers(47) == [100]


class TestCancelSprint:
    def test_cancel_planned(self, store):
        store.create_sprint(47)
        result = store.cancel_sprint(47)
        assert result["status"] == "cancelled"
        assert "snapshot" not in result
        assert store.get_sprint(47)["status"] == "cancelled"

    def test_cancel_in_progress_captures_snapshot(self, store):
        store.create_sprint(47)
        store.start_sprint(47, start_date="2026-03-09")
        store.add_issue(47, 100)
        store.add_issue(47, 200)
        result = store.cancel_sprint(47)
        assert result["status"] == "cancelled"
        assert result["snapshot"] == "end"
        snap = store.get_snapshot(47, "end")
        assert snap is not None
        assert snap["total_issues"] == 2
        assert snap["issue_numbers"] == [100, 200]

    def test_cancel_active_sets_end_date(self, store):
        """Cancelling an active sprint sets end_date for timeline reporting."""
        store.create_sprint(47)
        store.start_sprint(47, start_date="2026-03-09")
        store.cancel_sprint(47)
        sprint = store.get_sprint(47)
        assert sprint["end_date"] is not None  # Should be set to today

    def test_cancel_planned_no_end_date(self, store):
        """Cancelling a planned sprint does not set end_date."""
        store.create_sprint(48)
        store.cancel_sprint(48)
        sprint = store.get_sprint(48)
        assert sprint["end_date"] is None

    def test_cancel_already_completed(self, store):
        _create_sprint_in_status(store, 47, "completed")
        with pytest.raises(ValueError, match="already completed"):
            store.cancel_sprint(47)

    def test_cancel_already_cancelled(self, store):
        _create_sprint_in_status(store, 47, "cancelled")
        with pytest.raises(ValueError, match="already cancelled"):
            store.cancel_sprint(47)

    def test_cancel_not_found(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.cancel_sprint(99)


class TestFrozenSprintIssueGuards:
    def test_add_to_completed_sprint_rejected(self, store):
        _create_sprint_in_status(store, 47, "completed")
        assert store.add_issue(47, 100) is False

    def test_add_to_cancelled_sprint_rejected(self, store):
        _create_sprint_in_status(store, 47, "cancelled")
        assert store.add_issue(47, 100) is False

    def test_remove_from_completed_sprint_rejected(self, store):
        _create_sprint_in_status(store, 47, "in_progress")
        store.add_issue(47, 100)
        # Close the sprint via direct SQL (test setup)
        store.conn.execute(
            "UPDATE sprints SET status = 'completed' WHERE repo_owner = ? AND repo_name = ? AND number = ?",
            (store.repo_owner, store.repo_name, 47),
        )
        store.conn.commit()
        assert store.remove_issue(47, 100) is False
        # Issue should still be there (no mutation)
        assert 100 in store.get_issue_numbers(47)

    def test_remove_from_cancelled_sprint_rejected(self, store):
        store.create_sprint(47)
        store.add_issue(47, 100)
        # Cancel via direct SQL (test setup)
        store.conn.execute(
            "UPDATE sprints SET status = 'cancelled' WHERE repo_owner = ? AND repo_name = ? AND number = ?",
            (store.repo_owner, store.repo_name, 47),
        )
        store.conn.commit()
        assert store.remove_issue(47, 100) is False
        assert 100 in store.get_issue_numbers(47)


class TestCarryOverFromFrozenSource:
    def test_carry_over_from_completed_rejected(self, store):
        _create_sprint_in_status(store, 47, "completed")
        store.create_sprint(48)
        with pytest.raises(ValueError, match="completed"):
            store.carry_over(47, 48, [100])

    def test_carry_over_from_cancelled_rejected(self, store):
        _create_sprint_in_status(store, 47, "cancelled")
        store.create_sprint(48)
        with pytest.raises(ValueError, match="cancelled"):
            store.carry_over(47, 48, [100])


class TestSnapshots:
    def test_take_and_get_snapshot(self, store):
        store.create_sprint(47)
        store.take_snapshot(
            47,
            "start",
            total_issues=8,
            total_points=24,
            issue_numbers=[100, 101, 102, 103, 104, 105, 106, 107],
        )

        snap = store.get_snapshot(47, "start")
        assert snap is not None
        assert snap["total_issues"] == 8
        assert snap["total_points"] == 24
        assert snap["issue_numbers"] == [100, 101, 102, 103, 104, 105, 106, 107]

    def test_get_nonexistent_snapshot(self, store):
        store.create_sprint(47)
        assert store.get_snapshot(47, "end") is None

    def test_snapshot_nonexistent_sprint(self, store):
        assert (
            store.take_snapshot(
                999, "start", total_issues=0, total_points=0, issue_numbers=[]
            )
            is False
        )

    def test_replace_snapshot(self, store):
        """Re-taking a snapshot should replace the existing one."""
        store.create_sprint(47)
        store.take_snapshot(
            47, "start", total_issues=8, total_points=24, issue_numbers=[1, 2, 3]
        )
        store.take_snapshot(
            47, "start", total_issues=10, total_points=30, issue_numbers=[1, 2, 3, 4, 5]
        )

        snap = store.get_snapshot(47, "start")
        assert snap["total_issues"] == 10
        assert snap["total_points"] == 30
        assert len(snap["issue_numbers"]) == 5

    def test_both_snapshot_types(self, store):
        store.create_sprint(47)
        store.take_snapshot(
            47, "start", total_issues=8, total_points=24, issue_numbers=[1]
        )
        store.take_snapshot(
            47, "end", total_issues=10, total_points=28, issue_numbers=[1, 2]
        )

        start = store.get_snapshot(47, "start")
        end = store.get_snapshot(47, "end")
        assert start["total_issues"] == 8
        assert end["total_issues"] == 10
