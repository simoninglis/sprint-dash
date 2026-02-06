"""Gitea API client for sprint data."""

import logging
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import httpx
import yaml
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Module-level cache for API responses (60-second TTL)
# Cache keys include base_url and repo identifier to support multi-instance scenarios
_issues_cache: TTLCache[
    tuple[str | None, str | None, str | None, str, str | None], list["Issue"]
] = TTLCache(maxsize=100, ttl=60)

# Pagination limits
MAX_PAGES = 100
PAGE_LIMIT = 50

# Size to points mapping for capacity estimation
SIZE_POINTS: dict[str, int] = {"S": 1, "M": 3, "L": 5, "XL": 8}

# Epic color palette (low-chroma colors for dark theme)
EPIC_COLORS: list[str] = [
    "#3b82f6",  # blue
    "#8b5cf6",  # purple
    "#06b6d4",  # cyan
    "#f59e0b",  # amber
    "#10b981",  # emerald
    "#ec4899",  # pink
    "#6366f1",  # indigo
    "#14b8a6",  # teal
]

# CI pipeline workflows to track (in order: CI → Build → Deploy → Verify)
PIPELINE_WORKFLOWS: tuple[str, ...] = (
    "ci.yml",
    "build.yml",
    "staging-deploy.yml",
    "staging-verify.yml",
)

# Cache for dependency counts (repo:issue_number -> (blocked_by_count, blocks_count, blockers_list))
_deps_cache: TTLCache[str, tuple[int, int, list[tuple[int, str, int | None]]]] = TTLCache(
    maxsize=500, ttl=60
)

# Cache for epic -> color mapping (built per board load)
_epic_colors: dict[str, str] = {}

# Milestone cache (60s TTL)
_milestones_cache: TTLCache[str, list["Milestone"]] = TTLCache(maxsize=10, ttl=60)

# CI health cache (60s TTL for success, keyed by repo)
_ci_health_cache: TTLCache[str, "CIHealth"] = TTLCache(maxsize=10, ttl=60)

# Separate short-lived cache for CI health failures (5s TTL) to avoid hammering API
_ci_health_failure_cache: TTLCache[str, "CIHealth"] = TTLCache(maxsize=10, ttl=5)


def get_epic_color(epic_name: str | None) -> str:
    """Get a consistent color for an epic."""
    if not epic_name:
        return "transparent"
    if epic_name not in _epic_colors:
        # Assign next available color
        idx = len(_epic_colors) % len(EPIC_COLORS)
        _epic_colors[epic_name] = EPIC_COLORS[idx]
    return _epic_colors[epic_name]


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
        logger.warning(
            "SSL verification disabled (GITEA_INSECURE=1). "
            "This is insecure and should not be used in production."
        )
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


