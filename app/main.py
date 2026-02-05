"""Sprint Dashboard - FastAPI application."""

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .gitea import BacklogStats, ConfigError, GiteaError, get_client

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

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "current_sprint": current,
            "sprints": sprints[:5],
            "ready_count": len(ready_queue),
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
