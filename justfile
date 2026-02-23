# sprint-dash justfile

set dotenv-load

default_host := "0.0.0.0"
default_port := "8080"

# List available recipes
default:
    @just --list

# Install dependencies
install:
    uv sync

# Run dev server with hot reload
serve host=default_host port=default_port:
    uv run uvicorn app.main:app --host {{host}} --port {{port}} --reload

# Run linter
lint:
    uv run ruff check app/

# Run linter with auto-fix
lint-fix:
    uv run ruff check app/ --fix

# Run formatter
fmt:
    uv run ruff format app/

# Check formatting without changes
fmt-check:
    uv run ruff format app/ --check

# Run type checker
typecheck:
    uv run mypy app/

# Run tests
test *args:
    uv run pytest {{args}}

# Run tests with coverage
cov *args:
    uv run pytest --cov {{args}}

# Run all quality checks (lint, typecheck, tests)
check: lint typecheck test
