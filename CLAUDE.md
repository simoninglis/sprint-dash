# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

sprint-dash is a read-only FastAPI + HTMX dashboard for visualizing sprint and backlog data from Gitea. It provides sprint-centric views that Gitea's `sprint/N` label system lacks.

## Development Standards

Follow the patterns and practices documented in the [dev-manual](https://gitea.internal.kellgari.com.au/singlis/dev-manual):
- [Python Best Practices](../dev-manual/docs/python/best-practices.md)
- [Poetry Dependency Management](../dev-manual/docs/python/dependencies.md)
- [Code Quality Tools](../dev-manual/docs/python/code-quality.md)

## Commands

```bash
# Install dependencies
poetry install

# Run dev server
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# Lint and format
poetry run ruff check app/
poetry run ruff check app/ --fix
poetry run ruff format app/

# Type check
poetry run mypy app/

# Run tests
poetry run pytest
poetry run pytest --cov
```

## Configuration

Configuration is loaded in order of precedence:
1. Environment variables
2. Tea CLI config (`~/.config/tea/config.yml`)

### Option A: Use existing tea config (recommended)
If you have tea CLI configured, sprint-dash will use those credentials automatically. Just set the repo:
```bash
export GITEA_OWNER=singlis
export GITEA_REPO=deckengine
```

### Option B: Use .env file
Copy `.env.example` to `.env` and set:
- `GITEA_URL` - Gitea instance URL
- `GITEA_TOKEN` - API token
- `GITEA_OWNER` / `GITEA_REPO` - Target repository

## Architecture

**Data flow**: Gitea API → `GiteaClient` → typed dataclasses → FastAPI routes → Jinja2 templates

**Key components**:
- `app/gitea.py` - Gitea API client with `Issue` and `Sprint` dataclasses. Sprints are inferred from `sprint/N` labels on issues. Includes TTL caching (60s) and tea CLI config integration.
- `app/main.py` - FastAPI routes. All routes check `HX-Request` header to return partials vs full pages.
- `templates/` - Jinja2 templates using HTMX for interactivity. `base.html` contains all CSS (dark theme).

**HTMX pattern**: Routes return `partials/*.html` for HTMX requests, full templates otherwise.

## Design Principles

1. **Read-only** - Never modify Gitea data
2. **Server-rendered** - No JS framework, HTMX for interactivity
3. **Parse at boundaries** - Gitea JSON → typed dataclasses immediately
4. **Minimal dependencies** - FastAPI, httpx, Jinja2
