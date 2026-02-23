# Sprint-Dash Dockerfile
# Multi-stage build for minimal production image

# Stage 1: Build
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies without the project itself (cache-friendly)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code and install project
COPY app/ app/
RUN uv sync --frozen --no-dev --no-editable

# Stage 2: Production
FROM python:3.12-slim AS production

WORKDIR /app

# Install curl for healthcheck and apply security updates
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code and templates
COPY app/ app/
COPY templates/ templates/

# Create data directory for SQLite
RUN mkdir -p /data

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app /data
USER appuser

# Persistent volume for sprint database
VOLUME /data

# Build info (passed from CI)
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Expose port
EXPOSE 8080

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
