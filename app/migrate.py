"""Migration script: seed SQLite from Gitea labels and milestones.

Usage:
    poetry run python -m app.migrate [--db PATH] [--owner OWNER] [--repo REPO]

Idempotent: uses INSERT OR IGNORE for sprints (UNIQUE constraint),
so re-running is safe.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

from .database import get_connection, init_schema
from .gitea import GiteaClient, GiteaError
from .sprint_store import SprintStore

logger = logging.getLogger(__name__)


def migrate(
    db_path: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> dict:
    """Seed the database from Gitea labels and milestones.

    Args:
        db_path: SQLite database path (None = env/default).
        owner: Repository owner (None = env/tea config).
        repo: Repository name (None = env/tea config).

    Returns:
        Summary dict with counts of created/mapped items.
    """
    conn = get_connection(db_path)
    try:
        return _do_migrate(conn, owner, repo)
    finally:
        conn.close()


def _do_migrate(
    conn: sqlite3.Connection,
    owner: str | None,
    repo: str | None,
) -> dict:
    """Internal migration logic (connection managed by caller)."""
    init_schema(conn)

    client = GiteaClient(owner=owner, repo=repo)
    resolved_owner = client.owner
    resolved_repo = client.repo
    store = SprintStore(conn, resolved_owner, resolved_repo)

    summary = {
        "sprints_created": 0,
        "sprints_skipped": 0,
        "issues_mapped": 0,
        "issues_skipped": 0,
        "orphan_sprints": 0,
    }

    # Step 1: Fetch milestones → create sprint rows
    logger.info("Fetching milestones...")
    milestones = client.get_milestones(state="all")
    milestone_map = {}  # sprint_number -> milestone
    for m in milestones:
        if m.sprint_number is not None:
            milestone_map[m.sprint_number] = m

    for sprint_num, milestone in sorted(milestone_map.items()):
        status = milestone.lifecycle_state
        start_date = str(milestone.start_date) if milestone.start_date else None

        existing = store.get_sprint(sprint_num)
        if existing:
            summary["sprints_skipped"] += 1
            logger.debug("Sprint %d already exists, skipping", sprint_num)
            continue

        # Direct SQL insert to bypass lifecycle enforcement — migration needs
        # to seed historical sprints with their actual status (completed, etc.)
        try:
            conn.execute(
                """INSERT INTO sprints
                   (repo_owner, repo_name, number, status, start_date, goal)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    resolved_owner,
                    resolved_repo,
                    sprint_num,
                    status,
                    start_date,
                    milestone.description or "",
                ),
            )
            conn.commit()
            summary["sprints_created"] += 1
            logger.info(
                "Created sprint %d (status=%s, start=%s)",
                sprint_num,
                status,
                start_date,
            )
        except Exception:
            conn.rollback()
            summary["sprints_skipped"] += 1
            logger.warning("Failed to create sprint %d", sprint_num, exc_info=True)

    # Step 2: Fetch all issues → create sprint_issues rows from sprint/N labels
    logger.info("Fetching issues...")
    all_issues = client.get_all_issues(state="all")

    # Pre-load assigned numbers to avoid N+1 queries
    already_assigned = store.get_all_assigned_numbers()

    sprint_numbers_from_labels: set[int] = set()
    for issue in all_issues:
        if issue.sprint is not None:
            sprint_numbers_from_labels.add(issue.sprint)

            # Ensure sprint exists (may have been created from label but no milestone)
            existing = store.get_sprint(issue.sprint)
            if not existing:
                # Sprint found via label but no milestone — insert as completed (historical)
                try:
                    conn.execute(
                        """INSERT INTO sprints
                           (repo_owner, repo_name, number, status)
                           VALUES (?, ?, ?, 'completed')""",
                        (resolved_owner, resolved_repo, issue.sprint),
                    )
                    conn.commit()
                    summary["orphan_sprints"] += 1
                    logger.info(
                        "Created orphan sprint %d (from label, no milestone)",
                        issue.sprint,
                    )
                except Exception:
                    conn.rollback()
                    logger.debug("Sprint %d already exists (race)", issue.sprint)

            # Add issue to sprint (skip if already present)
            if issue.number in already_assigned:
                summary["issues_skipped"] += 1
                logger.debug(
                    "Issue #%d already in sprint %d", issue.number, issue.sprint
                )
            elif store.add_issue(issue.sprint, issue.number, source="migration"):
                summary["issues_mapped"] += 1
            else:
                summary["issues_skipped"] += 1

    return summary


def main() -> None:
    """CLI entry point for migration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Seed sprint-dash SQLite from Gitea labels/milestones"
    )
    parser.add_argument("--db", help="Database path (default: env/default)")
    parser.add_argument("--owner", help="Gitea repo owner")
    parser.add_argument("--repo", help="Gitea repo name")
    args = parser.parse_args()

    try:
        summary = migrate(db_path=args.db, owner=args.owner, repo=args.repo)
    except GiteaError as e:
        logger.error("Gitea API error: %s", e)
        sys.exit(1)

    logger.info("Migration complete:")
    logger.info("  Sprints created: %d", summary["sprints_created"])
    logger.info("  Sprints skipped: %d", summary["sprints_skipped"])
    logger.info("  Orphan sprints:  %d", summary["orphan_sprints"])
    logger.info("  Issues mapped:   %d", summary["issues_mapped"])
    logger.info("  Issues skipped:  %d", summary["issues_skipped"])


if __name__ == "__main__":
    main()
