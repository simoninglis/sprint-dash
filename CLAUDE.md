# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## User Communication Shortcuts

- **peibtm** / **peitm** = "please explain it back to me" - Confirm understanding of the request before executing. Explain what you're going to do and wait for approval.

## Project Overview

sprint-dash is a FastAPI + HTMX dashboard for sprint tracking against Gitea repositories. Sprint structure (membership, lifecycle, dates) is owned by a local SQLite database, while issue metadata (title, labels, state) comes from Gitea. The dashboard provides sprint boards, backlog views, burndown charts, and planning-vs-execution tracking.

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

# Sprint CLI (sd-cli) — HTTP mode (default, uses SPRINT_DASH_URL)
sd-cli --json sprint list                          # List sprints
sd-cli --json sprint show 47                       # Sprint details + issues
sd-cli sprint create 48 --start 2026-03-09 --end 2026-03-23 --goal "Feature X"
sd-cli sprint start 48                             # Set in_progress + snapshot
sd-cli sprint close 47 --carry-over-to 48          # Close + carry over
sd-cli sprint current                              # Current sprint number
sd-cli issue add 48 101 102 103                    # Add issues to sprint
sd-cli issue remove 48 101                         # Remove issue from sprint
sd-cli issue list 48                               # List issue numbers

# Sprint CLI — direct SQLite mode (inside Docker or with --db)
sd-cli --db /data/sprint-dash.db sprint current

# Migrate from Gitea labels/milestones to SQLite
poetry run python -m app.migrate
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
- `WOODPECKER_URL` - Woodpecker CI server URL (optional, for CI health display)
- `WOODPECKER_TOKEN` - Woodpecker personal access token (optional)
- `GITEA_INSECURE=1` - Disable SSL verification for self-signed Gitea certs (optional)
- `GITEA_CA_BUNDLE` - Path to custom CA bundle (alternative to GITEA_INSECURE)
- `SPRINT_DASH_DB` - SQLite database path (default: `/data/sprint-dash.db`)

### sd-cli client config (on dev machine)
- `SPRINT_DASH_URL` - Sprint-dash server URL (e.g., `http://sprint.internal.kellgari.com.au:6080`). Enables HTTP mode.
- `SPRINT_DASH_OWNER` / `SPRINT_DASH_REPO` - Target repo (falls back to `GITEA_OWNER`/`GITEA_REPO`)
- `SPRINT_DASH_DB` - If set, forces direct SQLite mode (overrides `SPRINT_DASH_URL`)

## Architecture

**Data flow**: SQLite (sprint structure) + Gitea API (issue metadata) → FastAPI routes → Jinja2 templates

**Key components**:
- `app/database.py` - SQLite connection manager. WAL mode, foreign keys, schema init. Singleton connection via `get_db()`. Path from `SPRINT_DASH_DB` env var (default `/data/sprint-dash.db`).
- `app/sprint_store.py` - `SprintStore` class — all sprint CRUD, repo-scoped by `(owner, repo)`. Manages sprints, sprint_issues (with soft-delete via `removed_at`), and snapshots.
- `app/api.py` - HTMX write endpoints (sprint CRUD, issue add/remove, carry-over, close). Returns HTMX partials for in-place UI updates.
- `app/api_v1.py` - JSON API v1 (`/{owner}/{repo}/api/v1/`). Used by sd-cli HTTP client. All endpoints return JSON, Pydantic request models, consistent error format.
- `app/http_client.py` - `SprintDashClient` class — sync httpx client that mirrors `SprintStore` interface over HTTP. Used by sd-cli in client-server mode.
- `app/cli.py` - sd-cli entry point. Dual-mode: HTTP client (default, via `SPRINT_DASH_URL`) or direct SQLite (via `--db` or `SPRINT_DASH_DB`).
- `app/migrate.py` - CLI migration: seeds SQLite from Gitea labels + milestones. Run once: `poetry run python -m app.migrate`.
- `app/gitea.py` - Gitea API client with typed dataclasses (`Issue`, `Sprint`, `CIHealth`, `Milestone`, `BoardIssue`, etc.). Includes TTL caching (60s) and tea CLI config integration.
- `app/woodpecker.py` - Woodpecker CI API client for pipeline health (`WoodpeckerClient`). Separate from Gitea client (different URL, token, auth). Provides `get_ci_health()` and `get_nightly_summary()`.
- `app/main.py` - FastAPI routes. All routes check `HX-Request` header to return partials vs full pages. Uses `SprintStore` for sprint structure, `GiteaClient` for issue details.
- `templates/` - Jinja2 templates using HTMX for interactivity. `base.html` contains all CSS (dark theme), Sortable.js for drag-and-drop.

