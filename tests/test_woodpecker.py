"""Tests for Woodpecker CI client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.gitea import CIHealth, NightlySummary
from app.woodpecker import (
    WoodpeckerClient,
    WoodpeckerError,
    _ci_health_cache,
    _ci_health_failure_cache,
    _nightly_cache,
    _nightly_failure_cache,
    _repo_id_cache,
    get_woodpecker_client,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear all caches before each test."""
    _repo_id_cache.clear()
    _ci_health_cache.clear()
    _ci_health_failure_cache.clear()
    _nightly_cache.clear()
    _nightly_failure_cache.clear()
    yield


@pytest.fixture()
def mock_client():
    """Create a WoodpeckerClient with a mocked httpx.Client."""
    with patch("app.woodpecker.httpx.Client"):
        client = WoodpeckerClient(
            base_url="http://10.0.20.50:9090",
            token="test-token",
        )
        # Replace the real client with our mock
        mock_transport = MagicMock()
        client._client = mock_transport
        yield client, mock_transport


class TestWoodpeckerClientInit:
    """Test WoodpeckerClient initialization."""

    def test_missing_url_raises(self):
        with patch.dict("os.environ", {}, clear=True), pytest.raises(
            WoodpeckerError, match="No Woodpecker URL"
        ):
            WoodpeckerClient(base_url="", token="tok")

    def test_missing_token_raises(self):
        with patch.dict("os.environ", {}, clear=True), pytest.raises(
            WoodpeckerError, match="No Woodpecker token"
        ):
            WoodpeckerClient(base_url="http://localhost:9090", token="")

    def test_auth_header_uses_bearer(self):
        with patch("app.woodpecker.httpx.Client") as mock_httpx:
            WoodpeckerClient(base_url="http://localhost:9090", token="my-pat")
            call_kwargs = mock_httpx.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer my-pat"

    def test_base_url_trailing_slash_stripped(self):
        with patch("app.woodpecker.httpx.Client"):
            client = WoodpeckerClient(base_url="http://localhost:9090/", token="tok")
            assert client.base_url == "http://localhost:9090"


class TestRepoIdLookup:
    """Test _get_repo_id resolution and caching."""

    def test_lookup_success(self, mock_client):
        client, transport = mock_client
        resp = MagicMock()
        resp.json.return_value = {"id": 42}
        resp.raise_for_status = MagicMock()
        transport.get.return_value = resp

        repo_id = client._get_repo_id("singlis", "deckengine")
        assert repo_id == 42
        transport.get.assert_called_once_with("/repos/lookup/singlis%2Fdeckengine")

    def test_lookup_cached(self, mock_client):
        client, transport = mock_client
        resp = MagicMock()
        resp.json.return_value = {"id": 42}
        resp.raise_for_status = MagicMock()
        transport.get.return_value = resp

        # First call hits API
        client._get_repo_id("singlis", "deckengine")
        # Second call uses cache
        result = client._get_repo_id("singlis", "deckengine")
        assert result == 42
        assert transport.get.call_count == 1

    def test_lookup_failure_raises(self, mock_client):
        client, transport = mock_client
        transport.get.side_effect = httpx.RequestError("connection failed")

        with pytest.raises(WoodpeckerError, match="Failed to resolve"):
            client._get_repo_id("singlis", "deckengine")


class TestStatusMapping:
    """Test Woodpecker status to internal status mapping."""

    @pytest.mark.parametrize(
        ("wp_status", "expected"),
        [
            ("success", "success"),
            ("failure", "failure"),
            ("running", "running"),
            ("pending", "pending"),
            ("blocked", "pending"),
            ("declined", "cancelled"),
            ("error", "failure"),
            ("killed", "cancelled"),
            ("FAILURE", "failure"),  # case insensitive
            ("nonsense", "unknown"),
        ],
    )
    def test_status_mapping(self, wp_status, expected):
        assert WoodpeckerClient._map_status(wp_status) == expected


def _mock_repo_lookup(transport):
    """Set up transport to handle repo lookup returning id=1."""
    original_get = transport.get

    def side_effect(url, **kwargs):
        if "/repos/lookup/" in url:
            resp = MagicMock()
            resp.json.return_value = {"id": 1}
            resp.raise_for_status = MagicMock()
            return resp
        return original_get(url, **kwargs)

    return side_effect


