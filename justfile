# sprint-dash justfile

set dotenv-load

default_host := "0.0.0.0"
default_port := "8080"

# List available recipes
default:
    @just --list

# Install dependencies
install:
    poetry install

# Run dev server with hot reload
serve host=default_host port=default_port:
    poetry run uvicorn app.main:app --host {{host}} --port {{port}} --reload

# Run linter
lint:
    poetry run ruff check app/

# Run linter with auto-fix
lint-fix:
    poetry run ruff check app/ --fix

# Run formatter
fmt:
    poetry run ruff format app/

# Check formatting without changes
fmt-check:
    poetry run ruff format app/ --check

# Run type checker
typecheck:
    poetry run mypy app/

# Run tests
test *args:
    poetry run pytest {{args}}

# Run tests with coverage
cov *args:
    poetry run pytest --cov {{args}}

# Run all quality checks (lint, typecheck, tests)
check: lint typecheck test
