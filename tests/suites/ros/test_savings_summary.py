"""E2E tests for ROS savings estimation endpoints."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import assert_structured_savings, parse_savings_value

_FLOAT_TOLERANCE = 0.02


def _savings_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/savings-summary"
    )


def _fleet_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/fleet-summary"
    )


def _has_container_recommendations(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> bool:
    resp = session.get(
        get_recommendations_endpoint(ros_api_url),
        headers=auth,
        params={"limit": 1},
        timeout=60,
    )
    if resp.status_code != 200:
        return False
    return bool(resp.json().get("data"))


def _plugin_sum(by_plugin: dict[str, Any]) -> float:
    """Sum plugin savings included in fleet total (GPU excluded at read time)."""
    total = 0.0
    for key in ("container", "node", "pvc", "snapshot", "vm"):
        value = by_plugin.get(key)
        if value is not None:
            total += float(value)
    return total


@pytest.fixture
def savings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.fixture
def require_recommendations(
    ros_api_url: str,
    savings_auth: dict,
    http_session: requests.Session,
):
    """Skip savings/fleet tests when the cluster has no container recommendations yet."""
    if not _has_container_recommendations(http_session, ros_api_url, savings_auth):
        pytest.skip(
            "No container recommendations in cluster; ingest ROS data before running "
            "savings summary E2E tests"
        )


@pytest.mark.ros
@pytest.mark.integration
class TestSavingsSummaryE2E:
    """Fleet savings and fleet summary endpoints."""

    @pytest.mark.timeout(60)
    def test_savings_summary_returns_valid_structure(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "currency" in body
        assert "estimated_monthly_savings" in body
        assert_structured_savings(body["estimated_monthly_savings"])
        assert "by_cluster" in body
        assert "by_plugin" in body
        assert isinstance(body["by_cluster"], list)
        assert isinstance(body["by_plugin"], dict)

    @pytest.mark.timeout(60)
    def test_savings_summary_by_plugin_has_expected_keys(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        by_plugin = resp.json()["by_plugin"]
        for key in ("container", "node", "pvc", "vm"):
            assert key in by_plugin, f"by_plugin missing required key {key!r}"
        # snapshot/gpu may be zero depending on cluster data
        for optional in ("snapshot", "gpu"):
            if optional in by_plugin:
                assert by_plugin[optional] is None or isinstance(
                    by_plugin[optional], (int, float)
                )

    @pytest.mark.timeout(60)
    def test_savings_summary_total_matches_plugin_sum(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        total = parse_savings_value(body["estimated_monthly_savings"])
        assert total is not None
        plugin_total = _plugin_sum(body["by_plugin"])
        assert total == pytest.approx(plugin_total, abs=_FLOAT_TOLERANCE), (
            f"total {total} != plugin sum {plugin_total} (by_plugin={body['by_plugin']})"
        )

    @pytest.mark.timeout(60)
    def test_savings_summary_engine_cost_default(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        default_resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        explicit_resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"engine": "cost"},
            timeout=60,
        )
        assert default_resp.status_code == 200, default_resp.text
        assert explicit_resp.status_code == 200, explicit_resp.text
        assert default_resp.json() == explicit_resp.json()

    @pytest.mark.timeout(60)
    def test_savings_summary_engine_performance_accepted(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"engine": "performance"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "estimated_monthly_savings" in body
        assert_structured_savings(body["estimated_monthly_savings"])
        assert "by_plugin" in body

    @pytest.mark.timeout(60)
    def test_savings_summary_invalid_engine_returns_400(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=savings_auth,
            params={"engine": "invalid"},
            timeout=60,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json().get("status") == "error"

    @pytest.mark.timeout(60)
    def test_fleet_summary_returns_valid_structure(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _fleet_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field in (
            "total_containers",
            "active_containers",
            "idle_containers",
            "abandoned_containers",
            "cluster_count",
            "currency",
        ):
            assert field in body, f"missing {field!r} in fleet-summary response"

    @pytest.mark.timeout(60)
    def test_fleet_summary_counts_consistent(
        self,
        require_recommendations,
        ros_api_url: str,
        savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _fleet_summary_url(ros_api_url),
            headers=savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        total = int(body["total_containers"])
        active = int(body["active_containers"])
        idle = int(body["idle_containers"])
        abandoned = int(body["abandoned_containers"])
        assert active + idle + abandoned <= total, (
            f"active({active}) + idle({idle}) + abandoned({abandoned}) "
            f"exceeds total_containers({total})"
        )
