"""Health check endpoint for Docker HEALTHCHECK and deploy verification."""

import os

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return health status and deployed git SHA."""
    return {
        "status": "ok",
        "git_sha": os.getenv("GIT_SHA", "dev"),
    }