**HTMX pattern**: Routes return `partials/*.html` for HTMX requests, full templates otherwise.

**Sprint data ownership**: Sprint membership, lifecycle, and dates are stored in SQLite (`sprints` + `sprint_issues` tables). Issue metadata (title, labels, assignees, state) comes from Gitea API with 60s cache. This separation means sprint operations are instant (no API calls) while issue data stays fresh.

**Write operations**: Sprint CRUD, add/remove issues, carry-over, close with snapshot. All via form-encoded POST/PUT/DELETE endpoints in `app/api.py`. Drag-and-drop between board columns uses Sortable.js + HTMX.

**Gitea APIs used**:

| API Endpoint | Purpose |
|-------------|---------|
| `GET /repos/{owner}/{repo}/issues` | Issues with labels, pagination |
| `GET /repos/{owner}/{repo}/issues/{n}/dependencies` | Issue dependency graph |
| `GET /repos/{owner}/{repo}/issues/{n}/blocks` | Issues blocked by this one |
| `GET /repos/{owner}/{repo}/milestones` | Sprint lifecycle state |

**Woodpecker CI APIs used** (via `WoodpeckerClient`):

| API Endpoint | Purpose |
|-------------|---------|
| `GET /api/repos/lookup/{owner}%2F{repo}` | Resolve repo to numeric ID |
| `GET /api/repos/{repo_id}/pipelines` | Pipeline runs (supports `?event=push\|cron` filter) |
| `GET /api/repos/{repo_id}/pipelines/{number}` | Pipeline detail with per-workflow breakdown |

**JSON API v1** (`/{owner}/{repo}/api/v1/`) — used by sd-cli HTTP client:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/sprints` | List sprints (optional `?status=` filter) |
| GET | `/sprints/current` | Current in-progress sprint |
| GET | `/sprints/{n}` | Sprint detail with issues + snapshots |
| POST | `/sprints` | Create sprint (body: `SprintCreate`) |
| PUT | `/sprints/{n}` | Update sprint dates/goal (body: `SprintUpdate`) |
| POST | `/sprints/{n}/start` | Start sprint (body: `SprintStart`) |
| POST | `/sprints/{n}/close` | Close sprint (body: `SprintClose`) |
| POST | `/sprints/{n}/cancel` | Cancel sprint |
| GET | `/sprints/{n}/issues` | List issue numbers |
| POST | `/sprints/{n}/issues` | Add issues (body: `IssueAdd`) |
| DELETE | `/sprints/{n}/issues/{issue}` | Remove issue (204) |
| POST | `/issues/move` | Move issues between sprints (body: `IssueMove`) |

Error responses: `{"error": "message", "code": "not_found|lifecycle_error|conflict|internal_error"}`.

## CI Pipeline Health

The dashboard shows CI pipeline health on the home page and board view for the current sprint. This uses the **Woodpecker CI Pipelines API** via a separate `WoodpeckerClient` (`app/woodpecker.py`).

**Pipeline stages tracked** (defined in `PIPELINE_WORKFLOWS`):

| Abbrev | Workflow | Stage |
|--------|----------|-------|
| C | `ci` | Lint, unit tests, integration tests |
| B | `build` | Build and push Docker images |
| D | `staging-deploy` | Deploy to staging |
| V | `staging-verify` | Smoke, E2E, visual tests |

All four stages run as a single Woodpecker pipeline with `depends_on` chaining. The list endpoint returns pipelines; the detail endpoint provides per-workflow status breakdown.

**State derivation**: Any failure → `failure`, any running → `running`, all success → `success`.

**Display**: `✓` (green), `✗` (red), `⏳` (yellow) next to sprint header, with per-workflow breakdown `C:✓ B:✓ D:✓ V:✓`.

**Caching**: 60-second TTL, same as issue data. CI health errors are swallowed gracefully - the dashboard renders without CI indicators rather than showing an error. If Woodpecker is not configured (`WOODPECKER_URL`/`WOODPECKER_TOKEN` missing), CI indicators are silently omitted.

## Deployment

Sprint-dash runs as a Docker container on `vm-gitea-runner-01` (10.0.20.50), deployed via Woodpecker CI.

**Production URL**: `http://sprint.internal.kellgari.com.au:6080` (10.0.20.50)

