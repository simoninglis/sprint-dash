"""Health check endpoint for Docker HEALTHCHECK and deploy verification."""

import logging
import os

from fastapi import APIRouter

from .database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return health status and deployed git SHA."""
    db_status = "ok"
    try:
        conn = get_db()
        conn.execute("SELECT 1")
    except Exception:
        logger.exception("Database health check failed")
        db_status = "error"

    overall = "ok" if db_status == "ok" else "degraded"
    return {
        "status": overall,
        "git_sha": os.getenv("GIT_SHA", "dev"),
        "db": db_status,
    }
