"""Write API endpoints for sprint management."""

from __future__ import annotations

import contextlib
import html
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .database import get_db
from .gitea import Sprint, get_client
from .sprint_store import SprintStore

if TYPE_CHECKING:
    from .gitea import GiteaClient

logger = logging.getLogger(__name__)

router = APIRouter()
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=templates_dir)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: str, field_name: str) -> str | None:
    """Validate a date string is strictly YYYY-MM-DD format. Returns error message or None."""
    if not value:
        return None
    if not _DATE_RE.match(value):
        return f"Invalid {field_name}: expected YYYY-MM-DD format"
    try:
        datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return f"Invalid {field_name}: not a valid date"
    return None


def _error_response(message: str, status_code: int = 400) -> HTMLResponse:
    """Return an error flash HTML response with escaped content."""
    safe = html.escape(str(message))
    return HTMLResponse(
        f'<div class="error-flash">{safe}</div>',
        status_code=status_code,
    )


def _get_store(owner: str, repo: str) -> SprintStore:
    return SprintStore(get_db(), owner, repo)


def _build_sprint(
    store: SprintStore, client: GiteaClient, number: int
) -> Sprint | None:
    row = store.get_sprint(number)
    if not row:
        return None
    issue_numbers = store.get_issue_numbers(number)
    issues = client.get_issues_by_numbers(issue_numbers)
    return Sprint(
        number=row["number"],
        issues=tuple(issues),
        lifecycle_state=row["status"],
    )


def _make_context(request: Request, owner: str, repo: str, **kwargs):
    def repo_url(path: str = "") -> str:
        return f"/{owner}/{repo}{path}"

    return {
        "request": request,
        "owner": owner,
        "repo": repo,
        "repo_url": repo_url,
        **kwargs,
    }


# --- Sprint CRUD ---


@router.get("/{owner}/{repo}/api/sprints/create-form", response_class=HTMLResponse)
async def sprint_create_form(request: Request, owner: str, repo: str):
    """Return the sprint creation form."""
    store = _get_store(owner, repo)
    sprints = store.list_sprints()
    next_number = max((s["number"] for s in sprints), default=0) + 1
    return templates.TemplateResponse(
        "partials/sprint_create_form.html",
        _make_context(request, owner, repo, next_number=next_number),
    )


@router.post("/{owner}/{repo}/api/sprints", response_class=HTMLResponse)
async def create_sprint(
    request: Request,
    owner: str,
    repo: str,
    number: int = Form(...),
    status: str = Form(default="planned"),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    goal: str = Form(default=""),
):
    """Create a new sprint."""
    for date_val, name in [(start_date, "start date"), (end_date, "end date")]:
        if err := _validate_date(date_val, name):
            return _error_response(err)

    store = _get_store(owner, repo)
    try:
        store.create_sprint(
            number,
            status=status,
            start_date=start_date or None,
            end_date=end_date or None,
            goal=goal,
        )
    except ValueError as e:
        return _error_response(str(e))
    except sqlite3.IntegrityError:
        return _error_response(f"Sprint {number} already exists")
    except Exception:
        logger.exception("Failed to create sprint %d", number)
        return _error_response("Failed to create sprint", status_code=500)

    # Return updated sprint list partial
    client = get_client(owner, repo)
    sprint_rows = store.list_sprints()
    sprints = []
    for row in sprint_rows:
        sprint = _build_sprint(store, client, row["number"])
        if sprint:
            sprints.append(sprint)

    return templates.TemplateResponse(
        "partials/sprint_list.html",
        _make_context(request, owner, repo, sprints=sprints),
    )


@router.get(
    "/{owner}/{repo}/api/sprints/{number}/edit-form", response_class=HTMLResponse
)
async def sprint_edit_form(request: Request, owner: str, repo: str, number: int):
    """Return the sprint edit form."""
    store = _get_store(owner, repo)
    sprint_row = store.get_sprint(number)
    if not sprint_row:
        return HTMLResponse("Sprint not found", status_code=404)
    return templates.TemplateResponse(
        "partials/sprint_edit_form.html",
        _make_context(request, owner, repo, sprint=sprint_row),
    )


