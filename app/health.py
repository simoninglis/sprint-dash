"""Health check endpoint for Docker HEALTHCHECK and deploy verification."""

import logging
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Return health status and deployed git SHA.

    Returns 200 when healthy, 503 when database is unavailable.
    """
    db_status = "ok"
    try:
        conn = get_db()
        conn.execute("SELECT 1")
    except Exception:
        logger.exception("Database health check failed")
        db_status = "error"

    overall = "ok" if db_status == "ok" else "degraded"
    status_code = 200 if db_status == "ok" else 503
    return JSONResponse(
        content={
            "status": overall,
            "git_sha": os.getenv("GIT_SHA", "dev"),
            "db": db_status,
        },
        status_code=status_code,
    )
