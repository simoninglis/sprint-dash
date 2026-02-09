"""Sprint Dashboard - FastAPI application."""

import contextlib
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .gitea import (
    BacklogStats,
    BurndownData,
    CIHealth,
    ConfigError,
    GiteaError,
    NightlySummary,
    close_all_clients,
    get_base_client,
    get_client,
)
from .woodpecker import close_woodpecker_client, get_woodpecker_client

app = FastAPI(title="Sprint Dashboard")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up cached clients on shutdown."""
    close_all_clients()
    close_woodpecker_client()


# Templates
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=templates_dir)


def make_context(
    request: Request, owner: str, repo: str, **kwargs: Any
) -> dict[str, Any]:
    """Build template context with repo info and URL helper.

    Args:
        request: FastAPI request object.
        owner: Repository owner.
        repo: Repository name.
        **kwargs: Additional context variables.

    Returns:
        Context dict with request, owner, repo, repo_url helper, and kwargs.
    """

    def repo_url(path: str = "") -> str:
        """Generate URL with repo prefix."""
        return f"/{owner}/{repo}{path}"

    return {
        "request": request,
        "owner": owner,
        "repo": repo,
        "repo_url": repo_url,
        **kwargs,
    }


@app.get("/", response_class=HTMLResponse)
async def repo_picker(request: Request):
    """Repository picker - select which repo to view."""
    try:
        client = get_base_client()
        repos = client.get_user_repos()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Group repos by owner
    repos_by_owner: dict[str, list[dict[str, str]]] = {}
    for r in repos:
        repos_by_owner.setdefault(r["owner"], []).append(r)

    return templates.TemplateResponse(
        "repo_picker.html",
        {"request": request, "repos_by_owner": repos_by_owner},
    )


@app.get("/{owner}/{repo}", response_class=HTMLResponse)
async def home(request: Request, owner: str, repo: str):
    """Dashboard home - shows current sprint and summary."""
    try:
        client = get_client(owner, repo)
        sprints = client.get_sprints()
        current = sprints[0] if sprints else None
        ready_queue = client.get_ready_queue()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Fetch CI health from Woodpecker (don't let failures break the page)
    ci_health: CIHealth | None = None
    nightly: NightlySummary | None = None
    wp = get_woodpecker_client()
    if wp:
        with contextlib.suppress(Exception):
            ci_health = wp.get_ci_health(owner, repo)
        with contextlib.suppress(Exception):
            nightly = wp.get_nightly_summary(owner, repo)

    context = make_context(
        request,
        owner,
        repo,
        current_sprint=current,
        sprints=sprints[:5],
        ready_count=len(ready_queue),
        ci_health=ci_health,
        nightly=nightly,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/home_content.html", context)

    return templates.TemplateResponse("home.html", context)


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


@app.get("/{owner}/{repo}/board", response_class=HTMLResponse)
async def board(
    request: Request,
    owner: str,
    repo: str,
    center: int | None = Query(default=None),
    show_closed: bool = Query(default=False),
    type_filter: str = Query(default=""),
    epic_filter: str = Query(default=""),
    group_by_epic: bool = Query(default=False),
):
    """Sprint board view - 5 columns: Backlog, Previous, Current, Next, Next+1."""
    try:
        client = get_client(owner, repo)
        board_data = client.get_board_data()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Fetch CI health from Woodpecker (don't let failures break the page)
    ci_health: CIHealth | None = None
    nightly: NightlySummary | None = None
    wp = get_woodpecker_client()
    if wp:
        with contextlib.suppress(Exception):
            ci_health = wp.get_ci_health(owner, repo)
        with contextlib.suppress(Exception):
            nightly = wp.get_nightly_summary(owner, repo)

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

    # Count blocked issues in backlog (only open issues)
    backlog_blocked_count = sum(
        1 for bi in ready_backlog_board if bi.state == "open" and bi.is_blocked
    )

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
        # Only count blocked/polish for open issues (closed issues are done)
        blocked_count = sum(
            1 for bi in board_issues if bi.state == "open" and bi.is_blocked
        )
        polish_count = sum(
            1 for bi in board_issues if bi.state == "open" and bi.needs_polish
        )
        sorted_sprint_columns.append(
            (sprint, board_issues, blocked_count, polish_count)
        )

    # Get filter options
    all_issues = board_data.backlog + [i for s in board_data.sprints for i in s.issues]
    all_types = sorted({i.issue_type for i in all_issues if i.issue_type != "unknown"})
    all_epics = sorted({i.epic for i in all_issues if i.epic})

    context = make_context(
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
        columns=5,  # Fixed: backlog + 4 sprints
        show_closed=show_closed,
        type_filter=type_filter,
        epic_filter=epic_filter,
        group_by_epic=group_by_epic,
        all_types=all_types,
        all_epics=all_epics,
        ci_health=ci_health,
        nightly=nightly,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/board_content.html", context)

    return templates.TemplateResponse("board.html", context)


@app.get("/{owner}/{repo}/board/column/{column_type}", response_class=HTMLResponse)
async def board_column(
    request: Request,
    owner: str,
    repo: str,
    column_type: str,
    sprint_num: int = Query(default=0),
    show_closed: bool = Query(default=False),
    type_filter: str = Query(default=""),
    epic_filter: str = Query(default=""),
):
    """Lazy-load a single board column."""
    try:
        client = get_client(owner, repo)
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
            column_stats = (
                f"{sprint.closed_count}/{sprint.total} done ({sprint.progress_pct}%)"
            )

    # Apply filters
    if type_filter:
        issues = [i for i in issues if i.issue_type == type_filter]
    if epic_filter:
        issues = [i for i in issues if i.epic == epic_filter]

    issues = _sort_board_issues(issues, show_closed)

    # Convert to BoardIssues with dependency info (consistent with full board view)
    board_issues = client.to_board_issues(issues)
    # Only count blocked/polish for open issues (closed issues are done)
    blocked_count = sum(
        1 for bi in board_issues if bi.state == "open" and bi.is_blocked
    )
    polish_count = sum(
        1 for bi in board_issues if bi.state == "open" and bi.needs_polish
    )

    # Build enhanced column_stats with polish count (consistent with main board)
    if polish_count > 0:
        column_stats = column_stats.replace(" done", f" done ({polish_count} polish)")

    return templates.TemplateResponse(
        "partials/board_column.html",
        make_context(
            request,
            owner,
            repo,
            issues=board_issues,
            column_title=column_title,
            column_stats=column_stats,
            show_closed=show_closed,
            blocked_count=blocked_count,
            polish_count=polish_count,
        ),
    )


@app.get("/{owner}/{repo}/sprints", response_class=HTMLResponse)
async def sprints_list(request: Request, owner: str, repo: str):
    """List all sprints."""
    try:
        client = get_client(owner, repo)
        sprints = client.get_sprints()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    context = make_context(request, owner, repo, sprints=sprints)

    # Check if HTMX request (partial update)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/sprint_list.html", context)

    return templates.TemplateResponse("sprints.html", context)


@app.get("/{owner}/{repo}/sprints/{number}", response_class=HTMLResponse)
async def sprint_detail(request: Request, owner: str, repo: str, number: int):
    """Sprint detail view."""
    try:
        client = get_client(owner, repo)
        sprint = client.get_sprint(number)
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Fetch burndown data (suppressed errors)
    burndown: BurndownData | None = None
    with contextlib.suppress(Exception):
        burndown = client.get_burndown_data(number)

    context = make_context(request, owner, repo, sprint=sprint, burndown=burndown)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/sprint_detail.html", context)

    return templates.TemplateResponse("sprint_detail.html", context)


def _sort_issues(issues: list, sort: str, reverse: bool = False) -> list:
    """Sort issues by the given field."""
    if sort == "priority":
        # P1 first (lowest number), issues without priority last
        return sorted(
            issues, key=lambda i: (i.priority or 99, i.number), reverse=reverse
        )
    elif sort == "size":
        # L first (most points), issues without size last
        size_order = {"XL": 0, "L": 1, "M": 2, "S": 3, None: 99}
        return sorted(
            issues,
            key=lambda i: (size_order.get(i.size, 99), i.number),
            reverse=reverse,
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


@app.get("/{owner}/{repo}/backlog", response_class=HTMLResponse)
async def backlog(
    request: Request,
    owner: str,
    repo: str,
    sort: str = Query(default="priority"),
    epic: str = Query(default=""),
    type: str = Query(default=""),
    size: str = Query(default=""),
    ready_only: bool = Query(default=False),
    view: str = Query(default="list"),  # list or epic
):
    """Backlog view with sorting and filtering."""
    try:
        client = get_client(owner, repo)
        all_backlog = client.get_backlog()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    # Exclude epic tracking issues (they live on the epics screen)
    all_backlog = [i for i in all_backlog if not i.is_epic_tracking]

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

    context = make_context(
        request,
        owner,
        repo,
        issues=issues,
        stats=stats,
        ready_stats=ready_stats,
        ready_issues=ready_issues,
        all_epics=all_epics,
        all_types=all_types,
        sort=sort,
        epic_filter=epic,
        type_filter=type,
        size_filter=size,
        ready_only=ready_only,
        view=view,
    )

    if request.headers.get("HX-Request"):
        # Return just the issue list for HTMX updates
        return templates.TemplateResponse("partials/backlog_list.html", context)

    return templates.TemplateResponse("backlog.html", context)


@app.get("/{owner}/{repo}/epics", response_class=HTMLResponse)
async def epics(request: Request, owner: str, repo: str):
    """Epic progress view - shows all epics with progress bars and sprint breakdowns."""
    try:
        client = get_client(owner, repo)
        epic_summaries = client.get_epic_summaries()
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    context = make_context(request, owner, repo, epics=epic_summaries)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/epics_content.html", context)

    return templates.TemplateResponse("epics.html", context)


@app.get("/{owner}/{repo}/search", response_class=HTMLResponse)
async def search(request: Request, owner: str, repo: str, q: str = Query(default="")):
    """Search issues."""
    try:
        client = get_client(owner, repo)
        issues = client.search_issues(q) if q else []
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    context = make_context(request, owner, repo, query=q, issues=issues)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/issue_list.html",
            make_context(request, owner, repo, issues=issues, title=f"Search: {q}"),
        )

    return templates.TemplateResponse("search.html", context)


@app.get("/{owner}/{repo}/issues/{number}", response_class=HTMLResponse)
async def issue_detail(request: Request, owner: str, repo: str, number: int):
    """Issue detail view with description, comments, and dependencies."""
    try:
        client = get_client(owner, repo)
        issue = client.get_issue(number)
        comments = client.get_issue_comments(number)
        depends_on = client.get_issue_dependencies(number)
        blocks = client.get_issue_blocks(number)
    except (GiteaError, ConfigError) as e:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "error": str(e)}
        )

    context = make_context(
        request,
        owner,
        repo,
        issue=issue,
        comments=comments,
        depends_on=depends_on,
        blocks=blocks,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/issue_detail.html", context)

    return templates.TemplateResponse("issue_detail.html", context)


@app.get("/{owner}/{repo}/issues", response_class=HTMLResponse)
async def issues_filtered(
    request: Request,
    owner: str,
    repo: str,
    q: str = Query(default=""),
    label: str = Query(default=""),
    state: str = Query(default="all"),
):
    """Filter issues with HTMX support."""
    try:
        client = get_client(owner, repo)
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
        make_context(request, owner, repo, issues=issues),
    )


# --- Backwards compatibility redirects ---


@app.get("/board", response_class=RedirectResponse)
async def board_redirect():
    """Redirect old /board to new path or picker."""
    owner = os.getenv("GITEA_OWNER", "")
    repo = os.getenv("GITEA_REPO", "")
    if owner and repo:
        return RedirectResponse(f"/{owner}/{repo}/board", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/sprints", response_class=RedirectResponse)
async def sprints_redirect():
    """Redirect old /sprints to new path or picker."""
    owner = os.getenv("GITEA_OWNER", "")
    repo = os.getenv("GITEA_REPO", "")
    if owner and repo:
        return RedirectResponse(f"/{owner}/{repo}/sprints", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/backlog", response_class=RedirectResponse)
async def backlog_redirect():
    """Redirect old /backlog to new path or picker."""
    owner = os.getenv("GITEA_OWNER", "")
    repo = os.getenv("GITEA_REPO", "")
    if owner and repo:
        return RedirectResponse(f"/{owner}/{repo}/backlog", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/search", response_class=RedirectResponse)
async def search_redirect():
    """Redirect old /search to new path or picker."""
    owner = os.getenv("GITEA_OWNER", "")
    repo = os.getenv("GITEA_REPO", "")
    if owner and repo:
        return RedirectResponse(f"/{owner}/{repo}/search", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/issues", response_class=RedirectResponse)
async def issues_redirect():
    """Redirect old /issues to new path or picker."""
    owner = os.getenv("GITEA_OWNER", "")
    repo = os.getenv("GITEA_REPO", "")
    if owner and repo:
        return RedirectResponse(f"/{owner}/{repo}/issues", status_code=302)
    return RedirectResponse("/", status_code=302)
