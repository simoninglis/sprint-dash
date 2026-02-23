"""HTTP client for sprint-dash JSON API v1.

Used by sd-cli in client-server mode to talk to a running sprint-dash
instance over HTTP instead of accessing SQLite directly.
"""

from __future__ import annotations

from typing import Any

import httpx


class SprintDashError(Exception):
    """Error from the sprint-dash API."""

    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class SprintDashClient:
    """Synchronous HTTP client for sprint-dash API v1.

    Mirrors the SprintStore interface so CLI commands work with either backend.
    """

    def __init__(self, base_url: str, owner: str, repo: str, **kwargs: Any):
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.repo = repo
        self._client = httpx.Client(
            base_url=f"{self.base_url}/{owner}/{repo}/api/v1",
            timeout=30.0,
            **kwargs,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | list | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Make a request and handle errors.

        Wraps httpx transport errors (connection refused, timeout, DNS failure)
        as SprintDashError so callers only need to catch one exception type.
        """
        try:
            resp = self._client.request(method, path, json=json, params=params)
        except httpx.RequestError as exc:
            raise SprintDashError(
                f"Connection error: {exc}", code="connection_error", status=0
            ) from exc
        if resp.status_code == 204:
            return resp
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("error", resp.text)
                code = body.get("code", "")
            except Exception:
                msg = resp.text
                code = ""
            raise SprintDashError(msg, code=code, status=resp.status_code)
        return resp

    # --- Sprint operations ---

    def list_sprints(self, *, status: str | None = None) -> list[dict]:
        params = {}
        if status:
            params["status"] = status
        resp = self._request("GET", "/sprints", params=params)
        result: list[dict] = resp.json()
        return result

    def get_sprint(self, number: int) -> dict | None:
        try:
            resp = self._request("GET", f"/sprints/{number}")
        except SprintDashError as e:
            if e.status == 404:
                return None
            raise
        result: dict = resp.json()
        return result

    def create_sprint(
        self,
        number: int,
        *,
        status: str = "planned",
        start_date: str | None = None,
        end_date: str | None = None,
        goal: str = "",
    ) -> dict:
        body: dict = {"number": number, "goal": goal}
        if start_date:
            body["start_date"] = start_date
        if end_date:
            body["end_date"] = end_date
        resp = self._request("POST", "/sprints", json=body)
        result: dict = resp.json()
        return result

    def update_sprint(self, number: int, **fields: str | None) -> dict | None:
        body = {k: v for k, v in fields.items() if v is not None}
        try:
            resp = self._request("PUT", f"/sprints/{number}", json=body)
        except SprintDashError as e:
            if e.status == 404:
                return None
            raise
        result: dict = resp.json()
        return result

    def start_sprint(self, number: int, *, start_date: str | None = None) -> dict:
        body: dict = {}
        if start_date:
            body["start_date"] = start_date
        resp = self._request("POST", f"/sprints/{number}/start", json=body)
        result: dict = resp.json()
        return result

    def close_sprint(
        self,
        number: int,
        *,
        carry_over_to: int | None = None,
        **_kwargs: Any,
    ) -> dict:
        body: dict = {}
        if carry_over_to is not None and carry_over_to > 0:
            body["carry_over_to"] = carry_over_to
        resp = self._request("POST", f"/sprints/{number}/close", json=body)
        result: dict = resp.json()
        return result

    def cancel_sprint(self, number: int) -> dict:
        resp = self._request("POST", f"/sprints/{number}/cancel", json={})
        result: dict = resp.json()
        return result

    def get_current_sprint_number(self) -> int | None:
        try:
            resp = self._request("GET", "/sprints/current")
        except SprintDashError as e:
            if e.status == 404:
                return None
            raise
        data: dict = resp.json()
        return data.get("number")

    # --- Issue operations ---

    def get_issue_numbers(self, sprint_number: int) -> list[int]:
        try:
            resp = self._request("GET", f"/sprints/{sprint_number}/issues")
        except SprintDashError as e:
            if e.status == 404:
                return []
            raise
        data: dict = resp.json()
        issues: list[int] = data["issues"]
        return issues

    def add_issue(
        self, sprint_number: int, issue_number: int, *, source: str = "manual"
    ) -> bool:
        try:
            self._request(
                "POST",
                f"/sprints/{sprint_number}/issues",
                json={"issues": [issue_number], "source": source},
            )
        except SprintDashError as e:
            # Re-raise transport errors and unexpected server errors;
            # only return False for expected domain failures (400/404/409)
            if e.code == "connection_error" or e.status >= 500:
                raise
            return False
        return True

    def remove_issue(self, sprint_number: int, issue_number: int) -> bool:
        try:
            self._request(
                "DELETE", f"/sprints/{sprint_number}/issues/{issue_number}"
            )
        except SprintDashError as e:
            if e.code == "connection_error" or e.status >= 500:
                raise
            return False
        return True

    def move_issue(
        self, issue_number: int, from_sprint: int, to_sprint: int
    ) -> bool:
        try:
            self._request(
                "POST",
                "/issues/move",
                json={
                    "issues": [issue_number],
                    "from_sprint": from_sprint,
                    "to_sprint": to_sprint,
                },
            )
        except SprintDashError as e:
            if e.code == "connection_error" or e.status >= 500:
                raise
            return False
        return True

    def get_snapshot(self, sprint_number: int, snapshot_type: str) -> dict | None:
        """Get snapshot from the enriched sprint detail endpoint."""
        sprint = self.get_sprint(sprint_number)
        if not sprint:
            return None
        return sprint.get(f"{snapshot_type}_snapshot")

    def close(self) -> None:
        self._client.close()