@router.put("/{owner}/{repo}/api/sprints/{number}", response_class=HTMLResponse)
async def update_sprint(
    request: Request,
    owner: str,
    repo: str,
    number: int,
    status: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    goal: str = Form(default=""),
):
    """Update sprint fields.

    Non-empty date values are applied (dates can only be set, not cleared).
    Goal is always applied — an empty string clears it.
    Status transitions are routed to dedicated workflow methods.
    """
    for date_val, name in [(start_date, "start date"), (end_date, "end date")]:
        if err := _validate_date(date_val, name):
            return _error_response(err)

    store = _get_store(owner, repo)

    # Collect non-status field updates (dates/goal)
    field_updates: dict[str, str | None] = {}
    if start_date:
        field_updates["start_date"] = start_date
    if end_date:
        field_updates["end_date"] = end_date
    # Always include goal — empty string clears it. This endpoint is designed
    # for HTMX form submits which always send all fields. Non-form clients
    # should use the CLI instead which has explicit --goal flag handling.
    field_updates["goal"] = goal

    # Route lifecycle transitions to dedicated workflow methods
    old_row = store.get_sprint(number)
    if not old_row:
        return HTMLResponse("Sprint not found", status_code=404)

    # Reject updates to frozen sprints
    if old_row["status"] in ("completed", "cancelled"):
        return _error_response(
            f"Cannot update {old_row['status']} sprint", status_code=400
        )

    # Validate status value
    valid_statuses = {"", "planned", "in_progress", "cancelled", "completed"}
    if status not in valid_statuses:
        return _error_response(f"Invalid status: {status}")

    did_freeze = False
    try:
        if status == "in_progress" and old_row["status"] != "in_progress":
            store.start_sprint(
                number,
                start_date=start_date or datetime.now(UTC).strftime("%Y-%m-%d"),
            )
        elif status == "cancelled" and old_row["status"] != "cancelled":
            store.cancel_sprint(number)
            did_freeze = True
        elif status == "completed":
            return _error_response("Use the close sprint form to complete a sprint")
        elif status == "planned" and old_row["status"] != "planned":
            return _error_response(
                f"Cannot revert {old_row['status']} sprint to planned"
            )
        # else: status unchanged, empty, or same as current — no lifecycle action needed

        # Apply field updates (dates/goal) unless the transition just froze the sprint
        if field_updates and not did_freeze:
            result = store.update_sprint(number, **field_updates)
            if not result:
                return HTMLResponse("Sprint not found", status_code=404)
    except ValueError as e:
        return _error_response(str(e))
    except sqlite3.IntegrityError as e:
        return _error_response(f"Invalid data: {e}")

    client = get_client(owner, repo)

    # Return updated sprint detail partial
    sprint = _build_sprint(store, client, number)
    if not sprint:
        return HTMLResponse("Sprint not found", status_code=404)

    return templates.TemplateResponse(
        "partials/sprint_detail.html",
        _make_context(request, owner, repo, sprint=sprint, burndown=None),
    )


# --- Sprint Close ---


@router.get(
    "/{owner}/{repo}/api/sprints/{number}/close-form", response_class=HTMLResponse
)
async def close_sprint_form(request: Request, owner: str, repo: str, number: int):
    """Return the close sprint confirmation dialog."""
    store = _get_store(owner, repo)
    client = get_client(owner, repo)
    sprint = _build_sprint(store, client, number)
    if not sprint:
        return HTMLResponse("Sprint not found", status_code=404)

    # Find nearest future sprint for carry-over target
    sprints = store.list_sprints()
    next_sprint = None
    for s in sprints:
        if s["number"] > number and s["status"] in ("planned", "in_progress"):
            # list_sprints returns descending, so keep overwriting to get lowest
            next_sprint = s

    open_issues = [i for i in sprint.issues if i.state == "open"]

    return templates.TemplateResponse(
        "partials/close_sprint_confirm.html",
        _make_context(
            request,
            owner,
            repo,
            sprint=sprint,
            open_issues=open_issues,
            next_sprint=next_sprint,
        ),
    )


