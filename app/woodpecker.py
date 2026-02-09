"""Woodpecker CI client for pipeline health data."""

import logging
import os

import httpx
from cachetools import TTLCache

from .gitea import (
    NIGHTLY_WORKFLOWS,
    PIPELINE_WORKFLOWS,
    CIHealth,
    NightlyHealth,
    NightlySummary,
)

logger = logging.getLogger(__name__)

# Repo ID cache (long-lived — repo IDs don't change)
_repo_id_cache: TTLCache[str, int] = TTLCache(maxsize=50, ttl=3600)

# CI health caches (60s/5s two-tier)
_ci_health_cache: TTLCache[str, CIHealth] = TTLCache(maxsize=10, ttl=60)
_ci_health_failure_cache: TTLCache[str, CIHealth] = TTLCache(maxsize=10, ttl=5)

# Nightly caches (60s/5s two-tier)
_nightly_cache: TTLCache[str, NightlySummary] = TTLCache(maxsize=10, ttl=60)
_nightly_failure_cache: TTLCache[str, None] = TTLCache(maxsize=10, ttl=5)


class WoodpeckerError(Exception):
    """Raised when Woodpecker API call fails."""


class WoodpeckerClient:
    """Woodpecker CI API client for pipeline health.

    Configuration (in order of precedence):
    1. Constructor arguments
    2. Environment variables (WOODPECKER_URL, WOODPECKER_TOKEN)
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
    ):
        resolved_url = base_url if base_url else os.getenv("WOODPECKER_URL", "")
        self.base_url = resolved_url.rstrip("/") if resolved_url else ""
        self.token = token if token else os.getenv("WOODPECKER_TOKEN", "") or ""

        if not self.base_url:
            raise WoodpeckerError(
                "No Woodpecker URL configured. Set WOODPECKER_URL in .env."
            )
        if not self.token:
            raise WoodpeckerError(
                "No Woodpecker token configured. Set WOODPECKER_TOKEN in .env."
            )

        self._client = httpx.Client(
            base_url=f"{self.base_url}/api",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> "WoodpeckerClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get_repo_id(self, owner: str, repo: str) -> int:
        """Resolve owner/repo to Woodpecker numeric repo_id.

        Cached for 1 hour (repo IDs don't change).
        """
        cache_key = f"{self.base_url}:{owner}/{repo}"
        if cache_key in _repo_id_cache:
            return _repo_id_cache[cache_key]

        try:
            resp = self._client.get(f"/repos/lookup/{owner}%2F{repo}")
            resp.raise_for_status()
            repo_id: int = resp.json()["id"]
            _repo_id_cache[cache_key] = repo_id
            return repo_id
        except (httpx.HTTPStatusError, httpx.RequestError, KeyError) as e:
            raise WoodpeckerError(
                f"Failed to resolve Woodpecker repo ID for {owner}/{repo}: {e}"
            ) from e

    def _pipeline_url(self, repo_id: int, pipeline_number: int) -> str:
        """Construct pipeline URL (Woodpecker has no link field)."""
        return f"{self.base_url}/repos/{repo_id}/pipeline/{pipeline_number}"

    @staticmethod
    def _map_status(status: str) -> str:
        """Map Woodpecker status to internal status vocabulary.

        Woodpecker statuses: success, failure, running, pending,
        blocked, declined, error, killed.
        """
        mapping = {
            "success": "success",
            "failure": "failure",
            "running": "running",
            "pending": "pending",
            "blocked": "pending",
            "declined": "cancelled",
            "error": "failure",
            "killed": "cancelled",
        }
        return mapping.get(status.lower(), "unknown")

    def get_ci_health(self, owner: str, repo: str) -> CIHealth:
        """Get CI pipeline health from Woodpecker.

        Woodpecker models CI/Build/Deploy/Verify as a single pipeline
        with multiple workflows linked by depends_on. The list endpoint
        returns pipelines; the detail endpoint has per-workflow breakdown.

        API call pattern (2 calls):
        1. GET /repos/{repo_id}/pipelines?per_page=5&event=push
        2. GET /repos/{repo_id}/pipelines/{number} (detail for latest)

        Returns CIHealth with per-workflow breakdown, or unknown on error.
        """
        cache_key = f"{self.base_url}:{owner}/{repo}:ci_health"
        if cache_key in _ci_health_cache:
            return _ci_health_cache[cache_key]
        if cache_key in _ci_health_failure_cache:
            return _ci_health_failure_cache[cache_key]

        unknown = CIHealth(sha="?", state="unknown", workflows=())

        try:
            repo_id = self._get_repo_id(owner, repo)

            # Fetch latest push pipelines
            resp = self._client.get(
                f"/repos/{repo_id}/pipelines",
                params={"per_page": 5, "event": "push"},
            )
            resp.raise_for_status()
            pipelines = resp.json()

            if not pipelines:
                _ci_health_failure_cache[cache_key] = unknown
                return unknown

            # Latest pipeline is first
            latest = pipelines[0]
            pipeline_number = latest["number"]
            commit_sha = (latest.get("commit", "") or "")[:8] or "?"

            # Fetch pipeline detail for workflow breakdown
            detail_resp = self._client.get(
                f"/repos/{repo_id}/pipelines/{pipeline_number}"
            )
            detail_resp.raise_for_status()
            detail = detail_resp.json()

            # Extract per-workflow status
            workflow_list = detail.get("workflows", [])
            pipeline_url = self._pipeline_url(repo_id, pipeline_number)

            workflows: dict[str, tuple[str, str]] = {}
            for wf_name in PIPELINE_WORKFLOWS:
                found = False
                for wf in workflow_list:
                    if wf.get("name") == wf_name:
                        status = self._map_status(wf.get("state", "unknown"))
                        workflows[wf_name] = (status, pipeline_url)
                        found = True
                        break
                if not found:
                    workflows[wf_name] = ("not_run", "")

            result = CIHealth.from_workflows(commit_sha, workflows)
            _ci_health_cache[cache_key] = result
            return result

        except (
            httpx.HTTPStatusError,
            httpx.RequestError,
            WoodpeckerError,
        ) as e:
            logger.warning("Failed to fetch Woodpecker CI health: %s", e)
            _ci_health_failure_cache[cache_key] = unknown
            return unknown

    def get_nightly_summary(
        self, owner: str, repo: str
    ) -> NightlySummary | None:
        """Get nightly workflow status from Woodpecker.

        Nightly runs are identified by event=cron. We fetch recent cron
        pipelines, then fetch detail for each to identify which nightly
        workflow it corresponds to (by workflow name in the detail).

        API call pattern (1 + N calls):
        1. GET /repos/{repo_id}/pipelines?event=cron&per_page=10
        2. GET /repos/{repo_id}/pipelines/{number} per pipeline

        Returns NightlySummary or None on error.
        """
        cache_key = f"{self.base_url}:{owner}/{repo}:nightly"
        if cache_key in _nightly_cache:
            return _nightly_cache[cache_key]
        if cache_key in _nightly_failure_cache:
            return _nightly_failure_cache[cache_key]

        nightly_names = {wf for _ab, wf, _dn, _wt in NIGHTLY_WORKFLOWS}

        try:
            repo_id = self._get_repo_id(owner, repo)

            # Fetch recent cron pipelines (server-side filter)
            resp = self._client.get(
                f"/repos/{repo_id}/pipelines",
                params={"per_page": 10, "event": "cron"},
            )
            resp.raise_for_status()
            pipelines = resp.json()

            if not pipelines:
                result = NightlySummary.from_runs({})
                _nightly_cache[cache_key] = result
                return result

            # Fetch detail per pipeline to identify nightly workflow name.
            # Keep latest run per nightly workflow.
            run_map: dict[str, NightlyHealth] = {}

            for pipeline in pipelines:
                if len(run_map) >= len(nightly_names):
                    break

                pipeline_number = pipeline["number"]
                pipeline_url = self._pipeline_url(repo_id, pipeline_number)

                detail_resp = self._client.get(
                    f"/repos/{repo_id}/pipelines/{pipeline_number}"
                )
                detail_resp.raise_for_status()
                detail = detail_resp.json()

                for wf in detail.get("workflows", []):
                    wf_name = wf.get("name", "")
                    if wf_name in nightly_names and wf_name not in run_map:
                        status = self._map_status(wf.get("state", "unknown"))
                        started_at = str(pipeline.get("started", ""))
                        run_map[wf_name] = NightlyHealth(
                            workflow=wf_name,
                            status=status,
                            started_at=started_at,
                            url=pipeline_url,
                        )

            result = NightlySummary.from_runs(run_map)
            _nightly_cache[cache_key] = result
            return result

        except (
            httpx.HTTPStatusError,
            httpx.RequestError,
            WoodpeckerError,
        ) as e:
            logger.warning("Failed to fetch Woodpecker nightly summary: %s", e)
            _nightly_failure_cache[cache_key] = None
            return None


# --- Client Factory ---

_client_instance: WoodpeckerClient | None = None


def get_woodpecker_client() -> WoodpeckerClient | None:
    """Get cached Woodpecker client.

    Returns None if WOODPECKER_URL/TOKEN not configured (graceful degradation).
    """
    global _client_instance  # noqa: PLW0603
    if _client_instance is not None:
        return _client_instance

    url = os.getenv("WOODPECKER_URL", "").strip()
    token = os.getenv("WOODPECKER_TOKEN", "").strip()

    if not url or not token:
        logger.debug("Woodpecker not configured (WOODPECKER_URL/TOKEN missing)")
        return None

    try:
        _client_instance = WoodpeckerClient(base_url=url, token=token)
        return _client_instance
    except WoodpeckerError as e:
        logger.warning("Failed to initialize Woodpecker client: %s", e)
        return None


def close_woodpecker_client() -> None:
    """Close the cached Woodpecker client."""
    global _client_instance  # noqa: PLW0603
    if _client_instance is not None:
        _client_instance.close()
        _client_instance = None