class TestGetCIHealth:
    """Test CI health fetching from Woodpecker."""

    def _make_pipeline_list(self, number=10, commit="abc12345def"):
        return [{"number": number, "commit": commit}]

    def _make_pipeline_detail(self, workflows):
        return {"workflows": workflows}

    def _make_workflow(self, name, state="success"):
        return {"name": name, "state": state}

    def test_success_all_workflows(self, mock_client):
        client, transport = mock_client
        pipelines = self._make_pipeline_list()
        detail = self._make_pipeline_detail(
            [
                self._make_workflow("ci"),
                self._make_workflow("build"),
                self._make_workflow("staging-deploy"),
                self._make_workflow("staging-verify"),
            ]
        )

        responses = [
            # repo lookup
            MagicMock(json=MagicMock(return_value={"id": 1})),
            # pipeline list
            MagicMock(json=MagicMock(return_value=pipelines)),
            # pipeline detail
            MagicMock(json=MagicMock(return_value=detail)),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_ci_health("singlis", "deckengine")
        assert isinstance(result, CIHealth)
        assert result.state == "success"
        assert result.sha == "abc12345"
        assert len(result.workflows) == 4

    def test_partial_failure(self, mock_client):
        client, transport = mock_client
        pipelines = self._make_pipeline_list()
        detail = self._make_pipeline_detail(
            [
                self._make_workflow("ci", "success"),
                self._make_workflow("build", "failure"),
                self._make_workflow("staging-deploy", "blocked"),
                self._make_workflow("staging-verify", "blocked"),
            ]
        )

        responses = [
            MagicMock(json=MagicMock(return_value={"id": 1})),
            MagicMock(json=MagicMock(return_value=pipelines)),
            MagicMock(json=MagicMock(return_value=detail)),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_ci_health("singlis", "deckengine")
        assert result.state == "failure"

    def test_running_pipeline(self, mock_client):
        client, transport = mock_client
        pipelines = self._make_pipeline_list()
        detail = self._make_pipeline_detail(
            [
                self._make_workflow("ci", "success"),
                self._make_workflow("build", "running"),
            ]
        )

        responses = [
            MagicMock(json=MagicMock(return_value={"id": 1})),
            MagicMock(json=MagicMock(return_value=pipelines)),
            MagicMock(json=MagicMock(return_value=detail)),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_ci_health("singlis", "deckengine")
        assert result.state == "running"

    def test_no_pipelines_returns_unknown(self, mock_client):
        client, transport = mock_client
        responses = [
            MagicMock(json=MagicMock(return_value={"id": 1})),
            MagicMock(json=MagicMock(return_value=[])),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_ci_health("singlis", "deckengine")
        assert result.state == "unknown"
        assert result.sha == "?"

    def test_api_error_returns_unknown(self, mock_client):
        client, transport = mock_client
        # Repo lookup succeeds, pipeline list fails
        repo_resp = MagicMock(json=MagicMock(return_value={"id": 1}))
        repo_resp.raise_for_status = MagicMock()

        def side_effect(url, **kwargs):
            if "/repos/lookup/" in url:
                return repo_resp
            raise httpx.RequestError("timeout")

        transport.get = MagicMock(side_effect=side_effect)

        result = client.get_ci_health("singlis", "deckengine")
        assert result.state == "unknown"

    def test_cache_hit(self, mock_client):
        client, transport = mock_client
        pipelines = self._make_pipeline_list()
        detail = self._make_pipeline_detail(
            [
                self._make_workflow("ci"),
                self._make_workflow("build"),
                self._make_workflow("staging-deploy"),
                self._make_workflow("staging-verify"),
            ]
        )

        responses = [
            MagicMock(json=MagicMock(return_value={"id": 1})),
            MagicMock(json=MagicMock(return_value=pipelines)),
            MagicMock(json=MagicMock(return_value=detail)),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        # First call
        result1 = client.get_ci_health("singlis", "deckengine")
        # Second call should use cache
        result2 = client.get_ci_health("singlis", "deckengine")

        assert result1 is result2
        # Only 3 calls (lookup + list + detail), not 6
        assert transport.get.call_count == 3

    def test_missing_workflow_shows_not_run(self, mock_client):
        client, transport = mock_client
        pipelines = self._make_pipeline_list()
        # Only ci and build present — staging-deploy and staging-verify missing
        detail = self._make_pipeline_detail(
            [
                self._make_workflow("ci"),
                self._make_workflow("build"),
            ]
        )

        responses = [
            MagicMock(json=MagicMock(return_value={"id": 1})),
            MagicMock(json=MagicMock(return_value=pipelines)),
            MagicMock(json=MagicMock(return_value=detail)),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_ci_health("singlis", "deckengine")
        wf_dict = {wf: status for wf, status, _url in result.workflows}
        assert wf_dict["ci"] == "success"
        assert wf_dict["build"] == "success"
        assert wf_dict["staging-deploy"] == "not_run"
        assert wf_dict["staging-verify"] == "not_run"

    def test_pipeline_url_constructed(self, mock_client):
        client, transport = mock_client
        pipelines = self._make_pipeline_list(number=42)
        detail = self._make_pipeline_detail([self._make_workflow("ci")])

        responses = [
            MagicMock(json=MagicMock(return_value={"id": 7})),
            MagicMock(json=MagicMock(return_value=pipelines)),
            MagicMock(json=MagicMock(return_value=detail)),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_ci_health("singlis", "deckengine")
        # Check URL for the ci workflow
        ci_url = next(url for wf, _st, url in result.workflows if wf == "ci")
        assert ci_url == "http://10.0.20.50:9090/repos/7/pipeline/42"


class TestGetNightlySummary:
    """Test nightly summary fetching from Woodpecker."""

    def test_all_nightlies_found(self, mock_client):
        client, transport = mock_client

        # 3 cron pipelines, each with one nightly workflow
        pipelines = [
            {"number": 30, "started": 1707436800},
            {"number": 29, "started": 1707350400},
            {"number": 28, "started": 1707264000},
        ]
        details = [
            {"workflows": [{"name": "nightly-fuzz", "state": "success"}]},
            {"workflows": [{"name": "nightly-perf", "state": "success"}]},
            {"workflows": [{"name": "nightly-quality", "state": "failure"}]},
        ]

        call_count = 0

        def side_effect(url, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "/repos/lookup/" in url:
                resp.json.return_value = {"id": 1}
            elif "/pipelines/" in url and not url.endswith("/pipelines"):
                # Detail endpoint - extract pipeline number from URL
                idx = call_count - 2  # offset for lookup + list calls
                resp.json.return_value = details[idx]
            else:
                resp.json.return_value = pipelines
            call_count += 1
            return resp

        transport.get = MagicMock(side_effect=side_effect)

        result = client.get_nightly_summary("singlis", "deckengine")
        assert isinstance(result, NightlySummary)
        assert result.has_known
        assert result.has_failure

    def test_no_cron_pipelines(self, mock_client):
        client, transport = mock_client
        responses = [
            MagicMock(json=MagicMock(return_value={"id": 1})),
            MagicMock(json=MagicMock(return_value=[])),
        ]
        for r in responses:
            r.raise_for_status = MagicMock()
        transport.get = MagicMock(side_effect=responses)

        result = client.get_nightly_summary("singlis", "deckengine")
        assert isinstance(result, NightlySummary)
        assert not result.has_known

    def test_api_error_returns_none(self, mock_client):
        client, transport = mock_client
        repo_resp = MagicMock(json=MagicMock(return_value={"id": 1}))
        repo_resp.raise_for_status = MagicMock()

        def side_effect(url, **kwargs):
            if "/repos/lookup/" in url:
                return repo_resp
            raise httpx.RequestError("timeout")

        transport.get = MagicMock(side_effect=side_effect)

        result = client.get_nightly_summary("singlis", "deckengine")
        assert result is None


class TestGetWoodpeckerClient:
    """Test client factory function."""

    def test_returns_none_when_not_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            # Reset singleton
            import app.woodpecker as wp_mod

            wp_mod._client_instance = None
            result = get_woodpecker_client()
            assert result is None

    def test_returns_client_when_configured(self):
        with patch.dict(
            "os.environ",
            {"WOODPECKER_URL": "http://localhost:9090", "WOODPECKER_TOKEN": "tok"},
        ):
            import app.woodpecker as wp_mod

            wp_mod._client_instance = None
            with patch("app.woodpecker.httpx.Client"):
                result = get_woodpecker_client()
                assert isinstance(result, WoodpeckerClient)
                # Cleanup
                wp_mod._client_instance = None