@router.post("/{owner}/{repo}/api/sprints/{number}/close", response_class=HTMLResponse)
async def close_sprint(
    request: Request,
    owner: str,
    repo: str,
    number: int,
    carry_over_to: int = Form(default=0),
):
    """Close a sprint with optional carry-over.

    Uses the atomic store.close_sprint() method so snapshot + carry-over +
    status update either all succeed or all roll back.
    """
    store = _get_store(owner, repo)
    client = get_client(owner, repo)

    sprint = _build_sprint(store, client, number)
    if not sprint:
        return HTMLResponse("Sprint not found", status_code=404)

    open_numbers = [i.number for i in sprint.issues if i.state == "open"]

    try:
        store.close_sprint(
            number,
            end_date=datetime.now(UTC).strftime("%Y-%m-%d"),
            total_issues=sprint.total,
            total_points=sprint.total_points,
            issue_numbers=[i.number for i in sprint.issues],
            carry_over_to=carry_over_to if carry_over_to > 0 else None,
            carry_over_issues=open_numbers if carry_over_to > 0 else None,
        )
    except ValueError as e:
        return _error_response(str(e))
    except Exception:
        logger.exception("Failed to close sprint %d", number)
        return _error_response("Failed to close sprint", status_code=500)

    # Return updated sprint list
    sprint_rows = store.list_sprints()
    sprints = []
    for row in sprint_rows:
        s = _build_sprint(store, client, row["number"])
        if s:
            sprints.append(s)

    return templates.TemplateResponse(
        "partials/sprint_list.html",
        _make_context(request, owner, repo, sprints=sprints),
    )


# --- Issue Management ---


@router.post(
    "/{owner}/{repo}/api/sprints/{number}/issues/{issue}", response_class=HTMLResponse
)
async def add_issue_to_sprint(
    request: Request,
    owner: str,
    repo: str,
    number: int,
    issue: int,
    from_sprint: int | None = Form(default=None),
):
    """Add an issue to a sprint. Optionally remove from source sprint."""
    store = _get_store(owner, repo)
    client = get_client(owner, repo)

    if from_sprint is not None and from_sprint > 0:
        # Validate target sprint lifecycle before attempting move
        target = store.get_sprint(number)
        if not target:
            return HTMLResponse("Target sprint not found", status_code=404)
        if target["status"] in ("completed", "cancelled"):
            return _error_response(f"Cannot move to {target['status']} sprint")
        # Atomic move between sprints (savepoint-based in store)
        if not store.move_issue(issue, from_sprint, number):
            return HTMLResponse("Issue not in source sprint", status_code=404)
    else:
        sprint_row = store.get_sprint(number)
        if not sprint_row:
            return HTMLResponse("Sprint not found", status_code=404)
        if sprint_row["status"] in ("completed", "cancelled"):
            return _error_response(f"Cannot add to {sprint_row['status']} sprint")
        if not store.add_issue(number, issue):
            return HTMLResponse("Failed to add issue", status_code=400)

    # Return updated board content
    return await _board_partial(request, owner, repo, store, client)


@router.delete(
    "/{owner}/{repo}/api/sprints/{number}/issues/{issue}", response_class=HTMLResponse
)
async def remove_issue_from_sprint(
    request: Request,
    owner: str,
    repo: str,
    number: int,
    issue: int,
):
    """Remove an issue from a sprint."""
    store = _get_store(owner, repo)
    client = get_client(owner, repo)

    sprint_row = store.get_sprint(number)
    if not sprint_row:
        return HTMLResponse("Sprint not found", status_code=404)
    if sprint_row["status"] in ("completed", "cancelled"):
        return _error_response(f"Cannot remove from {sprint_row['status']} sprint")
    if not store.remove_issue(number, issue):
        return HTMLResponse("Issue not found in sprint", status_code=404)

    return await _board_partial(request, owner, repo, store, client)


