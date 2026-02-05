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

@dataclass(frozen=True)
class Sprint:
    number: int
    issues: tuple[Issue, ...]
    # Derived: open_count, closed_count, progress_pct
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
GITEA_URL=https://gitea.internal.kellgari.com.au
GITEA_TOKEN=xxx
GITEA_OWNER=singlis
GITEA_REPO=deckengine
```

## Future Enhancements (Phase 2)

| Feature | Storage Needed |
|---------|----------------|
| Burndown charts | SQLite (daily snapshots) |
| Velocity trends | SQLite (sprint history) |
| Sprint dates/goals | SQLite (sprint metadata) |
| Cycle time | Event log parsing |

## Running

```bash
cd /home/singlis/work/sprint-dash
pip install -e .
cp .env.example .env
# Edit .env with Gitea token
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

## Current State

MVP scaffolded with:
- `app/gitea.py` - Gitea API client
- `app/main.py` - FastAPI routes
- `templates/` - Jinja2 + HTMX templates
- Basic dark theme CSS

## TODO

- [ ] Test with real Gitea connection
- [x] Add `__init__.py` to app/
- [x] Add loading indicators (htmx-indicator)
- [x] Caching for Gitea API calls (60s TTL via cachetools)
- [x] Error handling for API failures (GiteaError + error.html partial)
- [x] Tea CLI config integration (falls back to ~/.config/tea/config.yml)
- [x] Pagination improvements with max_pages limit and truncation warnings
- [ ] Deploy behind Caddy

## Design Principles

1. **Read-only** - Never modify Gitea data
2. **Server-rendered** - No JS framework, HTMX for interactivity
3. **Minimal dependencies** - FastAPI, httpx, Jinja2
4. **Dark theme** - Matches terminal aesthetic
5. **Parse at boundaries** - Gitea JSON → typed dataclasses immediately
