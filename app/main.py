"""Sprint Dashboard - FastAPI application."""

import contextlib
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .gitea import BacklogStats, CIHealth, ConfigError, GiteaError, get_client

app = FastAPI(title="Sprint Dashboard")

# Templates
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=templates_dir)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard home - shows current sprint and summary."""
    try:
        client = get_client()
        sprints = client.get_sprints()
        current = sprints[0] if sprints else None
        ready_queue = client.get_ready_queue()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Fetch CI health (don't let failures break the page)
    ci_health: CIHealth | None = None
    with contextlib.suppress(Exception):
        ci_health = client.get_ci_health()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "current_sprint": current,
            "sprints": sprints[:5],
            "ready_count": len(ready_queue),
            "ci_health": ci_health,
        },
    )


def _sort_board_issues(issues: list, show_closed: bool = False) -> list:
    """Sort issues for board display: open first, then priority, size, age."""
    # Filter closed if needed
    if not show_closed:
        issues = [i for i in issues if i.state == "open"]

    # Sort: open first, then P1→P3→None, then L→S→None, then oldest first
    def sort_key(issue):
        state_order = 0 if issue.state == "open" else 1
        priority_order = issue.priority if issue.priority else 99
        size_order = {"XL": 0, "L": 1, "M": 2, "S": 3}.get(issue.size, 99)
        return (state_order, priority_order, size_order, issue.created_at)

    return sorted(issues, key=sort_key)


@app.get("/board", response_class=HTMLResponse)
async def board(
    request: Request,
    center: int | None = Query(default=None),
    show_closed: bool = Query(default=False),
    type_filter: str = Query(default=""),
    epic_filter: str = Query(default=""),
    group_by_epic: bool = Query(default=False),
):
    """Sprint board view - 5 columns: Backlog, Previous, Current, Next, Next+1."""
    try:
        client = get_client()
        board_data = client.get_board_data()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Fetch CI health (don't let failures break the page)
    ci_health: CIHealth | None = None
    with contextlib.suppress(Exception):
        ci_health = client.get_ci_health()

    # Apply filters to backlog
    backlog = board_data.backlog
    if type_filter:
        backlog = [i for i in backlog if i.issue_type == type_filter]
    if epic_filter:
        backlog = [i for i in backlog if i.epic == epic_filter]

    # Get ready-only backlog for the board
    ready_backlog = [i for i in backlog if i.is_ready]
    ready_backlog = _sort_board_issues(ready_backlog, show_closed)
    # Convert to BoardIssues with dependency info
    ready_backlog_board = client.to_board_issues(ready_backlog)

    # Count blocked issues in backlog
    backlog_blocked_count = sum(1 for bi in ready_backlog_board if bi.is_blocked)

    # Build sprint lookup by number
    sprint_by_num = {s.number: s for s in board_data.sprints}
    all_sprint_nums = sorted(sprint_by_num.keys())

    # Determine center sprint (default to current)
    current_sprint_num = board_data.current_sprint_num
    center_sprint_num = center if center is not None else current_sprint_num

    # Calculate navigation bounds
    min_sprint = min(all_sprint_nums) if all_sprint_nums else 0
    max_sprint = max(all_sprint_nums) if all_sprint_nums else 0
    can_go_back = center_sprint_num and center_sprint_num > min_sprint
    can_go_forward = center_sprint_num and center_sprint_num < max_sprint

    # Build 4 sprint columns: center-1, center, center+1, center+2
    sprint_columns = []
    if center_sprint_num:
        for offset in [-1, 0, 1, 2]:
            sprint_num = center_sprint_num + offset
            sprint = sprint_by_num.get(sprint_num)
            if sprint:
                sprint_columns.append(sprint)

    # Sort issues within each sprint and convert to BoardIssues
    sorted_sprint_columns = []
    for sprint in sprint_columns:
        issues = list(sprint.issues)
        if type_filter:
            issues = [i for i in issues if i.issue_type == type_filter]
        if epic_filter:
            issues = [i for i in issues if i.epic == epic_filter]
        sorted_issues = _sort_board_issues(issues, show_closed)
        board_issues = client.to_board_issues(sorted_issues)
        blocked_count = sum(1 for bi in board_issues if bi.is_blocked)
        sorted_sprint_columns.append((sprint, board_issues, blocked_count))

    # Get filter options
    all_issues = board_data.backlog + [i for s in board_data.sprints for i in s.issues]
    all_types = sorted({i.issue_type for i in all_issues if i.issue_type != "unknown"})
    all_epics = sorted({i.epic for i in all_issues if i.epic})

    context = {
        "request": request,
        "ready_backlog": ready_backlog_board,
        "backlog_blocked_count": backlog_blocked_count,
        "sprint_columns": sorted_sprint_columns,
        "current_sprint_num": current_sprint_num,
        "center_sprint_num": center_sprint_num,
        "can_go_back": can_go_back,
        "can_go_forward": can_go_forward,
        "columns": 5,  # Fixed: backlog + 4 sprints
        "show_closed": show_closed,
        "type_filter": type_filter,
        "epic_filter": epic_filter,
        "group_by_epic": group_by_epic,
        "all_types": all_types,
        "all_epics": all_epics,
        "ci_health": ci_health,
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/board_content.html", context)

    return templates.TemplateResponse("board.html", context)


@app.get("/board/column/{column_type}", response_class=HTMLResponse)
async def board_column(
    request: Request,
    column_type: str,
    sprint_num: int = Query(default=0),
    show_closed: bool = Query(default=False),
    type_filter: str = Query(default=""),
    epic_filter: str = Query(default=""),
):
    """Lazy-load a single board column."""
    try:
        client = get_client()
        board_data = client.get_board_data()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    issues = []
    column_title = ""
    column_stats = ""

    if column_type == "backlog":
        issues = [i for i in board_data.backlog if i.is_ready]
        column_title = "Backlog (Ready)"
        total_pts = sum(i.points for i in issues)
        column_stats = f"{len(issues)} issues (~{total_pts} pts)"
    elif column_type == "sprint" and sprint_num:
        sprint = board_data.get_sprint(sprint_num)
        if sprint:
            issues = list(sprint.issues)
            is_current = sprint_num == board_data.current_sprint_num
            column_title = f"Sprint {sprint_num}" + (" (current)" if is_current else "")
            column_stats = f"{sprint.closed_count}/{sprint.total} done ({sprint.progress_pct}%)"

    # Apply filters
    if type_filter:
        issues = [i for i in issues if i.issue_type == type_filter]
    if epic_filter:
        issues = [i for i in issues if i.epic == epic_filter]

    issues = _sort_board_issues(issues, show_closed)

    return templates.TemplateResponse(
        "partials/board_column.html",
        {
            "request": request,
            "issues": issues,
            "column_title": column_title,
            "column_stats": column_stats,
            "show_closed": show_closed,
        },
    )


@app.get("/sprints", response_class=HTMLResponse)
async def sprints_list(request: Request):
    """List all sprints."""
    try:
        client = get_client()
        sprints = client.get_sprints()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Check if HTMX request (partial update)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/sprint_list.html",
            {"request": request, "sprints": sprints},
        )

    return templates.TemplateResponse(
        "sprints.html",
        {"request": request, "sprints": sprints},
    )


@app.get("/sprints/{number}", response_class=HTMLResponse)
async def sprint_detail(request: Request, number: int):
    """Sprint detail view."""
    try:
        client = get_client()
        sprint = client.get_sprint(number)
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/sprint_detail.html",
            {"request": request, "sprint": sprint},
        )

    return templates.TemplateResponse(
        "sprint_detail.html",
        {"request": request, "sprint": sprint},
    )


def _sort_issues(
    issues: list, sort: str, reverse: bool = False
) -> list:
    """Sort issues by the given field."""
    if sort == "priority":
        # P1 first (lowest number), issues without priority last
        return sorted(issues, key=lambda i: (i.priority or 99, i.number), reverse=reverse)
    elif sort == "size":
        # L first (most points), issues without size last
        size_order = {"XL": 0, "L": 1, "M": 2, "S": 3, None: 99}
        return sorted(
            issues, key=lambda i: (size_order.get(i.size, 99), i.number), reverse=reverse
        )
    elif sort == "age":
        # Oldest first
        return sorted(issues, key=lambda i: i.created_at, reverse=reverse)
    elif sort == "updated":
        # Most recently updated first
        return sorted(issues, key=lambda i: i.updated_at, reverse=not reverse)
    elif sort == "number":
        return sorted(issues, key=lambda i: i.number, reverse=reverse)
    return issues


@app.get("/backlog", response_class=HTMLResponse)
async def backlog(
    request: Request,
    sort: str = Query(default="priority"),
    epic: str = Query(default=""),
    type: str = Query(default=""),
    size: str = Query(default=""),
    ready_only: bool = Query(default=False),
    view: str = Query(default="list"),  # list or epic
):
    """Backlog view with sorting and filtering."""
    try:
        client = get_client()
        all_backlog = client.get_backlog()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Filter issues
    issues = all_backlog
    if ready_only:
        issues = [i for i in issues if i.is_ready]
    if epic:
        issues = [i for i in issues if i.epic == epic]
    if type:
        issues = [i for i in issues if i.issue_type == type]
    if size:
        issues = [i for i in issues if i.size == size]

    # Sort issues
    issues = _sort_issues(issues, sort)

    # Compute stats
    ready_issues = [i for i in all_backlog if i.is_ready]
    stats = BacklogStats(issues=issues)
    ready_stats = BacklogStats(issues=ready_issues)

    # Get unique values for filter dropdowns
    all_epics = sorted({i.epic for i in all_backlog if i.epic})
    all_types = sorted({i.issue_type for i in all_backlog if i.issue_type != "unknown"})

    context = {
        "request": request,
        "issues": issues,
        "stats": stats,
        "ready_stats": ready_stats,
        "ready_issues": ready_issues,
        "all_epics": all_epics,
        "all_types": all_types,
        "sort": sort,
        "epic_filter": epic,
        "type_filter": type,
        "size_filter": size,
        "ready_only": ready_only,
        "view": view,
    }

    if request.headers.get("HX-Request"):
        # Return just the issue list for HTMX updates
        return templates.TemplateResponse("partials/backlog_list.html", context)

    return templates.TemplateResponse("backlog.html", context)


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = Query(default="")):
    """Search issues."""
    try:
        client = get_client()
        issues = client.search_issues(q) if q else []
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/issue_list.html",
            {"request": request, "issues": issues, "title": f"Search: {q}"},
        )

    return templates.TemplateResponse(
        "search.html",
        {"request": request, "query": q, "issues": issues},
    )


@app.get("/issues/{number}", response_class=HTMLResponse)
async def issue_detail(request: Request, number: int):
    """Issue detail view with description, comments, and dependencies."""
    try:
        client = get_client()
        issue = client.get_issue(number)
        comments = client.get_issue_comments(number)
        depends_on = client.get_issue_dependencies(number)
        blocks = client.get_issue_blocks(number)
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    context = {
        "request": request,
        "issue": issue,
        "comments": comments,
        "depends_on": depends_on,
        "blocks": blocks,
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/issue_detail.html", context)

    return templates.TemplateResponse("issue_detail.html", context)


@app.get("/issues", response_class=HTMLResponse)
async def issues_filtered(
    request: Request,
    q: str = Query(default=""),
    label: str = Query(default=""),
    state: str = Query(default="all"),
):
    """Filter issues with HTMX support."""
    try:
        client = get_client()
        issues = client._get_issues(state=state, labels=label if label else None)
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Client-side filter by query
    if q:
        q_lower = q.lower()
        issues = [i for i in issues if q_lower in i.title.lower()]

    return templates.TemplateResponse(
        "partials/issue_list.html",
        {"request": request, "issues": issues},
    )