**CI/CD pipeline** (`.woodpecker/`): `ci` → `build` → `deploy` (~1 min total)

| Workflow | Steps | Duration |
|----------|-------|----------|
| `ci.yml` | install → lint + test (parallel) | ~13s |
| `build.yml` | docker build → trivy scan → push to Gitea registry | ~45s |
| `deploy.yml` | docker pull → stop/rm → run → health check | ~3s |

All workflows use `backend: local` (runs directly on the host, not Docker-in-Docker).

**Docker image**: `gitea.internal.kellgari.com.au/singlis/sprint-dash:<short-sha>`

**Runtime config**: `/opt/sprint-dash/.env` on the runner (not in repo). Contains:
- Gitea credentials (`GITEA_URL`, `GITEA_TOKEN`, `GITEA_OWNER`, `GITEA_REPO`)
- `GITEA_INSECURE=1` (Gitea uses self-signed cert)
- Woodpecker credentials (`WOODPECKER_URL`, `WOODPECKER_TOKEN`)

**Health endpoint**: `GET /health` → `{"status": "ok", "git_sha": "<sha>", "db": "ok"}`

**Persistent data**: SQLite database at `/opt/sprint-dash/data/sprint-dash.db` (mounted as Docker volume `-v /opt/sprint-dash/data:/data`)

**Secrets** (in Woodpecker repo settings): `ci_gitea_token` (for docker registry login)

### Deployment gotchas
- `docker restart` does NOT re-read `--env-file` — must `docker rm` + `docker run` to pick up .env changes
- Port 6080 chosen to avoid conflicts with deckengine CI containers (8080) and browser-blocked ports (6000 = X11)
- Woodpecker clone sets git remote to `prod-vm-gitea` URL — don't use `git fetch origin` in build steps
- Trivy scan: starlette CVE-2024-47874 suppressed via `.trivyignore` (not exploitable, read-only app)

## Database Schema

Three main tables (see `app/database.py` for full DDL):

| Table | Purpose |
|-------|---------|
| `sprints` | Sprint lifecycle — number, status, dates, goal. Scoped by `(repo_owner, repo_name)`. |
| `sprint_issues` | Issue membership — which issues belong to which sprint. Soft-delete via `removed_at`. Source tracking (`migration`, `manual`, `rollover`). |
| `sprint_snapshots` | Point-in-time captures at sprint start/end for planning-vs-execution analysis. |

**Migration**: `poetry run python -m app.migrate` seeds from Gitea labels + milestones. Idempotent (UNIQUE constraints, INSERT OR IGNORE).

## Design Principles

1. **Gitea is read-only** - Never modify Gitea data; sprint structure lives in SQLite
2. **Server-rendered** - No JS framework, HTMX for interactivity
3. **Parse at boundaries** - Gitea JSON → typed dataclasses immediately
4. **Minimal dependencies** - FastAPI, httpx, Jinja2, stdlib sqlite3
