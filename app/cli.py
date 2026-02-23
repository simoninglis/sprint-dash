"""sd-cli: Sprint management CLI for Claude Code and terminal use.

Usage:
    sd-cli [--json] [--db PATH] [--owner OWNER] [--repo REPO] COMMAND

Commands:
    sprint list [--status STATUS]     List all sprints
    sprint show NUMBER                Show sprint details with issues
    sprint create NUMBER [opts]       Create a new sprint
    sprint update NUMBER [opts]       Update sprint dates/goal
    sprint start NUMBER               Start a sprint (in_progress + snapshot)
    sprint close NUMBER               Close a sprint (completed + snapshot)
    sprint cancel NUMBER              Cancel a sprint (end snapshot if active)
    sprint current                    Show current in-progress sprint number
    issue list NUMBER                 List issue numbers in a sprint
    issue add NUMBER ISSUE...         Add issues to a sprint
    issue remove NUMBER ISSUE...      Remove issues from a sprint
    issue move FROM ISSUE... --to TO  Move issues between sprints
    batch                             Execute multiple operations from stdin JSON
"""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime

from .database import get_connection, init_schema
from .sprint_store import SprintStore

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: str | None, field_name: str) -> None:
    """Validate a date string is strictly YYYY-MM-DD. Exits on failure."""
    if not value:
        return
    if not _DATE_RE.match(value):
        print(
            f"Error: Invalid {field_name}: expected YYYY-MM-DD format", file=sys.stderr
        )
        sys.exit(1)
    try:
        datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        print(f"Error: Invalid {field_name}: not a valid date", file=sys.stderr)
        sys.exit(1)


def _get_store(args: argparse.Namespace) -> SprintStore:
    """Create a SprintStore from CLI args + env vars.

    Stores the connection on args._conn so main() can close it.
    """
    db_path = args.db or os.getenv("SPRINT_DASH_DB", "/data/sprint-dash.db")
    owner = args.owner or os.getenv("GITEA_OWNER", "")
    repo = args.repo or os.getenv("GITEA_REPO", "")

    if not owner or not repo:
        print(
            "Error: --owner/--repo or GITEA_OWNER/GITEA_REPO required", file=sys.stderr
        )
        sys.exit(1)

    conn = get_connection(db_path)
    init_schema(conn)
    args._conn = conn  # noqa: SLF001
    return SprintStore(conn, owner, repo)


def _output(data: object, *, json_mode: bool) -> None:
    """Print output as JSON or human-readable text."""
    if json_mode:
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _print_row(item)
            else:
                print(item)
    elif isinstance(data, dict):
        _print_row(data)
    else:
        print(data)


def _print_row(d: dict) -> None:
    """Print a dict as a compact key=value line."""
    parts = [f"{k}={v}" for k, v in d.items() if v is not None]
    print("  ".join(parts))


# --- Sprint commands ---


def cmd_sprint_list(args: argparse.Namespace) -> None:
    store = _get_store(args)
    sprints = store.list_sprints(status=args.status)
    if args.json:
        _output(sprints, json_mode=True)
    else:
        if not sprints:
            print("No sprints found.")
            return
        # Table output
        print(f"{'#':<6} {'Status':<14} {'Start':<12} {'End':<12} {'Goal'}")
        print("-" * 70)
        for s in sprints:
            print(
                f"{s['number']:<6} {s['status']:<14} "
                f"{s['start_date'] or '-':<12} "
                f"{s['end_date'] or '-':<12} "
                f"{s['goal'] or ''}"
            )


def cmd_sprint_show(args: argparse.Namespace) -> None:
    store = _get_store(args)
    sprint = store.get_sprint(args.number)
    if not sprint:
        print(f"Sprint {args.number} not found.", file=sys.stderr)
        sys.exit(1)

    issues = store.get_issue_numbers(args.number)
    sprint["issues"] = issues
    sprint["issue_count"] = len(issues)

    start_snap = store.get_snapshot(args.number, "start")
    end_snap = store.get_snapshot(args.number, "end")
    if start_snap:
        sprint["start_snapshot"] = start_snap
    if end_snap:
        sprint["end_snapshot"] = end_snap

    if args.json:
        _output(sprint, json_mode=True)
    else:
        print(f"Sprint {sprint['number']}  [{sprint['status']}]")
        if sprint["goal"]:
            print(f"Goal: {sprint['goal']}")
        print(f"Start: {sprint['start_date'] or '-'}  End: {sprint['end_date'] or '-'}")
        print(f"Issues ({len(issues)}): {', '.join(f'#{n}' for n in issues) or 'none'}")
        if start_snap:
            print(
                f"Start snapshot: {start_snap['total_issues']} issues, {start_snap['total_points']} pts"
            )
        if end_snap:
            print(
                f"End snapshot: {end_snap['total_issues']} issues, {end_snap['total_points']} pts"
            )


