"""Sprint data store backed by SQLite."""

import json
import sqlite3
from datetime import UTC, datetime


class SprintStore:
    """Repository-scoped sprint CRUD operations.

    All methods are scoped to (repo_owner, repo_name) passed at construction.
    Uses a shared sqlite3.Connection (caller manages lifecycle).
    """

    def __init__(self, conn: sqlite3.Connection, repo_owner: str, repo_name: str):
        self.conn = conn
        self.repo_owner = repo_owner
        self.repo_name = repo_name

    # --- Sprint CRUD ---

    # Statuses allowed at creation time
    _CREATION_STATUSES = {"planned"}

    def create_sprint(
        self,
        number: int,
        *,
        status: str = "planned",
        start_date: str | None = None,
        end_date: str | None = None,
        goal: str = "",
    ) -> dict:
        """Create a new sprint.

        Only 'planned' status is allowed at creation. Use start_sprint()
        and close_sprint() for lifecycle transitions.

        Returns:
            The created sprint as a dict.

        Raises:
            sqlite3.IntegrityError: If sprint number already exists for this repo.
            ValueError: If status is not 'planned'.
        """
        if status not in self._CREATION_STATUSES:
            msg = (
                f"Cannot create sprint with status '{status}'. "
                f"Create as 'planned', then use start_sprint()/close_sprint() for transitions."
            )
            raise ValueError(msg)
        self.conn.execute(
            """INSERT INTO sprints
               (repo_owner, repo_name, number, status, start_date, end_date, goal)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                self.repo_owner,
                self.repo_name,
                number,
                status,
                start_date,
                end_date,
                goal,
            ),
        )
        self.conn.commit()
        return self.get_sprint(number)  # type: ignore[return-value]

    def get_sprint(self, number: int) -> dict | None:
        """Get a sprint by number.

        Returns:
            Sprint dict or None if not found.
        """
        row = self.conn.execute(
            """SELECT * FROM sprints
               WHERE repo_owner = ? AND repo_name = ? AND number = ?""",
            (self.repo_owner, self.repo_name, number),
        ).fetchone()
        return dict(row) if row else None

    # Status values that require dedicated workflow methods
    _WORKFLOW_STATUSES = {"in_progress", "completed", "cancelled"}

    def update_sprint(self, number: int, **fields: str | None) -> dict | None:
        """Update sprint fields (dates and goal only).

        Allowed fields: start_date, end_date, goal.
        Status cannot be changed via this method — use the dedicated
        workflow methods: start_sprint(), close_sprint(), cancel_sprint().

        Returns:
            Updated sprint dict, or None if not found.

        Raises:
            ValueError: If status is provided.
        """
        allowed = {"start_date", "end_date", "goal"}
        updates = {k: v for k, v in fields.items() if k in allowed}

        # Block updates to frozen sprints
        sprint = self.get_sprint(number)
        if not sprint:
            return None
        if sprint["status"] in self._FROZEN_STATUSES:
            msg = f"Cannot update {sprint['status']} sprint"
            raise ValueError(msg)

        # Block all status changes — must use workflow methods
        if "status" in fields:
            method_map = {
                "in_progress": "start_sprint",
                "completed": "close_sprint",
                "cancelled": "cancel_sprint",
                "planned": "update_sprint (status changes not allowed)",
            }
            method = method_map.get(
                str(fields["status"]), "the appropriate workflow method"
            )
            msg = (
                f"Cannot change status via update_sprint(). " f"Use {method}() instead."
            )
            raise ValueError(msg)
        if not updates:
            return self.get_sprint(number)

        updates["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params: list[str | int | None] = list(updates.values())
        params.extend([self.repo_owner, self.repo_name, number])

        self.conn.execute(
            f"UPDATE sprints SET {set_clause} "  # noqa: S608
            "WHERE repo_owner = ? AND repo_name = ? AND number = ?",
            params,
        )
        self.conn.commit()
        return self.get_sprint(number)

    def list_sprints(self, *, status: str | None = None) -> list[dict]:
        """List sprints, optionally filtered by status.

        Returns:
            List of sprint dicts, ordered by number descending.
        """
        if status:
            rows = self.conn.execute(
                """SELECT * FROM sprints
                   WHERE repo_owner = ? AND repo_name = ? AND status = ?
                   ORDER BY number DESC""",
                (self.repo_owner, self.repo_name, status),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM sprints
                   WHERE repo_owner = ? AND repo_name = ?
                   ORDER BY number DESC""",
                (self.repo_owner, self.repo_name),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_current_sprint_number(self) -> int | None:
        """Get the highest in_progress sprint number.

        Returns:
            Sprint number or None if no sprint is in progress.
        """
        row = self.conn.execute(
            """SELECT number FROM sprints
               WHERE repo_owner = ? AND repo_name = ? AND status = 'in_progress'
               ORDER BY number DESC LIMIT 1""",
            (self.repo_owner, self.repo_name),
        ).fetchone()
        return row["number"] if row else None

    def start_sprint(
        self,
        number: int,
        *,
        start_date: str,
    ) -> dict:
        """Atomically start a sprint: status update + start snapshot.

        Args:
            number: Sprint number to start.
            start_date: Start date string (YYYY-MM-DD).

        Returns:
            Result dict with sprint info, snapshot marker, and issue list.

        Raises:
            ValueError: If sprint not found, not in 'planned' status,
                or another sprint is already in_progress.
        """
        sprint = self.get_sprint(number)
        if not sprint:
            msg = f"Sprint {number} not found"
            raise ValueError(msg)
        if sprint["status"] != "planned":
            msg = f"Sprint {number} is {sprint['status']} (must be planned to start)"
            raise ValueError(msg)

        # Enforce single active sprint invariant
        current = self.get_current_sprint_number()
        if current is not None:
            msg = f"Sprint {current} is already in progress"
            raise ValueError(msg)

        issues = self.get_issue_numbers(number)
        updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("SAVEPOINT start_sprint")
        try:
            # 1. Update status
            self.conn.execute(
                """UPDATE sprints SET status = ?, start_date = ?, updated_at = ?
                   WHERE repo_owner = ? AND repo_name = ? AND number = ?""",
                (
                    "in_progress",
                    start_date,
                    updated_at,
                    self.repo_owner,
                    self.repo_name,
                    number,
                ),
            )

            # 2. Capture start snapshot
            self.conn.execute(
                """INSERT OR REPLACE INTO sprint_snapshots
                   (sprint_id, snapshot_type, total_issues, total_points, issue_numbers)
                   VALUES (?, ?, ?, ?, ?)""",
                (sprint["id"], "start", len(issues), 0, json.dumps(issues)),
            )

            self.conn.execute("RELEASE start_sprint")
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK TO start_sprint")
            self.conn.execute("RELEASE start_sprint")
            msg = f"Cannot start sprint {number}: another sprint is already in progress"
            raise ValueError(msg) from None
        except Exception:
            self.conn.execute("ROLLBACK TO start_sprint")
            self.conn.execute("RELEASE start_sprint")
            raise
        self.conn.commit()

        return {
            "number": number,
            "status": "in_progress",
            "start_date": start_date,
            "snapshot": "start",
            "issues": issues,
        }

    def close_sprint(
        self,
        number: int,
        *,
        end_date: str,
        total_issues: int,
        total_points: int,
        issue_numbers: list[int],
        carry_over_to: int | None = None,
        carry_over_issues: list[int] | None = None,
    ) -> dict:
        """Atomically close a sprint: snapshot + carry-over + status update.

        All operations are wrapped in a single savepoint so that either
        everything succeeds or nothing changes.

        Args:
            number: Sprint number to close.
            end_date: End date string (YYYY-MM-DD).
            total_issues: Total issue count for the end snapshot.
            total_points: Total points for the end snapshot.
            issue_numbers: All issue numbers for the end snapshot.
            carry_over_to: Optional target sprint number for carry-over.
            carry_over_issues: Issue numbers to carry over (required if carry_over_to set).

        Returns:
            Result dict with status, end_date, and optional carried_over info.

        Raises:
            ValueError: If sprint not found, already completed, or carry-over invalid.
        """
        sprint = self.get_sprint(number)
        if not sprint:
            msg = f"Sprint {number} not found"
            raise ValueError(msg)
        if sprint["status"] != "in_progress":
            msg = (
                f"Sprint {number} is {sprint['status']} (must be in_progress to close)"
            )
            raise ValueError(msg)

        # Pre-validate carry-over target
        to_row = None
        if carry_over_to is not None:
            if carry_over_issues is None:
                msg = "carry_over_issues required when carry_over_to is set"
                raise ValueError(msg)
            if carry_over_to == number:
                msg = "Cannot carry over to the same sprint"
                raise ValueError(msg)
            to_row = self.get_sprint(carry_over_to)
            if not to_row:
                msg = f"Carry-over target sprint {carry_over_to} not found"
                raise ValueError(msg)
            if to_row["status"] in ("completed", "cancelled"):
                msg = f"Cannot carry over to {to_row['status']} sprint"
                raise ValueError(msg)

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        updated_at = now
        carried: list[int] = []

        self.conn.execute("SAVEPOINT close_sprint")
        try:
            # 1. Take end snapshot
            self.conn.execute(
                """INSERT OR REPLACE INTO sprint_snapshots
                   (sprint_id, snapshot_type, total_issues, total_points, issue_numbers)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    sprint["id"],
                    "end",
                    total_issues,
                    total_points,
                    json.dumps(issue_numbers),
                ),
            )

            # 2. Carry over if requested (empty list is valid — nothing to move)
            if carry_over_to is not None and to_row and carry_over_issues is not None:
                for num in carry_over_issues:
                    cursor = self.conn.execute(
                        """UPDATE sprint_issues SET removed_at = ?
                           WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
                        (now, sprint["id"], num),
                    )
                    if cursor.rowcount == 0:
                        continue
                    existing = self.conn.execute(
                        """SELECT 1 FROM sprint_issues
                           WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
                        (to_row["id"], num),
                    ).fetchone()
                    if not existing:
                        add_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
                        self.conn.execute(
                            """INSERT INTO sprint_issues (sprint_id, issue_number, source, added_at)
                               VALUES (?, ?, 'rollover', ?)""",
                            (to_row["id"], num, add_ts),
                        )
                    carried.append(num)

            # 3. Mark sprint as completed
            self.conn.execute(
                """UPDATE sprints SET status = ?, end_date = ?, updated_at = ?
                   WHERE repo_owner = ? AND repo_name = ? AND number = ?""",
                (
                    "completed",
                    end_date,
                    updated_at,
                    self.repo_owner,
                    self.repo_name,
                    number,
                ),
            )

            self.conn.execute("RELEASE close_sprint")
        except Exception:
            self.conn.execute("ROLLBACK TO close_sprint")
            self.conn.execute("RELEASE close_sprint")
            raise
        self.conn.commit()

        result: dict = {"sprint": number, "status": "completed", "end_date": end_date}
        if carry_over_to is not None:
            result["carried_over"] = {"to_sprint": carry_over_to, "issues": carried}
        return result

    def cancel_sprint(self, number: int) -> dict:
        """Cancel a sprint.

        If the sprint was in_progress, captures an end snapshot first.
        Planned sprints are cancelled directly.

        Args:
            number: Sprint number to cancel.

        Returns:
            Result dict with sprint info and optional snapshot marker.

        Raises:
            ValueError: If sprint not found, or already completed/cancelled.
        """
        sprint = self.get_sprint(number)
        if not sprint:
            msg = f"Sprint {number} not found"
            raise ValueError(msg)
        if sprint["status"] in ("completed", "cancelled"):
            msg = f"Sprint {number} is already {sprint['status']}"
            raise ValueError(msg)

        was_active = sprint["status"] == "in_progress"
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("SAVEPOINT cancel_sprint")
        try:
            # Capture end snapshot if sprint was active
            if was_active:
                issues = self.get_issue_numbers(number)
                self.conn.execute(
                    """INSERT OR REPLACE INTO sprint_snapshots
                       (sprint_id, snapshot_type, total_issues, total_points, issue_numbers)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sprint["id"], "end", len(issues), 0, json.dumps(issues)),
                )

            # Set end_date on active sprints for timeline reporting
            end_date = now[:10] if was_active else None
            if end_date:
                self.conn.execute(
                    """UPDATE sprints SET status = ?, end_date = ?, updated_at = ?
                       WHERE repo_owner = ? AND repo_name = ? AND number = ?""",
                    (
                        "cancelled",
                        end_date,
                        now,
                        self.repo_owner,
                        self.repo_name,
                        number,
                    ),
                )
            else:
                self.conn.execute(
                    """UPDATE sprints SET status = ?, updated_at = ?
                       WHERE repo_owner = ? AND repo_name = ? AND number = ?""",
                    ("cancelled", now, self.repo_owner, self.repo_name, number),
                )
            self.conn.execute("RELEASE cancel_sprint")
        except Exception:
            self.conn.execute("ROLLBACK TO cancel_sprint")
            self.conn.execute("RELEASE cancel_sprint")
            raise
        self.conn.commit()

        result: dict = {"number": number, "status": "cancelled"}
        if was_active:
            result["snapshot"] = "end"
        return result

    # --- Issue Management ---

    # Statuses that are frozen (no issue add/remove)
    _FROZEN_STATUSES = {"completed", "cancelled"}

    def add_issue(
        self,
        sprint_number: int,
        issue_number: int,
        *,
        source: str = "manual",
    ) -> bool:
        """Add an issue to a sprint.

        If the issue is already active in this sprint, this is a no-op (returns True).
        If the issue was previously removed, a new row is created preserving history.

        Returns:
            True if added (or already present), False if sprint not found or frozen.
        """
        sprint = self.get_sprint(sprint_number)
        if not sprint:
            return False
        if sprint["status"] in self._FROZEN_STATUSES:
            return False

        # Check if already active in this sprint
        existing = self.conn.execute(
            """SELECT 1 FROM sprint_issues
               WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
            (sprint["id"], issue_number),
        ).fetchone()
        if existing:
            return True

        # Use explicit timestamp to avoid collisions with recently-removed rows
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        self.conn.execute(
            """INSERT INTO sprint_issues (sprint_id, issue_number, source, added_at)
               VALUES (?, ?, ?, ?)""",
            (sprint["id"], issue_number, source, now),
        )
        self.conn.commit()
        return True

    def remove_issue(self, sprint_number: int, issue_number: int) -> bool:
        """Soft-remove an issue from a sprint (sets removed_at).

        Returns:
            True if removed, False if not found, already removed, or sprint frozen.
        """
        sprint = self.get_sprint(sprint_number)
        if not sprint:
            return False
        if sprint["status"] in self._FROZEN_STATUSES:
            return False

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            """UPDATE sprint_issues SET removed_at = ?
               WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
            (now, sprint["id"], issue_number),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_issue_numbers(self, sprint_number: int) -> list[int]:
        """Get active issue numbers for a sprint (removed_at IS NULL).

        Returns:
            List of issue numbers, or empty list if sprint not found.
        """
        sprint = self.get_sprint(sprint_number)
        if not sprint:
            return []

        rows = self.conn.execute(
            """SELECT issue_number FROM sprint_issues
               WHERE sprint_id = ? AND removed_at IS NULL
               ORDER BY issue_number""",
            (sprint["id"],),
        ).fetchall()
        return [r["issue_number"] for r in rows]

    def get_all_assigned_numbers(self) -> set[int]:
        """Get all issue numbers currently assigned to any sprint.

        Used for computing backlog (issues not in any sprint).

        Returns:
            Set of issue numbers across all sprints for this repo.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT si.issue_number
               FROM sprint_issues si
               JOIN sprints s ON si.sprint_id = s.id
               WHERE s.repo_owner = ? AND s.repo_name = ?
                 AND si.removed_at IS NULL""",
            (self.repo_owner, self.repo_name),
        ).fetchall()
        return {r["issue_number"] for r in rows}

    def move_issue(
        self,
        issue_number: int,
        from_sprint: int,
        to_sprint: int,
    ) -> bool:
        """Atomically move an issue between sprints.

        Uses a savepoint so removal + addition either both succeed or neither.

        Returns:
            True if moved, False if source/target sprint not found,
            issue not in source sprint, or from == to.
        """
        if from_sprint == to_sprint:
            return False

        from_row = self.get_sprint(from_sprint)
        to_row = self.get_sprint(to_sprint)
        if not from_row or not to_row:
            return False
        if from_row["status"] in ("completed", "cancelled"):
            return False
        if to_row["status"] in ("completed", "cancelled"):
            return False

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        add_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

        self.conn.execute("SAVEPOINT move_issue")
        try:
            # Soft-remove from source
            cursor = self.conn.execute(
                """UPDATE sprint_issues SET removed_at = ?
                   WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
                (now, from_row["id"], issue_number),
            )
            if cursor.rowcount == 0:
                self.conn.execute("ROLLBACK TO move_issue")
                self.conn.execute("RELEASE move_issue")
                return False

            # Check if already active in target
            existing = self.conn.execute(
                """SELECT 1 FROM sprint_issues
                   WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
                (to_row["id"], issue_number),
            ).fetchone()
            if not existing:
                self.conn.execute(
                    """INSERT INTO sprint_issues (sprint_id, issue_number, source, added_at)
                       VALUES (?, ?, 'manual', ?)""",
                    (to_row["id"], issue_number, add_ts),
                )

            self.conn.execute("RELEASE move_issue")
        except Exception:
            self.conn.execute("ROLLBACK TO move_issue")
            self.conn.execute("RELEASE move_issue")
            raise
        self.conn.commit()
        return True

    def carry_over(
        self,
        from_sprint: int,
        to_sprint: int,
        issue_numbers: list[int],
    ) -> list[int]:
        """Carry over issues from one sprint to another.

        Removes issues from the source sprint and adds them to the target
        with source='rollover'. The entire operation is wrapped in a
        transaction — if any step fails, all changes are rolled back.

        Returns:
            List of issue numbers that were actually carried over.

        Raises:
            ValueError: If source or target sprint does not exist,
                from == to, or target is completed/cancelled.
        """
        if from_sprint == to_sprint:
            msg = "Cannot carry over to the same sprint"
            raise ValueError(msg)

        from_row = self.get_sprint(from_sprint)
        to_row = self.get_sprint(to_sprint)
        if not from_row:
            msg = f"Source sprint {from_sprint} not found"
            raise ValueError(msg)
        if from_row["status"] in ("completed", "cancelled"):
            msg = f"Cannot carry over from {from_row['status']} sprint"
            raise ValueError(msg)
        if not to_row:
            msg = f"Target sprint {to_sprint} not found"
            raise ValueError(msg)
        if to_row["status"] in ("completed", "cancelled"):
            msg = f"Cannot carry over to {to_row['status']} sprint"
            raise ValueError(msg)

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        carried: list[int] = []

        # Use explicit savepoint for atomic carry-over
        self.conn.execute("SAVEPOINT carry_over")
        try:
            for num in issue_numbers:
                # Soft-remove from source
                cursor = self.conn.execute(
                    """UPDATE sprint_issues SET removed_at = ?
                       WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
                    (now, from_row["id"], num),
                )
                if cursor.rowcount == 0:
                    continue  # Not in source sprint, skip
                # Add to target (skip if already active there)
                existing = self.conn.execute(
                    """SELECT 1 FROM sprint_issues
                       WHERE sprint_id = ? AND issue_number = ? AND removed_at IS NULL""",
                    (to_row["id"], num),
                ).fetchone()
                if not existing:
                    add_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
                    self.conn.execute(
                        """INSERT INTO sprint_issues (sprint_id, issue_number, source, added_at)
                           VALUES (?, ?, 'rollover', ?)""",
                        (to_row["id"], num, add_ts),
                    )
                carried.append(num)
            self.conn.execute("RELEASE carry_over")
        except Exception:
            self.conn.execute("ROLLBACK TO carry_over")
            self.conn.execute("RELEASE carry_over")
            raise
        self.conn.commit()
        return carried

    # --- Snapshots ---

    def take_snapshot(
        self,
        sprint_number: int,
        snapshot_type: str,
        *,
        total_issues: int,
        total_points: int,
        issue_numbers: list[int],
    ) -> bool:
        """Capture a sprint snapshot (start or end).

        Uses INSERT OR REPLACE to allow re-taking a snapshot.

        Returns:
            True if captured, False if sprint not found.
        """
        sprint = self.get_sprint(sprint_number)
        if not sprint:
            return False

        self.conn.execute(
            """INSERT OR REPLACE INTO sprint_snapshots
               (sprint_id, snapshot_type, total_issues, total_points, issue_numbers)
               VALUES (?, ?, ?, ?, ?)""",
            (
                sprint["id"],
                snapshot_type,
                total_issues,
                total_points,
                json.dumps(issue_numbers),
            ),
        )
        self.conn.commit()
        return True

    def get_snapshot(self, sprint_number: int, snapshot_type: str) -> dict | None:
        """Get a sprint snapshot.

        Returns:
            Snapshot dict with issue_numbers parsed from JSON, or None.
        """
        sprint = self.get_sprint(sprint_number)
        if not sprint:
            return None

        row = self.conn.execute(
            """SELECT * FROM sprint_snapshots
               WHERE sprint_id = ? AND snapshot_type = ?""",
            (sprint["id"], snapshot_type),
        ).fetchone()
        if not row:
            return None

        result = dict(row)
        result["issue_numbers"] = json.loads(result["issue_numbers"])
        return result