@router.post(
    "/{owner}/{repo}/api/sprints/{from_sprint}/carry-over/{to_sprint}",
    response_class=HTMLResponse,
)
async def carry_over(
    request: Request,
    owner: str,
    repo: str,
    from_sprint: int,
    to_sprint: int,
):
    """Carry over all open issues from one sprint to another."""
    store = _get_store(owner, repo)
    client = get_client(owner, repo)

    if from_sprint == to_sprint:
        return _error_response("Cannot carry over to the same sprint")

    sprint = _build_sprint(store, client, from_sprint)
    if not sprint:
        return HTMLResponse("Source sprint not found", status_code=404)

    target = store.get_sprint(to_sprint)
    if not target:
        return _error_response(f"Target sprint {to_sprint} not found", status_code=404)
    if target["status"] in ("completed", "cancelled"):
        return _error_response(f"Cannot carry over to {target['status']} sprint")

    open_numbers = [i.number for i in sprint.issues if i.state == "open"]
    try:
        store.carry_over(from_sprint, to_sprint, open_numbers)
    except ValueError as e:
        return _error_response(str(e))
    except Exception:
        logger.exception("Carry-over error %d -> %d", from_sprint, to_sprint)
        return _error_response("Carry-over failed", status_code=500)

    return await _board_partial(request, owner, repo, store, client)


async def _board_partial(
    request: Request, owner: str, repo: str, store: SprintStore, client: GiteaClient
) -> HTMLResponse:
    """Return updated board content partial after a write operation."""
    from .main import _build_board_data, _sort_board_issues

    board_data = _build_board_data(store, client)

    # Build minimal board context
    ready_backlog = [i for i in board_data.backlog if i.is_ready]
    ready_backlog = _sort_board_issues(ready_backlog)
    ready_backlog_board = client.to_board_issues(ready_backlog)

    backlog_blocked_count = sum(
        1 for bi in ready_backlog_board if bi.state == "open" and bi.is_blocked
    )

    sprint_by_num = {s.number: s for s in board_data.sprints}
    all_sprint_nums = sorted(sprint_by_num.keys())
    current_sprint_num = board_data.current_sprint_num
    center_sprint_num = current_sprint_num

    min_sprint = min(all_sprint_nums) if all_sprint_nums else 0
    max_sprint = max(all_sprint_nums) if all_sprint_nums else 0
    can_go_back = center_sprint_num and center_sprint_num > min_sprint
    can_go_forward = center_sprint_num and center_sprint_num < max_sprint

    sprint_columns = []
    if center_sprint_num:
        for offset in [-1, 0, 1, 2]:
            sprint_num = center_sprint_num + offset
            sprint = sprint_by_num.get(sprint_num)
            if sprint:
                sprint_columns.append(sprint)

    sorted_sprint_columns = []
    for sprint in sprint_columns:
        issues = list(sprint.issues)
        sorted_issues = _sort_board_issues(issues)
        board_issues = client.to_board_issues(sorted_issues)
        blocked_count = sum(
            1 for bi in board_issues if bi.state == "open" and bi.is_blocked
        )
        polish_count = sum(
            1 for bi in board_issues if bi.state == "open" and bi.needs_polish
        )
        sorted_sprint_columns.append(
            (sprint, board_issues, blocked_count, polish_count)
        )

    all_issues = board_data.backlog + [i for s in board_data.sprints for i in s.issues]
    all_types = sorted({i.issue_type for i in all_issues if i.issue_type != "unknown"})
    all_epics = sorted({i.epic for i in all_issues if i.epic})

    # Fetch CI health
    from .woodpecker import get_woodpecker_client

    ci_health = None
    nightly = None
    wp = get_woodpecker_client()
    if wp:
        with contextlib.suppress(Exception):
            ci_health = wp.get_ci_health(owner, repo)
        with contextlib.suppress(Exception):
            nightly = wp.get_nightly_summary(owner, repo)

    context = _make_context(
        request,
        owner,
        repo,
        ready_backlog=ready_backlog_board,
        backlog_blocked_count=backlog_blocked_count,
        sprint_columns=sorted_sprint_columns,
        current_sprint_num=current_sprint_num,
        center_sprint_num=center_sprint_num,
        can_go_back=can_go_back,
        can_go_forward=can_go_forward,
        columns=5,
        show_closed=False,
        type_filter="",
        epic_filter="",
        group_by_epic=False,
        all_types=all_types,
        all_epics=all_epics,
        ci_health=ci_health,
        nightly=nightly,
    )

    return templates.TemplateResponse("partials/board_content.html", context)
