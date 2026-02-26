# Sprint Dashboard - Design Document

## Overview

A lightweight read-only dashboard for visualizing sprint and backlog data from Gitea. Runs alongside Gitea on a headless VM, accessible via browser during planning sessions.

## Problem

- Sprint tracking via `sprint/N` labels in Gitea lacks visualization
- No sprint-centric views (progress, backlog, ready queue)
- Planning sessions require switching between terminal (teax) and manual queries
- Need visual overview while discussing with AI assistant

## Solution

FastAPI + HTMX dashboard that:
1. Pulls data from Gitea API
2. Provides sprint/backlog visualization
3. Supports filtering and search
4. Read-only (no writes to Gitea)

## Architecture

```
┌─────────────────────────────────────────────┐
│  headless VM                                │
│  ┌─────────┐    ┌─────────────┐             │
│  │  Gitea  │◄───│ sprint-dash │◄──► Browser │
│  └─────────┘    └─────────────┘    (WSL)    │
│      :3000          :8080                   │
└─────────────────────────────────────────────┘
```

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Backend | FastAPI | Already familiar, async-ready |
| Frontend | HTMX + Jinja2 | No build step, server-rendered |
| Styling | Inline CSS | Minimal, dark theme |
| Data | Gitea API | No local storage (MVP) |

## MVP Scope (~500 LOC)

### Pages

1. **Home** (`/`)
   - Current sprint summary (open/closed/progress)
   - Ready queue count
   - Recent sprints list

2. **Sprint Detail** (`/sprints/{n}`)
   - Progress stats
   - Issue list with state/type badges

3. **Sprints List** (`/sprints`)
   - All sprints with progress bars

4. **Backlog** (`/backlog`)
   - Ready queue (has `ready` label, no sprint)
   - Unscheduled (no sprint label)

5. **Search** (`/search`)
   - Live search with HTMX
   - Filter by label (bug, feature, tech-debt)
   - Filter by state (open, closed, all)

### Data Model

```python
@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str  # open, closed
    labels: tuple[str, ...]
    created_at: str
    updated_at: str
    closed_at: str | None
    body: str  # For parsing size/effort from issue body

@dataclass(frozen=True)
class Sprint:
    number: int
    issues: tuple[Issue, ...]
    lifecycle_state: str  # "in_progress", "planned", "completed", "unknown"
    # Derived: open_count, closed_count, progress_pct, lifecycle_indicator

@dataclass(frozen=True)
class Milestone:
    id: int
    title: str           # "Sprint 45"
    state: str           # "open" or "closed"
    open_issues: int
    closed_issues: int
    # Derived: sprint_number, lifecycle_state

@dataclass(frozen=True)
class CIHealth:
    sha: str             # short SHA of main branch HEAD
    state: str           # "success", "failure", "running", "pending", "unknown"
    workflows: tuple[tuple[str, str], ...]  # ((workflow_file, status), ...)
    # Factory: CIHealth.from_workflows(sha, {workflow: status})
    # Derived: workflow_abbrevs → [(abbrev, status, icon), ...]

@dataclass
class BoardIssue:
    issue: Issue
    blocked_by_count: int
    blocks_count: int
    blockers: list[tuple[int, str, int | None]]  # (issue_num, state, sprint_num)
    # Derived: is_blocked, open_blocker_count, blocker_context
```

## HTMX Patterns

### Live Search
```html
<input type="search"
       hx-get="/issues"
       hx-trigger="keyup changed delay:300ms"
       hx-target="#results">
```

### Filter Buttons
```html
<button hx-get="/issues?label=bug" hx-target="#results">Bugs</button>
```

### Partial Updates
Server checks `HX-Request` header and returns partial HTML instead of full page.

## Configuration

Configuration is loaded in order of precedence:
1. Constructor arguments
2. Environment variables (GITEA_URL, GITEA_TOKEN, GITEA_OWNER, GITEA_REPO)
3. Tea CLI config (~/.config/tea/config.yml)

```bash
# .env (or use tea CLI config for URL/token)
GITEA_URL=https://gitea.example.com
GITEA_TOKEN=xxx
GITEA_OWNER=your_org
GITEA_REPO=your_repo
```

## CI Pipeline Health

The dashboard shows CI pipeline status for the current sprint via the **Gitea Actions Runs API**.

**Why Actions Runs API instead of Commit Status API:**
- Commit status API (`/commits/{sha}/status`) only reports statuses set by `ci.yml` jobs
- Downstream chained workflows (build, staging-deploy, staging-verify) don't set commit statuses on the original commit
- The Actions Runs API (`/actions/runs`) shows all workflow runs and gives the full pipeline view

**API pattern:**
1. Get main branch SHA: `GET /repos/{owner}/{repo}/branches/main` → `response.commit.id`
2. Get recent runs: `GET /repos/{owner}/{repo}/actions/runs?limit=20`
3. Filter runs by SHA, group by workflow file (from `path` field: `"ci.yml@refs/heads/main"` → `"ci.yml"`)
4. Take latest run per workflow, map `status`/`conclusion` to display state

**Pipeline workflows tracked** (`PIPELINE_WORKFLOWS` constant):
- `ci.yml` → **C** (Lint, Unit Tests, Integration Tests)
- `build.yml` → **B** (Build and push Docker images)
- `staging-deploy.yml` → **D** (Deploy to staging)
- `staging-verify.yml` → **V** (Smoke, E2E, visual tests)

**Display:** Home page sprint card and board current sprint column show `✓`/`✗`/`⏳` with per-workflow breakdown.

## Future Enhancements (Phase 2)

| Feature | Storage Needed |
|---------|----------------|
| Burndown charts | SQLite (daily snapshots) |
| Velocity trends | SQLite (sprint history) |
| Sprint dates/goals | SQLite (sprint metadata) |
| Cycle time | Event log parsing |

## Running

```bash
cd sprint-dash
pip install -e .
cp .env.example .env
# Edit .env with Gitea token
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

## Current State

Beyond MVP with:
- `app/gitea.py` (~1100 lines) - Gitea API client with issues, sprints, milestones, dependencies, CI health
- `app/main.py` (~420 lines) - FastAPI routes with board, backlog, search, issue detail views
- `templates/` - Jinja2 + HTMX templates with dark theme, board view, filters
- CI pipeline health integration via Actions Runs API
- Dependency tracking with blocked/blocker indicators on board cards
- Milestone-based sprint lifecycle (via ADR-0017)

## TODO

- [ ] Test with real Gitea connection
- [x] Add `__init__.py` to app/
- [x] Add loading indicators (htmx-indicator)
- [x] Caching for Gitea API calls (60s TTL via cachetools)
- [x] Error handling for API failures (GiteaError + error.html partial)
- [x] Tea CLI config integration (falls back to ~/.config/tea/config.yml)
- [x] Pagination improvements with max_pages limit and truncation warnings
- [x] CI pipeline health on home and board views
- [x] Issue dependency tracking (blocked-by / blocks)
- [x] Milestone-based sprint lifecycle state
- [x] Board view with Kanban columns, filters, epic grouping
- [ ] Deploy behind Caddy

## Design Principles

1. **Read-only** - Never modify Gitea data
2. **Server-rendered** - No JS framework, HTMX for interactivity
3. **Minimal dependencies** - FastAPI, httpx, Jinja2
4. **Dark theme** - Matches terminal aesthetic
5. **Parse at boundaries** - Gitea JSON → typed dataclasses immediately
