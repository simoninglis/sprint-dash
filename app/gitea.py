"""Gitea API client for sprint data."""

import logging
import os
import re
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
import yaml
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Module-level cache for API responses (60-second TTL)
_issues_cache: TTLCache[tuple[str, str | None], list["Issue"]] = TTLCache(
    maxsize=100, ttl=60
)

# Pagination limits
MAX_PAGES = 100
PAGE_LIMIT = 50

# Size to points mapping for capacity estimation
SIZE_POINTS: dict[str, int] = {"S": 1, "M": 3, "L": 5, "XL": 8}


def _get_ssl_verify() -> bool | str:
    """Get SSL verification setting.

    Environment variables (checked in order):
    - GITEA_CA_BUNDLE: Path to custom CA certificate bundle
    - GITEA_INSECURE=1: Disable SSL verification (not recommended)

    Returns:
        True for default verification, False to disable, or path string for custom CA.
    """
    ca_bundle = os.environ.get("GITEA_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle
    if os.environ.get("GITEA_INSECURE", "").lower() in ("1", "true", "yes"):
        return False
    return True


class GiteaError(Exception):
    """Raised when Gitea API call fails."""

    pass


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""

    pass


# --- Tea Config Support ---


def _get_tea_config_path() -> Path:
    """Get the path to tea's config file."""
    return Path.home() / ".config" / "tea" / "config.yml"


def _load_tea_config() -> dict | None:
    """Load tea configuration from YAML file.

    Returns:
        Parsed tea configuration dict, or None if not found/invalid.
    """
    path = _get_tea_config_path()

    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as f:
            result = yaml.safe_load(f)
            return result if isinstance(result, dict) else None
    except (yaml.YAMLError, PermissionError, OSError) as e:
        logger.debug(f"Could not load tea config: {e}")
        return None


def _get_tea_login(login_name: str | None = None) -> dict | None:
    """Get a tea login configuration.

    Args:
        login_name: Optional specific login name. If None, uses default.

    Returns:
        Login dict with 'url' and 'token' keys, or None if not found.
    """
    config = _load_tea_config()
    if not config or "logins" not in config:
        return None

    logins: list = config.get("logins", [])
    if not logins:
        return None

    # Find specific login or default
    if login_name:
        for login in logins:
            if isinstance(login, dict) and login.get("name") == login_name:
                return login
        return None

    # Find default login
    for login in logins:
        if isinstance(login, dict) and login.get("default"):
            return login

    # Fall back to first login
    first = logins[0] if logins else None
    return first if isinstance(first, dict) else None


def _normalize_base_url(url: str) -> str:
    """Normalize a base URL for API requests.

    Handles various URL formats:
    - Strips leading/trailing whitespace
    - Strips trailing slashes and /api/v1 if already present
    - Returns a clean base URL ending with /api/v1
    """
    url = url.strip().rstrip("/")
    # Remove any existing /api/v1 suffix to avoid duplication
    if url.endswith("/api/v1"):
        url = url[:-7]
    elif url.endswith("/api"):
        url = url[:-4]
    return url + "/api/v1"


# --- Data Models ---


@dataclass(frozen=True)
class Comment:
    id: int
    body: str
    user: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Dependency:
    """Represents a dependency link between issues."""

    number: int
    title: str
    state: str
    sprint: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_closed(self) -> bool:
        return self.state == "closed"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    created_at: str
    updated_at: str
    closed_at: str | None
    body: str = ""

    @property
    def sprint(self) -> int | None:
        """Extract sprint number from labels."""
        for label in self.labels:
            if match := re.match(r"sprint/(\d+)", label):
                return int(match.group(1))
        return None

    @property
    def is_ready(self) -> bool:
        return "ready" in self.labels

    @property
    def issue_type(self) -> str:
        """Infer issue type from labels."""
        for label in self.labels:
            if label in ("bug", "feature", "tech-debt", "chore", "docs", "hotfix"):
                return label
        return "unknown"

    @property
    def size(self) -> str | None:
        """Extract size from labels or body.

        Checks labels first (size/S, size/M, size/L), then falls back
        to parsing ## Effort section in body.
        """
        # Check labels first (new convention)
        for label in self.labels:
            if label.startswith("size/"):
                return label.split("/")[1].upper()

        # Fallback: parse from body (## Effort: S/M/L or **Effort:** S)
        # Match "## Effort: S" or "## Effort\nS" or "**Effort:** M"
        if self.body and (
            match := re.search(
                r"(?:##\s*Effort[:\s]*|\*\*Effort:?\*\*[:\s]*)([SMLsml])\b",
                self.body,
            )
        ):
            return match.group(1).upper()
        return None

    @property
    def points(self) -> int:
        """Get point value based on size."""
        return SIZE_POINTS.get(self.size or "", 0)

    @property
    def priority(self) -> int | None:
        """Extract priority from labels (P1=highest, P3=lowest)."""
        for label in self.labels:
            if match := re.match(r"P([1-3])$", label):
                return int(match.group(1))
        return None

    @property
    def epic(self) -> str | None:
        """Extract epic name from labels."""
        for label in self.labels:
            if label.startswith("epic/"):
                return label.split("/", 1)[1]
        return None


@dataclass(frozen=True)
class Sprint:
    number: int
    issues: tuple[Issue, ...]

    @property
    def open_count(self) -> int:
        return sum(1 for i in self.issues if i.state == "open")

    @property
    def closed_count(self) -> int:
        return sum(1 for i in self.issues if i.state == "closed")

    @property
    def total(self) -> int:
        return len(self.issues)

    @property
    def progress_pct(self) -> int:
        if self.total == 0:
            return 0
        return int(self.closed_count / self.total * 100)

    @property
    def total_points(self) -> int:
        return sum(i.points for i in self.issues)

    @property
    def completed_points(self) -> int:
        return sum(i.points for i in self.issues if i.state == "closed")


@dataclass
class BacklogStats:
    """Statistics for backlog display."""

    issues: list[Issue]

    @property
    def total_count(self) -> int:
        return len(self.issues)

    @property
    def total_points(self) -> int:
        return sum(i.points for i in self.issues)

    @property
    def size_counts(self) -> dict[str, int]:
        """Count of issues by size."""
        counts: dict[str, int] = {"S": 0, "M": 0, "L": 0, "?": 0}
        for issue in self.issues:
            size = issue.size or "?"
            counts[size] = counts.get(size, 0) + 1
        return counts

    @property
    def by_epic(self) -> dict[str | None, list[Issue]]:
        """Group issues by epic."""
        groups: dict[str | None, list[Issue]] = {}
        for issue in self.issues:
            groups.setdefault(issue.epic, []).append(issue)
        return groups

    @property
    def epics_sorted(self) -> list[tuple[str | None, list[Issue]]]:
        """Epics sorted by issue count, with None (no epic) last."""
        by_epic = self.by_epic
        result: list[tuple[str | None, list[Issue]]] = [
            (k, v) for k, v in by_epic.items() if k is not None
        ]
        result.sort(key=lambda x: (-len(x[1]), x[0] or ""))
        if None in by_epic:
            result.append((None, by_epic[None]))
        return result


# --- Gitea Client ---


class GiteaClient:
    """Simple Gitea API client.

    Configuration is loaded from (in order of precedence):
    1. Constructor arguments
    2. Environment variables (GITEA_URL, GITEA_TOKEN, GITEA_OWNER, GITEA_REPO)
    3. Tea CLI config (~/.config/tea/config.yml)
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        tea_login: str | None = None,
    ):
        """Initialize the Gitea client.

        Args:
            base_url: Gitea instance URL (optional, from env/tea if not provided)
            token: API token (optional, from env/tea if not provided)
            owner: Repository owner (optional, from env if not provided)
            repo: Repository name (optional, from env if not provided)
            tea_login: Specific tea login name to use (optional)
        """
        # Try tea config as fallback for URL and token
        tea = _get_tea_login(tea_login)

        self.base_url = (
            base_url or os.getenv("GITEA_URL") or (tea.get("url") if tea else None)
        )
        self.token = (
            token or os.getenv("GITEA_TOKEN") or (tea.get("token") if tea else None)
        )
        self.owner = owner or os.getenv("GITEA_OWNER", "")
        self.repo = repo or os.getenv("GITEA_REPO", "")

        if not self.base_url:
            raise ConfigError(
                "No Gitea URL configured. Set GITEA_URL in .env or configure tea CLI."
            )
        if not self.token:
            raise ConfigError(
                "No Gitea token configured. Set GITEA_TOKEN in .env or configure tea CLI."
            )

        # Normalize URL
        api_url = _normalize_base_url(self.base_url)

        self._client = httpx.Client(
            base_url=api_url,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/json",
            },
            timeout=30.0,
            verify=_get_ssl_verify(),
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> "GiteaClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _get_issues(
        self,
        state: str = "all",
        labels: str | None = None,
        max_pages: int = MAX_PAGES,
    ) -> list[Issue]:
        """Fetch issues from Gitea with TTL caching.

        Args:
            state: Issue state filter ('open', 'closed', 'all')
            labels: Comma-separated label filter
            max_pages: Maximum pages to fetch (prevents runaway pagination)

        Returns:
            List of issues matching the filter
        """
        cache_key = (state, labels)

        # Check cache first
        if cache_key in _issues_cache:
            return _issues_cache[cache_key]

        params: dict[str, str | int] = {"state": state, "limit": PAGE_LIMIT}
        if labels:
            params["labels"] = labels

        issues: list[Issue] = []
        page = 1
        truncated = False

        try:
            while page <= max_pages:
                params["page"] = page
                resp = self._client.get(
                    f"/repos/{self.owner}/{self.repo}/issues",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

                if not data:
                    break

                for item in data:
                    issues.append(
                        Issue(
                            number=item["number"],
                            title=item["title"],
                            state=item["state"],
                            labels=tuple(lbl["name"] for lbl in item.get("labels", [])),
                            created_at=item["created_at"],
                            updated_at=item["updated_at"],
                            closed_at=item.get("closed_at"),
                            body=item.get("body", ""),
                        )
                    )

                # If we got fewer items than the limit, we're on the last page
                if len(data) < PAGE_LIMIT:
                    break
                page += 1
            else:
                # Loop completed without break - hit max_pages ceiling
                truncated = True

        except httpx.HTTPStatusError as e:
            raise GiteaError(f"Gitea API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise GiteaError(f"Network error: {e}") from e

        if truncated:
            warnings.warn(
                f"Issues list truncated at {max_pages} pages "
                f"({len(issues)} items). Results may be incomplete.",
                UserWarning,
                stacklevel=2,
            )

        # Cache the result
        _issues_cache[cache_key] = issues
        return issues

    def get_sprint(self, number: int) -> Sprint:
        """Get a specific sprint by number."""
        issues = self._get_issues(labels=f"sprint/{number}")
        return Sprint(number=number, issues=tuple(issues))

    def get_sprints(self) -> list[Sprint]:
        """Get all sprints with issues."""
        all_issues = self._get_issues()

        # Group by sprint
        sprint_map: dict[int, list[Issue]] = {}
        for issue in all_issues:
            if issue.sprint is not None:
                sprint_map.setdefault(issue.sprint, []).append(issue)

        return [
            Sprint(number=num, issues=tuple(issues))
            for num, issues in sorted(sprint_map.items(), reverse=True)
        ]

    def get_backlog(self) -> list[Issue]:
        """Get issues not in any sprint."""
        all_issues = self._get_issues(state="open")
        return [i for i in all_issues if i.sprint is None]

    def get_ready_queue(self) -> list[Issue]:
        """Get issues with 'ready' label but no sprint."""
        ready_issues = self._get_issues(state="open", labels="ready")
        return [i for i in ready_issues if i.sprint is None]

    def search_issues(self, query: str) -> list[Issue]:
        """Search issues by title (client-side filter)."""
        all_issues = self._get_issues()
        query_lower = query.lower()
        return [i for i in all_issues if query_lower in i.title.lower()]

    def get_issue(self, number: int) -> Issue:
        """Get a single issue by number."""
        try:
            resp = self._client.get(f"/repos/{self.owner}/{self.repo}/issues/{number}")
            resp.raise_for_status()
            item = resp.json()
            return Issue(
                number=item["number"],
                title=item["title"],
                state=item["state"],
                labels=tuple(lbl["name"] for lbl in item.get("labels", [])),
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                closed_at=item.get("closed_at"),
                body=item.get("body", ""),
            )
        except httpx.HTTPStatusError as e:
            raise GiteaError(f"Gitea API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise GiteaError(f"Network error: {e}") from e

    def get_issue_comments(self, number: int) -> list[Comment]:
        """Get comments for an issue."""
        try:
            resp = self._client.get(
                f"/repos/{self.owner}/{self.repo}/issues/{number}/comments"
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                Comment(
                    id=item["id"],
                    body=item["body"],
                    user=item.get("user", {}).get("login", "unknown"),
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                )
                for item in data
            ]
        except httpx.HTTPStatusError as e:
            raise GiteaError(f"Gitea API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise GiteaError(f"Network error: {e}") from e

    def _parse_dependency(self, item: dict) -> Dependency:
        """Parse a dependency from API response."""
        labels = [lbl["name"] for lbl in item.get("labels", [])]
        sprint_num = None
        for label in labels:
            if match := re.match(r"sprint/(\d+)", label):
                sprint_num = int(match.group(1))
                break
        return Dependency(
            number=item["number"],
            title=item["title"],
            state=item["state"],
            sprint=sprint_num,
        )

    def get_issue_dependencies(self, number: int) -> list[Dependency]:
        """Get issues that this issue depends on (is blocked by)."""
        try:
            resp = self._client.get(
                f"/repos/{self.owner}/{self.repo}/issues/{number}/dependencies"
            )
            resp.raise_for_status()
            data = resp.json()
            return [self._parse_dependency(item) for item in data] if data else []
        except httpx.HTTPStatusError as e:
            # 404 might mean dependencies feature not enabled or no deps
            if e.response.status_code == 404:
                return []
            raise GiteaError(f"Gitea API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise GiteaError(f"Network error: {e}") from e

    def get_issue_blocks(self, number: int) -> list[Dependency]:
        """Get issues that this issue blocks."""
        try:
            resp = self._client.get(
                f"/repos/{self.owner}/{self.repo}/issues/{number}/blocks"
            )
            resp.raise_for_status()
            data = resp.json()
            return [self._parse_dependency(item) for item in data] if data else []
        except httpx.HTTPStatusError as e:
            # 404 might mean dependencies feature not enabled or no blocks
            if e.response.status_code == 404:
                return []
            raise GiteaError(f"Gitea API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise GiteaError(f"Network error: {e}") from e


@lru_cache
def get_client() -> GiteaClient:
    """Get cached Gitea client instance."""
    return GiteaClient()
