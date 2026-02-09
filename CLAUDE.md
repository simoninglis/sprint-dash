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
- `WOODPECKER_URL` - Woodpecker CI server URL (optional, for CI health display)
- `WOODPECKER_TOKEN` - Woodpecker personal access token (optional)
- `GITEA_INSECURE=1` - Disable SSL verification for self-signed Gitea certs (optional)
- `GITEA_CA_BUNDLE` - Path to custom CA bundle (alternative to GITEA_INSECURE)

## Architecture

**Data flow**: Gitea API → `GiteaClient` → typed dataclasses → FastAPI routes → Jinja2 templates

**Key components**:
- `app/gitea.py` - Gitea API client with typed dataclasses (`Issue`, `Sprint`, `CIHealth`, `Milestone`, `BoardIssue`, etc.). Includes TTL caching (60s) and tea CLI config integration.
- `app/woodpecker.py` - Woodpecker CI API client for pipeline health (`WoodpeckerClient`). Separate from Gitea client (different URL, token, auth). Provides `get_ci_health()` and `get_nightly_summary()`.
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

**Woodpecker CI APIs used** (via `WoodpeckerClient`):

| API Endpoint | Purpose |
|-------------|---------|
| `GET /api/repos/lookup/{owner}%2F{repo}` | Resolve repo to numeric ID |
| `GET /api/repos/{repo_id}/pipelines` | Pipeline runs (supports `?event=push\|cron` filter) |
| `GET /api/repos/{repo_id}/pipelines/{number}` | Pipeline detail with per-workflow breakdown |

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

**Production URL**: `http://10.0.20.50:6080`

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

**Health endpoint**: `GET /health` → `{"status": "ok", "git_sha": "<sha>"}`

**Secrets** (in Woodpecker repo settings): `ci_gitea_token` (for docker registry login)

### Deployment gotchas
- `docker restart` does NOT re-read `--env-file` — must `docker rm` + `docker run` to pick up .env changes
- Port 6080 chosen to avoid conflicts with deckengine CI containers (8080) and browser-blocked ports (6000 = X11)
- Woodpecker clone sets git remote to `prod-vm-gitea` URL — don't use `git fetch origin` in build steps
- Trivy scan: starlette CVE-2024-47874 suppressed via `.trivyignore` (not exploitable, read-only app)

## Design Principles

1. **Read-only** - Never modify Gitea data
2. **Server-rendered** - No JS framework, HTMX for interactivity
3. **Parse at boundaries** - Gitea JSON → typed dataclasses immediately
4. **Minimal dependencies** - FastAPI, httpx, Jinja2
