# sprint-dash

A lightweight sprint dashboard for Gitea. FastAPI + HTMX + SQLite — no JavaScript framework, no build step.

Sprint structure (membership, lifecycle, dates) lives in a local SQLite database. Issue metadata (title, labels, state) comes from Gitea's API. The dashboard never writes to Gitea.

## Why

Gitea's built-in issue tracker is great, but it doesn't give you sprint-centric views. If you're tracking sprints with labels or milestones, you end up switching between the terminal and Gitea's web UI during planning sessions. sprint-dash fills that gap with:

- **Board view** — Kanban columns with drag-and-drop (Sortable.js + HTMX)
- **Sprint management** — Create, start, close sprints with carry-over
- **Backlog** — Ready queue and unscheduled issues in one view
- **Burndown charts** — Point-in-time snapshots at sprint start/end
- **Dependency tracking** — Blocked/blocker indicators on board cards
- **CI health** — Woodpecker pipeline status on sprint cards (optional)
- **Search** — Live search with label and state filters
- **CLI** — `sd-cli` for terminal-based sprint operations

## Screenshot

Dark theme, server-rendered, designed for planning sessions on a second monitor.

<!-- TODO: Add screenshot -->

## Stack

| Component | Choice |
|-----------|--------|
| Backend | FastAPI |
| Frontend | HTMX + Jinja2 |
| Data | SQLite (sprints) + Gitea API (issues) |
| Styling | Inline CSS, dark theme |
| CI integration | Woodpecker (optional) |

## Quick Start

```bash
# Clone and install
git clone https://github.com/simoninglis/sprint-dash.git
cd sprint-dash
uv sync

# Configure
cp .env.example .env
# Edit .env with your Gitea URL and token

# Run
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

If you have [tea CLI](https://gitea.com/gitea/tea) configured, sprint-dash picks up your credentials from `~/.config/tea/config.yml` automatically. Just set `GITEA_OWNER` and `GITEA_REPO`.

### Docker

```bash
docker build -t sprint-dash .
docker run -d \
  --name sprint-dash \
  -p 8080:8080 \
  --env-file .env \
  -v sprint-dash-data:/data \
  sprint-dash
```

### Migrating from Labels/Milestones

If you've been tracking sprints with Gitea labels (`sprint/N`) or milestones:

```bash
uv run python -m app.migrate
```

This seeds the SQLite database from your existing Gitea data. Run once, then manage sprints through the dashboard or CLI.

## sd-cli

A companion CLI for sprint operations. Works in two modes:

- **HTTP mode** (default) — talks to a running sprint-dash server
- **Direct mode** — reads/writes SQLite directly (for Docker or local use)

```bash
sd-cli sprint list                          # List sprints
sd-cli sprint show 47                       # Sprint details + issues
sd-cli sprint create 48 --start 2026-03-09 --end 2026-03-23 --goal "Feature X"
sd-cli sprint start 48                      # Set in_progress + snapshot
sd-cli sprint close 47 --carry-over-to 48   # Close + carry over
sd-cli sprint current                       # Current sprint number
sd-cli issue add 48 101 102 103             # Add issues to sprint
sd-cli issue remove 48 101                  # Remove issue from sprint
```

### Standalone install

sd-cli is also published as a standalone package (HTTP mode only, no SQLite dependency):

```bash
uv tool install sd-cli  # from PyPI or your Gitea registry
```

## Architecture

```
┌─────────────────────────────────────────┐
│  Gitea API (read-only)                  │
│  issues, labels, milestones, deps       │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│  sprint-dash                            │
│  ┌────────────┐  ┌───────────────────┐  │
│  │  SQLite    │  │  FastAPI routes   │  │
│  │  sprints   │  │  HTMX partials    │  │
│  │  snapshots │  │  JSON API v1      │  │
│  └────────────┘  └───────────────────┘  │
└──────────────┬──────────────────────────┘
               │
        ┌──────┴──────┐
        │  Browser    │  sd-cli
        │  (HTMX)     │  (HTTP/SQLite)
        └─────────────┘
```

**Key design decisions:**

- **Gitea is read-only** — sprint-dash never modifies Gitea data. Sprint structure (which issues belong to which sprint, lifecycle state, dates) is owned by SQLite. Issue metadata (title, labels, assignees) comes from Gitea with 60s cache.
- **Server-rendered** — HTMX for interactivity, no client-side framework. Routes return HTML partials for HTMX requests, full pages otherwise.
- **Parse at boundaries** — Gitea JSON is converted to typed dataclasses (`Issue`, `Sprint`, `BoardIssue`, `CIHealth`) at the API layer. Everything downstream works with Python objects.
- **Minimal dependencies** — FastAPI, httpx, Jinja2, stdlib sqlite3, cachetools.

## Woodpecker CI Integration

If you run [Woodpecker CI](https://woodpecker-ci.org/), sprint-dash shows pipeline health on the home page and board view. Configure `WOODPECKER_URL` and `WOODPECKER_TOKEN` in `.env`.

Pipeline stages tracked: **C**I → **B**uild → **D**eploy → **V**erify, displayed as `C:✓ B:✓ D:✓ V:✓` next to the sprint header.

If Woodpecker isn't configured, CI indicators are silently omitted.

## CI/CD

The `.woodpecker/` directory contains the pipeline used to build, scan, and deploy sprint-dash:

| Workflow | What it does |
|----------|-------------|
| `ci.yml` | Lint (ruff) + tests (pytest) in parallel |
| `build.yml` | Docker build → Trivy scan → push to registry |
| `deploy.yml` | Pull image → restart container → health check |
| `publish-cli.yml` | Build and publish sd-cli package (on tag) |

Total pipeline time: ~60 seconds. All workflows use Woodpecker's `local` backend.

## Development

```bash
uv sync                          # Install dependencies
uv run pytest --cov              # Run tests with coverage
uv run ruff check app/           # Lint
uv run ruff format app/          # Format
uv run mypy app/                 # Type check
```

## Related

- [teax](https://github.com/simoninglis/teax) — CLI companion for Gitea's `tea` command, filling feature gaps (issue editing, bulk operations, dependencies, epics)

## License

MIT