@dataclass
class BoardIssue:
    """Issue wrapper with dependency info for board display."""

    issue: "Issue"
    blocked_by_count: int = 0
    blocks_count: int = 0
    # List of (issue_number, state, sprint_number) for blockers
    blockers: list[tuple[int, str, int | None]] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.blockers is None:
            self.blockers = []

    @property
    def is_blocked(self) -> bool:
        """True if blocked by any open issues."""
        return any(state == "open" for _, state, _ in self.blockers)

    @property
    def open_blocker_count(self) -> int:
        """Count of open blockers."""
        return sum(1 for _, state, _ in self.blockers if state == "open")

    @property
    def blocker_context(self) -> str:
        """Get sprint context for blockers (S-1, S0, S+1, BL)."""
        if not self.blockers:
            return ""
        current_sprint = self.issue.sprint
        contexts = []
        for _, state, blocker_sprint in self.blockers:
            if state != "open":
                continue
            if blocker_sprint is None:
                contexts.append("BL")
            elif current_sprint is None:
                contexts.append(f"S{blocker_sprint}")
            elif blocker_sprint < current_sprint:
                contexts.append("S-1")
            elif blocker_sprint == current_sprint:
                contexts.append("S0")
            else:
                contexts.append("S+1")
        # Return most relevant context
        if "S-1" in contexts:
            return "S-1"
        if "BL" in contexts:
            return "BL"
        if "S0" in contexts:
            return "S0"
        if "S+1" in contexts:
            return "S+1"
        return ""

    @property
    def epic_color(self) -> str:
        """Get the color for this issue's epic."""
        return get_epic_color(self.issue.epic)

    # Delegate common properties to the wrapped issue
    def __getattr__(self, name: str):
        return getattr(self.issue, name)


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

        # Fallback: parse from body (## Effort: S/M/L/XL or **Effort:** S)
        # Match "## Effort: S" or "## Effort\nS" or "**Effort:** M" or "XL"
        if self.body and (
            match := re.search(
                r"(?:##\s*Effort[:\s]*|\*\*Effort:?\*\*[:\s]*)(XL|[SMLsml])\b",
                self.body,
                re.IGNORECASE,
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
    lifecycle_state: str = "unknown"  # "in_progress", "planned", "completed", "unknown"

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

    @property
    def lifecycle_indicator(self) -> str:
        """Get visual indicator for lifecycle state."""
        return {
            "in_progress": "▶",
            "planned": "◻",
            "completed": "✓",
        }.get(self.lifecycle_state, "")


def _parse_start_date(description: str | None) -> date | None:
    """Extract start_date from milestone description first line.

    Expected format: 'start_date: YYYY-MM-DD' as the first line.
    """
    if not description:
        return None
    first_line = description.strip().split("\n")[0]
    if first_line.startswith("start_date:"):
        value = first_line.split(":", 1)[1].strip()
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Milestone:
    """Gitea milestone for sprint lifecycle tracking."""

    id: int
    title: str
    state: str  # "open" or "closed"
    open_issues: int
    closed_issues: int
    created_at: str
    description: str = ""

    @property
    def sprint_number(self) -> int | None:
        """Extract sprint number from title like 'Sprint 45'."""
        if match := re.match(r"Sprint (\d+)", self.title):
            return int(match.group(1))
        return None

    @property
    def start_date(self) -> date | None:
        """Get start_date from description (per ADR-0017)."""
        return _parse_start_date(self.description)

    @property
    def lifecycle_state(self) -> str:
        """Derive lifecycle state per ADR-0017.

        - Closed → completed
        - Open + start_date <= today → in_progress
        - Open + start_date > today or null → planned
        """
        if self.state == "closed":
            return "completed"
        start = self.start_date
        if start is not None and start <= date.today():
            return "in_progress"
        return "planned"


@dataclass(frozen=True)
class CIHealth:
    """CI pipeline health for a commit."""

    sha: str  # short SHA
    state: str  # "success", "failure", "pending", "running", "unknown"
    workflows: tuple[tuple[str, str], ...]  # ((workflow_file, status), ...)

    @classmethod
    def from_workflows(cls, sha: str, workflows: dict[str, str]) -> "CIHealth":
        """Create CIHealth from workflow dict, deriving overall state."""
        if not workflows:
            return cls(sha=sha, state="unknown", workflows=())

        # Derive state from workflow statuses
        statuses = list(workflows.values())
        if any(s == "failure" for s in statuses):
            state = "failure"
        elif any(s in ("running", "waiting") for s in statuses):
            state = "running"
        elif all(s == "success" for s in statuses):
            state = "success"
        else:
            state = "pending"

        return cls(
            sha=sha,
            state=state,
            workflows=tuple(workflows.items()),
        )

    @property
    def workflow_abbrevs(self) -> list[tuple[str, str, str]]:
        """Get workflow abbreviations with status for display.

        Returns list of (abbrev, status, icon) tuples.
        """
        abbrev_map = {
            "ci.yml": "C",
            "build.yml": "B",
            "staging-deploy.yml": "D",
            "staging-verify.yml": "V",
        }
        icon_map = {
            "success": "✓",
            "failure": "✗",
            "running": "⏳",
            "waiting": "⏳",
        }
        return [
            (abbrev_map.get(wf, wf[:1].upper()), status, icon_map.get(status, "?"))
            for wf, status in self.workflows
        ]


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
        counts: dict[str, int] = {"S": 0, "M": 0, "L": 0, "XL": 0, "?": 0}
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


@dataclass
class BoardData:
    """Data for the sprint board view."""

    backlog: list[Issue]
    sprints: list[Sprint]
    current_sprint_num: int | None

    @property
    def current_sprint(self) -> Sprint | None:
        """Get current (highest numbered) sprint."""
        for s in self.sprints:
            if s.number == self.current_sprint_num:
                return s
        return self.sprints[0] if self.sprints else None

    def get_sprint(self, number: int) -> Sprint | None:
        """Get sprint by number."""
        for s in self.sprints:
            if s.number == number:
                return s
        return None

    @property
    def next_sprints(self) -> list[Sprint]:
        """Get sprints after current (for planning columns)."""
        if not self.current_sprint_num:
            return []
        return [s for s in self.sprints if s.number > self.current_sprint_num]


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

        # Note: Using sync httpx.Client for simplicity. For a read-only dashboard
        # with low concurrency this is acceptable. For high-load scenarios,
        # consider switching to httpx.AsyncClient with async methods.
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
        # Include base_url in cache key for multi-instance support
        cache_key = (self.base_url, self.owner, self.repo, state, labels)

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
        milestones = self.get_milestones(state="all")
        milestone_map = {m.sprint_number: m for m in milestones if m.sprint_number}

        # Group by sprint
        sprint_map: dict[int, list[Issue]] = {}
        for issue in all_issues:
            if issue.sprint is not None:
                sprint_map.setdefault(issue.sprint, []).append(issue)

        # Build sprints with lifecycle state
        sprints = []
        for num, issues in sorted(sprint_map.items(), reverse=True):
            milestone = milestone_map.get(num)
            lifecycle = milestone.lifecycle_state if milestone else "unknown"
            sprints.append(
                Sprint(
                    number=num,
                    issues=tuple(issues),
                    lifecycle_state=lifecycle,
                )
            )
        return sprints

    def get_backlog(self) -> list[Issue]:
        """Get issues not in any sprint."""
        all_issues = self._get_issues(state="open")
        return [i for i in all_issues if i.sprint is None]

    def get_ready_queue(self) -> list[Issue]:
        """Get issues with 'ready' label but no sprint."""
        ready_issues = self._get_issues(state="open", labels="ready")
        return [i for i in ready_issues if i.sprint is None]

    def search_issues(self, query: str) -> list[Issue]:
        """Search issues by title or issue number (client-side filter).

        Supports:
        - Title search: "bug" matches issues with "bug" in title
        - Number search: "123" or "#123" matches issue #123
        """
        all_issues = self._get_issues()
        query = query.strip()

        # Check if query is an issue number (with or without #)
        number_match = re.match(r"^#?(\d+)$", query)
        if number_match:
            target_num = int(number_match.group(1))
            return [i for i in all_issues if i.number == target_num]

        # Otherwise search by title
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

    def get_dependency_info(
        self, number: int
    ) -> tuple[int, int, list[tuple[int, str, int | None]]]:
        """Get cached dependency counts and blocker info for an issue.

        Returns:
            Tuple of (blocked_by_count, blocks_count, blockers_list)
            where blockers_list is [(issue_num, state, sprint_num), ...]
        """
        cache_key = f"{self.base_url}:{self.owner}/{self.repo}:{number}"
        if cache_key in _deps_cache:
            return _deps_cache[cache_key]

        try:
            deps = self.get_issue_dependencies(number)
            blocks = self.get_issue_blocks(number)

            blockers = [(d.number, d.state, d.sprint) for d in deps]
            result = (len(deps), len(blocks), blockers)

            _deps_cache[cache_key] = result
            return result
        except GiteaError as e:
            # Log error but return empty deps to avoid breaking the board
            logger.warning(f"Failed to fetch dependencies for issue #{number}: {e}")
            return (0, 0, [])

    def to_board_issue(self, issue: Issue, fetch_deps: bool = True) -> BoardIssue:
        """Convert an Issue to a BoardIssue with dependency info."""
        if fetch_deps:
            blocked_by, blocks, blockers = self.get_dependency_info(issue.number)
            return BoardIssue(
                issue=issue,
                blocked_by_count=blocked_by,
                blocks_count=blocks,
                blockers=blockers,
            )
        return BoardIssue(issue=issue)

    def to_board_issues(
        self, issues: list[Issue], fetch_deps: bool = True
    ) -> list[BoardIssue]:
        """Convert a list of Issues to BoardIssues."""
        return [self.to_board_issue(issue, fetch_deps) for issue in issues]

    def get_milestones(self, state: str = "all") -> list[Milestone]:
        """Fetch milestones with caching.

        Args:
            state: Milestone state filter ('open', 'closed', 'all')

        Returns:
            List of milestones matching the filter (only sprint milestones)
        """
        cache_key = f"{self.base_url}:{self.owner}/{self.repo}:milestones:{state}"
        if cache_key in _milestones_cache:
            return _milestones_cache[cache_key]

        try:
            resp = self._client.get(
                f"/repos/{self.owner}/{self.repo}/milestones",
                params={"state": state},
            )
            resp.raise_for_status()
            milestones = [
                Milestone(
                    id=m["id"],
                    title=m["title"],
                    state=m["state"],
                    open_issues=m["open_issues"],
                    closed_issues=m["closed_issues"],
                    created_at=m["created_at"],
                    description=m.get("description", ""),
                )
                for m in resp.json()
                if m["title"].startswith("Sprint ")  # Filter to sprint milestones
            ]
            _milestones_cache[cache_key] = milestones
            return milestones
        except httpx.HTTPStatusError as e:
            raise GiteaError(f"Gitea API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise GiteaError(f"Network error: {e}") from e

    def get_current_sprint_number(self) -> int | None:
        """Get current sprint number from milestones (in_progress state).

        Returns:
            Sprint number of the current in-progress sprint, or lowest planned
            sprint if none in progress, or None if no milestones exist.
        """
        milestones = self.get_milestones(state="open")

        # Find in_progress milestones (lowest number first for multiple active)
        in_progress = [
            m
            for m in milestones
            if m.lifecycle_state == "in_progress" and m.sprint_number
        ]
        in_progress.sort(key=lambda m: m.sprint_number or 0)

        if in_progress:
            return in_progress[0].sprint_number

        # Fallback: lowest planned sprint
        planned = [
            m for m in milestones if m.lifecycle_state == "planned" and m.sprint_number
        ]
        planned.sort(key=lambda m: m.sprint_number or 0)

        if planned:
            return planned[0].sprint_number

        return None  # No milestones = fallback to label-based

    def get_main_sha(self, short: bool = False) -> str:
        """Get the HEAD SHA of the main branch.

        Args:
            short: If True, return 8-char short SHA. Otherwise full SHA.

        Returns:
            SHA of main branch HEAD, or "?" on error.
        """
        try:
            resp = self._client.get(f"/repos/{self.owner}/{self.repo}/branches/main")
            resp.raise_for_status()
            data = resp.json()
            full_sha: str = data.get("commit", {}).get("id", "")
            if not full_sha:
                return "?"
            return full_sha[:8] if short else full_sha
        except (httpx.HTTPStatusError, httpx.RequestError):
            return "?"

    def get_ci_health(self) -> CIHealth:
        """Get CI pipeline health for main branch HEAD.

        Returns:
            CIHealth with overall state and per-workflow status.
            On error, returns CIHealth with state="unknown".
        """
        cache_key = f"{self.base_url}:{self.owner}/{self.repo}:ci_health"
        if cache_key in _ci_health_cache:
            return _ci_health_cache[cache_key]
        # Check short-lived failure cache to avoid hammering API on errors
        if cache_key in _ci_health_failure_cache:
            return _ci_health_failure_cache[cache_key]

        try:
            full_sha = self.get_main_sha(short=False)
            if full_sha == "?":
                # Cache the "unknown SHA" failure briefly
                result = CIHealth(sha="?", state="unknown", workflows=())
                _ci_health_failure_cache[cache_key] = result
                return result

            short_sha = full_sha[:8]

            # Fetch recent workflow runs
            resp = self._client.get(
                f"/repos/{self.owner}/{self.repo}/actions/runs",
                params={"limit": 20},
            )
            resp.raise_for_status()
            runs = resp.json().get("workflow_runs", [])

            # Filter runs for this SHA (exact match) and group by workflow
            workflow_runs: dict[str, dict] = {}
            for run in runs:
                if run.get("head_sha", "") == full_sha:
                    # Extract workflow file from path (e.g., "ci.yml@refs/heads/main")
                    path = run.get("path", "")
                    workflow_file = path.split("@")[0] if "@" in path else path

                    # Only track pipeline workflows
                    if workflow_file not in PIPELINE_WORKFLOWS:
                        continue

                    # Keep latest run per workflow (highest run_number)
                    if (
                        workflow_file not in workflow_runs
                        or run.get("run_number", 0)
                        > workflow_runs[workflow_file].get("run_number", 0)
                    ):
                        workflow_runs[workflow_file] = run

            # Build workflow status map
            workflows: dict[str, str] = {}
            for wf in PIPELINE_WORKFLOWS:
                if wf in workflow_runs:
                    run = workflow_runs[wf]
                    status = run.get("status", "")
                    if status == "completed":
                        workflows[wf] = run.get("conclusion", "unknown")
                    else:
                        workflows[wf] = status  # running, waiting, etc.

            result = CIHealth.from_workflows(short_sha, workflows)
            _ci_health_cache[cache_key] = result
            return result

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning(f"Failed to fetch CI health: {e}")
            # Cache the failure briefly (5s) to avoid hammering API on errors
            result = CIHealth(sha="?", state="unknown", workflows=())
            _ci_health_failure_cache[cache_key] = result
            return result

    def get_board_data(self, num_future_sprints: int = 2) -> BoardData:
        """Get all data needed for board view in a single fetch.

        Efficiently loads all issues once and groups them for the board.

        Args:
            num_future_sprints: How many sprints beyond current to show

        Returns:
            BoardData with backlog and sprints organized for board display
        """
        # Single fetch for all issues (uses cache)
        all_issues = self._get_issues(state="all")

        # Fetch milestones for lifecycle state
        milestones = self.get_milestones(state="all")
        milestone_map = {m.sprint_number: m for m in milestones if m.sprint_number}

        # Group by sprint
        sprint_map: dict[int, list[Issue]] = {}
        backlog: list[Issue] = []

        for issue in all_issues:
            if issue.sprint is not None:
                sprint_map.setdefault(issue.sprint, []).append(issue)
            elif issue.state == "open":
                # Only open issues in backlog
                backlog.append(issue)

        # Build sprint objects with lifecycle state from milestones
        sprints = []
        for num, issues in sorted(sprint_map.items(), reverse=True):
            milestone = milestone_map.get(num)
            lifecycle = milestone.lifecycle_state if milestone else "unknown"
            sprints.append(
                Sprint(
                    number=num,
                    issues=tuple(issues),
                    lifecycle_state=lifecycle,
                )
            )

        # Get current sprint from milestones (authoritative)
        current_num = self.get_current_sprint_number()

        # Fallback to label-based if no milestones
        if current_num is None and sprints:
            for s in sprints:
                if s.open_count > 0:
                    current_num = s.number
                    break
            if current_num is None:
                current_num = sprints[0].number

        return BoardData(
            backlog=backlog,
            sprints=sprints,
            current_sprint_num=current_num,
        )


@lru_cache
def get_client() -> GiteaClient:
    """Get cached Gitea client instance."""
    return GiteaClient()