def cmd_sprint_create(args: argparse.Namespace) -> None:
    _validate_date(args.start, "start date")
    _validate_date(args.end, "end date")
    store = _get_store(args)
    try:
        sprint = store.create_sprint(
            args.number,
            status=args.status,
            start_date=args.start,
            end_date=args.end,
            goal=args.goal,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _output(sprint, json_mode=args.json)


def cmd_sprint_update(args: argparse.Namespace) -> None:
    _validate_date(args.start, "start date")
    _validate_date(args.end, "end date")
    store = _get_store(args)
    fields: dict[str, str | None] = {}
    if args.start is not None:
        fields["start_date"] = args.start
    if args.end is not None:
        fields["end_date"] = args.end
    if args.goal is not None:
        fields["goal"] = args.goal

    if not fields:
        print("No fields to update. Use --start, --end, or --goal.", file=sys.stderr)
        sys.exit(1)

    try:
        sprint = store.update_sprint(args.number, **fields)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not sprint:
        print(f"Sprint {args.number} not found.", file=sys.stderr)
        sys.exit(1)
    _output(sprint, json_mode=args.json)


def cmd_sprint_start(args: argparse.Namespace) -> None:
    _validate_date(args.start, "start date")
    store = _get_store(args)
    start_date = args.start or datetime.now(UTC).strftime("%Y-%m-%d")

    try:
        result = store.start_sprint(args.number, start_date=start_date)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _output(result, json_mode=True)
    else:
        print(f"Sprint {args.number} started ({start_date})")
        print(f"Start snapshot: {len(result['issues'])} issues")


def cmd_sprint_close(args: argparse.Namespace) -> None:
    store = _get_store(args)
    issues = store.get_issue_numbers(args.number)
    end_date = datetime.now(UTC).strftime("%Y-%m-%d")

    # Normalize: <=0 means "no carry-over" (consistent with API)
    carry_over_to = (
        args.carry_over_to if args.carry_over_to and args.carry_over_to > 0 else None
    )

    try:
        result = store.close_sprint(
            args.number,
            end_date=end_date,
            total_issues=len(issues),
            total_points=0,  # Points require Gitea; CLI captures count only
            issue_numbers=issues,
            carry_over_to=carry_over_to,
            carry_over_issues=issues if carry_over_to is not None else None,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _output(result, json_mode=True)
    else:
        print(f"Sprint {args.number} closed ({end_date})")
        print(f"End snapshot: {len(issues)} issues")
        if "carried_over" in result:
            co = result["carried_over"]
            print(
                f"Carried over {len(co['issues'])} issues to sprint {co['to_sprint']}"
            )


def cmd_sprint_cancel(args: argparse.Namespace) -> None:
    store = _get_store(args)
    try:
        result = store.cancel_sprint(args.number)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _output(result, json_mode=True)
    else:
        print(f"Sprint {args.number} cancelled")
        if result.get("snapshot"):
            print("End snapshot captured (sprint was active)")


def cmd_sprint_current(args: argparse.Namespace) -> None:
    store = _get_store(args)
    number = store.get_current_sprint_number()
    if args.json:
        _output({"current_sprint": number}, json_mode=True)
    elif number:
        print(number)
    else:
        print("No sprint in progress.", file=sys.stderr)
        sys.exit(1)


# --- Issue commands ---


def cmd_issue_list(args: argparse.Namespace) -> None:
    store = _get_store(args)
    sprint = store.get_sprint(args.sprint_number)
    if not sprint:
        print(f"Sprint {args.sprint_number} not found.", file=sys.stderr)
        sys.exit(1)

    issues = store.get_issue_numbers(args.sprint_number)
    if args.json:
        _output(
            {"sprint": args.sprint_number, "issues": issues, "count": len(issues)},
            json_mode=True,
        )
    else:
        if not issues:
            print(f"Sprint {args.sprint_number} has no issues.")
        else:
            print(" ".join(str(n) for n in issues))


def cmd_issue_add(args: argparse.Namespace) -> None:
    store = _get_store(args)
    added = []
    failed = []
    for num in args.issues:
        if store.add_issue(args.sprint_number, num, source=args.source):
            added.append(num)
        else:
            failed.append(num)

    if args.json:
        _output(
            {"sprint": args.sprint_number, "added": added, "failed": failed},
            json_mode=True,
        )
    else:
        if added:
            print(
                f"Added to sprint {args.sprint_number}: {', '.join(f'#{n}' for n in added)}"
            )
        if failed:
            print(f"Failed: {', '.join(f'#{n}' for n in failed)}", file=sys.stderr)
    if failed:
        sys.exit(1)


def cmd_issue_remove(args: argparse.Namespace) -> None:
    store = _get_store(args)
    removed = []
    failed = []
    for num in args.issues:
        if store.remove_issue(args.sprint_number, num):
            removed.append(num)
        else:
            failed.append(num)

    if args.json:
        _output(
            {"sprint": args.sprint_number, "removed": removed, "failed": failed},
            json_mode=True,
        )
    else:
        if removed:
            print(
                f"Removed from sprint {args.sprint_number}: {', '.join(f'#{n}' for n in removed)}"
            )
        if failed:
            print(
                f"Not found/already removed: {', '.join(f'#{n}' for n in failed)}",
                file=sys.stderr,
            )
    if failed:
        sys.exit(1)


def cmd_issue_move(args: argparse.Namespace) -> None:
    store = _get_store(args)
    moved = []
    failed = []
    for num in args.issues:
        if store.move_issue(num, args.from_sprint, args.to_sprint):
            moved.append(num)
        else:
            failed.append(num)

    if args.json:
        _output(
            {
                "from_sprint": args.from_sprint,
                "to_sprint": args.to_sprint,
                "moved": moved,
                "failed": failed,
            },
            json_mode=True,
        )
    else:
        if moved:
            print(
                f"Moved to sprint {args.to_sprint}: {', '.join(f'#{n}' for n in moved)}"
            )
        if failed:
            print(f"Failed: {', '.join(f'#{n}' for n in failed)}", file=sys.stderr)
    if failed:
        sys.exit(1)


# --- Batch ---


def cmd_batch(args: argparse.Namespace) -> None:
    """Execute multiple operations from a JSON array on stdin.

    Each operation is a dict with "command" and "args" keys.
    All operations share one DB connection.

    Input format:
        [
            {"command": "sprint create", "args": {"number": 50, "goal": "test"}},
            {"command": "issue add", "args": {"sprint": 50, "issues": [10, 20]}},
            {"command": "sprint start", "args": {"number": 50}}
        ]

    Supported commands:
        sprint create  - args: number, goal?, start?, end?
        sprint update  - args: number, goal?, start?, end?
        sprint start   - args: number, start?
        sprint close   - args: number, carry_over_to?, carry_over_issues?
        sprint cancel  - args: number
        issue add      - args: sprint, issues (list of ints), source?
        issue remove   - args: sprint, issues (list of ints)
        issue move     - args: from_sprint, to_sprint, issues (list of ints)
    """
    store = _get_store(args)

    raw = sys.stdin.read()
    try:
        operations = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(operations, list):
        print("Expected JSON array of operations", file=sys.stderr)
        sys.exit(1)

    results: list[dict] = []
    errors: list[dict] = []

    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            errors.append(
                {
                    "index": i,
                    "command": "",
                    "ok": False,
                    "error": f"Expected dict, got {type(op).__name__}",
                }
            )
            continue
        cmd = op.get("command", "")
        op_args = op.get("args", {})
        try:
            result = _execute_batch_op(store, cmd, op_args)
            results.append({"index": i, "command": cmd, "ok": True, "result": result})
        except Exception as e:
            errors.append({"index": i, "command": cmd, "ok": False, "error": str(e)})

    output = {"results": results, "errors": errors, "total": len(operations)}
    _output(output, json_mode=True)

    if errors:
        sys.exit(1)


def _validate_batch_date(value: str | None, field_name: str) -> None:
    """Validate a date in batch context. Raises ValueError on failure."""
    if not value:
        return
    if not _DATE_RE.match(value):
        msg = f"Invalid {field_name}: expected YYYY-MM-DD format, got '{value}'"
        raise ValueError(msg)
    try:
        datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        msg = f"Invalid {field_name}: '{value}' is not a valid date"
        raise ValueError(msg) from None


def _execute_batch_op(store: SprintStore, command: str, args: dict) -> dict:
    """Execute a single batch operation."""
    if not isinstance(args, dict):
        msg = f"Expected args to be dict, got {type(args).__name__}"
        raise TypeError(msg)
    if command == "sprint create":
        _validate_batch_date(args.get("start"), "start date")
        _validate_batch_date(args.get("end"), "end date")
        sprint = store.create_sprint(
            args["number"],
            goal=args.get("goal", ""),
            start_date=args.get("start"),
            end_date=args.get("end"),
        )
        return {"sprint": sprint["number"], "status": sprint["status"]}

    if command == "sprint update":
        _validate_batch_date(args.get("start"), "start date")
        _validate_batch_date(args.get("end"), "end date")
        fields: dict[str, str | None] = {}
        if "goal" in args:
            fields["goal"] = args["goal"]
        if "start" in args:
            fields["start_date"] = args["start"]
        if "end" in args:
            fields["end_date"] = args["end"]
        result = store.update_sprint(args["number"], **fields)
        if not result:
            msg = f"Sprint {args['number']} not found"
            raise ValueError(msg)
        return {"sprint": result["number"], "status": result["status"]}

    if command == "sprint start":
        _validate_batch_date(args.get("start"), "start date")
        start_date = args.get("start") or datetime.now(UTC).strftime("%Y-%m-%d")
        return store.start_sprint(args["number"], start_date=start_date)

    if command == "sprint close":
        issues = store.get_issue_numbers(args["number"])
        carry_over_to_raw = args.get("carry_over_to")
        # Normalize: <=0 and None both mean "no carry-over" (consistent with API)
        carry_over_to = (
            carry_over_to_raw
            if carry_over_to_raw is not None and carry_over_to_raw > 0
            else None
        )
        # Default carry_over_issues to all sprint issues when not explicitly provided
        carry_over_issues = (
            args.get("carry_over_issues", issues) if carry_over_to is not None else None
        )
        return store.close_sprint(
            args["number"],
            end_date=datetime.now(UTC).strftime("%Y-%m-%d"),
            total_issues=len(issues),
            total_points=0,
            issue_numbers=issues,
            carry_over_to=carry_over_to,
            carry_over_issues=carry_over_issues,
        )

    if command == "sprint cancel":
        return store.cancel_sprint(args["number"])

    if command == "issue add":
        added = []
        failed = []
        for num in args["issues"]:
            if store.add_issue(
                args["sprint"], num, source=args.get("source", "manual")
            ):
                added.append(num)
            else:
                failed.append(num)
        if failed:
            msg = f"Failed to add issues: {failed}"
            raise ValueError(msg)
        return {"sprint": args["sprint"], "added": added}

    if command == "issue remove":
        removed = []
        failed = []
        for num in args["issues"]:
            if store.remove_issue(args["sprint"], num):
                removed.append(num)
            else:
                failed.append(num)
        if failed:
            msg = f"Failed to remove issues: {failed}"
            raise ValueError(msg)
        return {"sprint": args["sprint"], "removed": removed}

    if command == "issue move":
        moved = []
        failed = []
        for num in args["issues"]:
            if store.move_issue(num, args["from_sprint"], args["to_sprint"]):
                moved.append(num)
            else:
                failed.append(num)
        if failed:
            msg = f"Failed to move issues: {failed}"
            raise ValueError(msg)
        return {
            "from_sprint": args["from_sprint"],
            "to_sprint": args["to_sprint"],
            "moved": moved,
        }

    msg = f"Unknown command: {command}"
    raise ValueError(msg)


# --- Parser ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sd-cli",
        description="Sprint-dash CLI for sprint management",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--db",
        help="SQLite database path (default: $SPRINT_DASH_DB or /data/sprint-dash.db)",
    )
    parser.add_argument("--owner", help="Repo owner (default: $GITEA_OWNER)")
    parser.add_argument("--repo", help="Repo name (default: $GITEA_REPO)")

    sub = parser.add_subparsers(dest="command", help="Command group")

    # --- sprint subcommands ---
    sprint_parser = sub.add_parser("sprint", help="Sprint management")
    sprint_sub = sprint_parser.add_subparsers(dest="sprint_command")

    # sprint list
    sp_list = sprint_sub.add_parser("list", help="List sprints")
    sp_list.add_argument(
        "--status", choices=["planned", "in_progress", "completed", "cancelled"]
    )
    sp_list.set_defaults(func=cmd_sprint_list)

    # sprint show
    sp_show = sprint_sub.add_parser("show", help="Show sprint details")
    sp_show.add_argument("number", type=int)
    sp_show.set_defaults(func=cmd_sprint_show)

    # sprint create
    sp_create = sprint_sub.add_parser("create", help="Create a sprint")
    sp_create.add_argument("number", type=int)
    sp_create.add_argument("--status", default="planned", choices=["planned"])
    sp_create.add_argument("--start", help="Start date (YYYY-MM-DD)")
    sp_create.add_argument("--end", help="End date (YYYY-MM-DD)")
    sp_create.add_argument("--goal", default="", help="Sprint goal")
    sp_create.set_defaults(func=cmd_sprint_create)

    # sprint update
    sp_update = sprint_sub.add_parser("update", help="Update a sprint")
    sp_update.add_argument("number", type=int)
    sp_update.add_argument("--start", help="Start date (YYYY-MM-DD)")
    sp_update.add_argument("--end", help="End date (YYYY-MM-DD)")
    sp_update.add_argument(
        "--goal", help="Sprint goal (use start/close/cancel for status)"
    )
    sp_update.set_defaults(func=cmd_sprint_update)

    # sprint start
    sp_start = sprint_sub.add_parser("start", help="Start a sprint")
    sp_start.add_argument("number", type=int)
    sp_start.add_argument("--start", help="Start date (default: today)")
    sp_start.set_defaults(func=cmd_sprint_start)

    # sprint close
    sp_close = sprint_sub.add_parser("close", help="Close a sprint")
    sp_close.add_argument("number", type=int)
    sp_close.add_argument(
        "--carry-over-to", type=int, help="Sprint number to carry open issues to"
    )
    sp_close.set_defaults(func=cmd_sprint_close)

    # sprint cancel
    sp_cancel = sprint_sub.add_parser("cancel", help="Cancel a sprint")
    sp_cancel.add_argument("number", type=int)
    sp_cancel.set_defaults(func=cmd_sprint_cancel)

    # sprint current
    sp_current = sprint_sub.add_parser("current", help="Show current sprint number")
    sp_current.set_defaults(func=cmd_sprint_current)

    # --- issue subcommands ---
    issue_parser = sub.add_parser("issue", help="Issue management within sprints")
    issue_sub = issue_parser.add_subparsers(dest="issue_command")

    # issue list
    is_list = issue_sub.add_parser("list", help="List issues in a sprint")
    is_list.add_argument("sprint_number", type=int)
    is_list.set_defaults(func=cmd_issue_list)

    # issue add
    is_add = issue_sub.add_parser("add", help="Add issues to a sprint")
    is_add.add_argument("sprint_number", type=int)
    is_add.add_argument("issues", type=int, nargs="+", help="Issue numbers")
    is_add.add_argument("--source", default="manual", choices=["manual", "rollover"])
    is_add.set_defaults(func=cmd_issue_add)

    # issue remove
    is_remove = issue_sub.add_parser("remove", help="Remove issues from a sprint")
    is_remove.add_argument("sprint_number", type=int)
    is_remove.add_argument("issues", type=int, nargs="+", help="Issue numbers")
    is_remove.set_defaults(func=cmd_issue_remove)

    # issue move
    is_move = issue_sub.add_parser("move", help="Move issues between sprints")
    is_move.add_argument("from_sprint", type=int, help="Source sprint number")
    is_move.add_argument("issues", type=int, nargs="+", help="Issue numbers")
    is_move.add_argument(
        "--to", type=int, required=True, dest="to_sprint", help="Target sprint number"
    )
    is_move.set_defaults(func=cmd_issue_move)

    # --- batch command ---
    batch_parser = sub.add_parser(
        "batch", help="Execute multiple operations from stdin JSON"
    )
    batch_parser.set_defaults(func=cmd_batch)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "sprint" and not getattr(args, "sprint_command", None):
        # No subcommand given, show sprint help
        # Re-parse to get sprint subparser
        parser.parse_args([args.command, "--help"])

    if args.command == "issue" and not getattr(args, "issue_command", None):
        parser.parse_args([args.command, "--help"])

    if hasattr(args, "func"):
        try:
            args.func(args)
        finally:
            conn = getattr(args, "_conn", None)
            if conn is not None:
                conn.close()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
