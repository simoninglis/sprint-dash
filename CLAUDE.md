# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## User Communication Shortcuts

- **peibtm** / **peitm** = "please explain it back to me" - Confirm understanding of the request before executing. Explain what you're going to do and wait for approval.

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
- `app/gitea.py` - Gitea API client with typed dataclasses (`Issue`, `Sprint`, `CIHealth`, `Milestone`, `BoardIssue`, etc.). Includes TTL caching (60s) and tea CLI config integration.
- `app/main.py` - FastAPI routes. All routes check `HX-Request` header to return partials vs full pages.
- `templates/` - Jinja2 templates using HTMX for interactivity. `base.html` contains all CSS (dark theme).

**HTMX pattern**: Routes return `partials/*.html` for HTMX requests, full templates otherwise.

**Gitea APIs used**:

| API Endpoint | Purpose |
|-------------|---------|
| `GET /repos/{owner}/{repo}/issues` | Issues with labels, pagination |
| `GET /repos/{owner}/{repo}/issues/{n}/dependencies` | Issue dependency graph |
| `GET /repos/{owner}/{repo}/issues/{n}/blocks` | Issues blocked by this one |
| `GET /repos/{owner}/{repo}/milestones` | Sprint lifecycle state |
| `GET /repos/{owner}/{repo}/branches/main` | Main branch HEAD SHA |
| `GET /repos/{owner}/{repo}/actions/runs` | CI workflow run statuses |

## CI Pipeline Health

The dashboard shows CI pipeline health on the home page and board view for the current sprint. This uses the **Gitea Actions Runs API** (`/actions/runs`), not the commit status API (which only covers `ci.yml` jobs, missing downstream workflows).

**Pipeline stages tracked** (defined in `PIPELINE_WORKFLOWS`):

| Abbrev | Workflow | Stage |
|--------|----------|-------|
| C | `ci.yml` | Lint, unit tests, integration tests |
| B | `build.yml` | Build and push Docker images |
| D | `staging-deploy.yml` | Deploy to staging |
| V | `staging-verify.yml` | Smoke, E2E, visual tests |

**State derivation**: Any failure → `failure`, any running → `running`, all success → `success`.

**Display**: `✓` (green), `✗` (red), `⏳` (yellow) next to sprint header, with per-workflow breakdown `C:✓ B:✓ D:✓ V:✓`.

**Caching**: 60-second TTL, same as issue data. CI health errors are swallowed gracefully - the dashboard renders without CI indicators rather than showing an error.

## Design Principles

1. **Read-only** - Never modify Gitea data
2. **Server-rendered** - No JS framework, HTMX for interactivity
3. **Parse at boundaries** - Gitea JSON → typed dataclasses immediately
4. **Minimal dependencies** - FastAPI, httpx, Jinja2
