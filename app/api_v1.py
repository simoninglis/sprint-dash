"""JSON API v1 endpoints for sprint management.

All endpoints live under /{owner}/{repo}/api/v1/ and return JSON.
Used by sd-cli HTTP client and any future integrations.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from .database import get_db
from .sprint_store import SprintStore

logger = logging.getLogger(__name__)

router = APIRouter()

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _validate_date_str(v: str | None) -> str | None:
    """Validate a date string is a real YYYY-MM-DD date."""
    if v is None:
        return v
    try:
        datetime.strptime(v, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        msg = f"Invalid date: '{v}' is not a valid YYYY-MM-DD date"
        raise ValueError(msg) from None
    return v


# --- Pydantic request models ---


class SprintCreate(BaseModel):
    number: int = Field(gt=0, description="Sprint number (positive integer)")
    start_date: str | None = Field(None, pattern=_DATE_PATTERN)
    end_date: str | None = Field(None, pattern=_DATE_PATTERN)
    goal: str = ""

    @field_validator("start_date", "end_date")
    @classmethod
    def check_date(cls, v: str | None) -> str | None:
        return _validate_date_str(v)


class SprintUpdate(BaseModel):
    start_date: str | None = Field(None, pattern=_DATE_PATTERN)
    end_date: str | None = Field(None, pattern=_DATE_PATTERN)
    goal: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def check_date(cls, v: str | None) -> str | None:
        return _validate_date_str(v)


class SprintStart(BaseModel):
    start_date: str | None = Field(None, pattern=_DATE_PATTERN)

    @field_validator("start_date")
    @classmethod
    def check_date(cls, v: str | None) -> str | None:
        return _validate_date_str(v)


class SprintClose(BaseModel):
    carry_over_to: int | None = Field(None, gt=0)


class IssueAdd(BaseModel):
    issues: list[int] = Field(min_length=1)
    source: str = "manual"

    @field_validator("issues")
    @classmethod
    def check_positive_issues(cls, v: list[int]) -> list[int]:
        if any(n <= 0 for n in v):
            msg = "All issue numbers must be positive integers"
            raise ValueError(msg)
        return v


class IssueMove(BaseModel):
    issues: list[int] = Field(min_length=1)
    from_sprint: int = Field(gt=0)
    to_sprint: int = Field(gt=0)

    @field_validator("issues")
    @classmethod
    def check_positive_issues(cls, v: list[int]) -> list[int]:
        if any(n <= 0 for n in v):
            msg = "All issue numbers must be positive integers"
            raise ValueError(msg)
        return v


# --- Helpers ---


def _get_store(owner: str, repo: str) -> SprintStore:
    return SprintStore(get_db(), owner, repo)


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


def _handle_exception(e: Exception) -> JSONResponse:
    """Map common exceptions to JSON error responses."""
    msg = str(e)
    if isinstance(e, ValueError):
        if "not found" in msg.lower():
            return _error(msg, "not_found", 404)
        return _error(msg, "lifecycle_error", 400)
    if isinstance(e, sqlite3.IntegrityError):
        return _error(msg, "conflict", 409)
    logger.exception("Unhandled error in API v1")
    return _error("Internal server error", "internal_error", 500)


# --- Sprint routes ---


@router.get("/{owner}/{repo}/api/v1/sprints")
async def list_sprints(
    request: Request, owner: str, repo: str, status: str | None = None
):
    store = _get_store(owner, repo)
    sprints = store.list_sprints(status=status)
    return sprints


@router.get("/{owner}/{repo}/api/v1/sprints/current")
async def current_sprint(request: Request, owner: str, repo: str):
    store = _get_store(owner, repo)
    number = store.get_current_sprint_number()
    if number is None:
        return _error("No sprint in progress", "not_found", 404)
    sprint = store.get_sprint(number)
    return sprint


@router.get("/{owner}/{repo}/api/v1/sprints/{n}")
async def get_sprint(request: Request, owner: str, repo: str, n: int):
    store = _get_store(owner, repo)
    sprint = store.get_sprint(n)
    if not sprint:
        return _error(f"Sprint {n} not found", "not_found", 404)

    issues = store.get_issue_numbers(n)
    start_snapshot = store.get_snapshot(n, "start")
    end_snapshot = store.get_snapshot(n, "end")

    result = dict(sprint)
    result["issues"] = issues
    result["issue_count"] = len(issues)
    result["start_snapshot"] = _clean_snapshot(start_snapshot)
    result["end_snapshot"] = _clean_snapshot(end_snapshot)
    return result


def _clean_snapshot(snap: dict | None) -> dict | None:
    """Return only the fields relevant to API consumers."""
    if not snap:
        return None
    return {
        "total_issues": snap["total_issues"],
        "total_points": snap["total_points"],
        "issue_numbers": snap["issue_numbers"],
    }


@router.post("/{owner}/{repo}/api/v1/sprints", status_code=201)
async def create_sprint(request: Request, owner: str, repo: str, body: SprintCreate):
    store = _get_store(owner, repo)
    try:
        sprint = store.create_sprint(
            body.number,
            start_date=body.start_date,
            end_date=body.end_date,
            goal=body.goal,
        )
    except Exception as e:
        return _handle_exception(e)
    return sprint


@router.put("/{owner}/{repo}/api/v1/sprints/{n}")
async def update_sprint(
    request: Request, owner: str, repo: str, n: int, body: SprintUpdate
):
    store = _get_store(owner, repo)
    fields: dict[str, str | None] = {}
    if body.start_date is not None:
        fields["start_date"] = body.start_date
    if body.end_date is not None:
        fields["end_date"] = body.end_date
    if body.goal is not None:
        fields["goal"] = body.goal

    if not fields:
        sprint = store.get_sprint(n)
        if not sprint:
            return _error(f"Sprint {n} not found", "not_found", 404)
        return sprint

    try:
        sprint = store.update_sprint(n, **fields)
    except Exception as e:
        return _handle_exception(e)
    if not sprint:
        return _error(f"Sprint {n} not found", "not_found", 404)
    return sprint


@router.post("/{owner}/{repo}/api/v1/sprints/{n}/start")
async def start_sprint(
    request: Request, owner: str, repo: str, n: int, body: SprintStart
):
    store = _get_store(owner, repo)
    start_date = body.start_date or datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        result = store.start_sprint(n, start_date=start_date)
    except Exception as e:
        return _handle_exception(e)
    return result


@router.post("/{owner}/{repo}/api/v1/sprints/{n}/close")
async def close_sprint(
    request: Request, owner: str, repo: str, n: int, body: SprintClose
):
    store = _get_store(owner, repo)

    issues = store.get_issue_numbers(n)
    end_date = datetime.now(UTC).strftime("%Y-%m-%d")

    carry_over_to = (
        body.carry_over_to if body.carry_over_to and body.carry_over_to > 0 else None
    )

    try:
        result = store.close_sprint(
            n,
            end_date=end_date,
            total_issues=len(issues),
            total_points=0,
            issue_numbers=issues,
            carry_over_to=carry_over_to,
            carry_over_issues=issues if carry_over_to is not None else None,
        )
    except Exception as e:
        return _handle_exception(e)
    return result


@router.post("/{owner}/{repo}/api/v1/sprints/{n}/cancel")
async def cancel_sprint(request: Request, owner: str, repo: str, n: int):
    store = _get_store(owner, repo)
    try:
        result = store.cancel_sprint(n)
    except Exception as e:
        return _handle_exception(e)
    return result


# --- Issue routes ---


@router.get("/{owner}/{repo}/api/v1/sprints/{n}/issues")
async def list_issues(request: Request, owner: str, repo: str, n: int):
    store = _get_store(owner, repo)
    sprint = store.get_sprint(n)
    if not sprint:
        return _error(f"Sprint {n} not found", "not_found", 404)
    issues = store.get_issue_numbers(n)
    return {"sprint": n, "issues": issues, "count": len(issues)}


@router.post("/{owner}/{repo}/api/v1/sprints/{n}/issues")
async def add_issues(request: Request, owner: str, repo: str, n: int, body: IssueAdd):
    store = _get_store(owner, repo)
    sprint = store.get_sprint(n)
    if not sprint:
        return _error(f"Sprint {n} not found", "not_found", 404)

    # Deduplicate issue list
    unique_issues = list(dict.fromkeys(body.issues))

    added = []
    failed = []
    for num in unique_issues:
        if store.add_issue(n, num, source=body.source):
            added.append(num)
        else:
            failed.append(num)

    if failed:
        return JSONResponse(
            {
                "error": f"Failed to add issues: {failed}",
                "code": "lifecycle_error",
                "added": added,
                "failed": failed,
            },
            status_code=400,
        )
    return {"sprint": n, "added": added}


@router.delete("/{owner}/{repo}/api/v1/sprints/{n}/issues/{issue}")
async def remove_issue(request: Request, owner: str, repo: str, n: int, issue: int):
    store = _get_store(owner, repo)
    sprint = store.get_sprint(n)
    if not sprint:
        return _error(f"Sprint {n} not found", "not_found", 404)
    if not store.remove_issue(n, issue):
        return _error(f"Issue {issue} not found in sprint {n}", "not_found", 404)
    return Response(status_code=204)


@router.post("/{owner}/{repo}/api/v1/issues/move")
async def move_issues(request: Request, owner: str, repo: str, body: IssueMove):
    store = _get_store(owner, repo)

    # Verify source sprint exists (404 not 400)
    if not store.get_sprint(body.from_sprint):
        return _error(f"Sprint {body.from_sprint} not found", "not_found", 404)
    if not store.get_sprint(body.to_sprint):
        return _error(f"Sprint {body.to_sprint} not found", "not_found", 404)

    # Deduplicate issue list to prevent partial mutation from repeated IDs
    unique_issues = list(dict.fromkeys(body.issues))

    # Pre-validate: check all issues exist in source sprint before moving any
    source_issues = store.get_issue_numbers(body.from_sprint)
    missing = [n for n in unique_issues if n not in source_issues]
    if missing:
        return JSONResponse(
            {
                "error": f"Issues not in sprint {body.from_sprint}: {missing}",
                "code": "lifecycle_error",
                "missing": missing,
            },
            status_code=400,
        )

    moved = []
    failed = []
    for num in unique_issues:
        if store.move_issue(num, body.from_sprint, body.to_sprint):
            moved.append(num)
        else:
            failed.append(num)

    if failed:
        return JSONResponse(
            {
                "error": f"Failed to move issues: {failed}",
                "code": "lifecycle_error",
                "moved": moved,
                "failed": failed,
            },
            status_code=400,
        )
    return {
        "from_sprint": body.from_sprint,
        "to_sprint": body.to_sprint,
        "moved": moved,
    }
