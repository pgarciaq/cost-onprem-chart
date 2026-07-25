"""E2E tests for ROS idle-detection list and savings APIs.

Validates filter[category], filter[gpu_idle_state], namespace category filters,
group_by[idle_state], order_by category fields, and response shape.
"""

from __future__ import annotations

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import parse_savings_value


def _savings_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/savings-summary"
    )


def _namespaces_endpoint(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/namespaces"
    )


_IDLE_RESPONSE_FIELDS = (
    "category",
    "idle_since",
    "idle_duration_days",
    "estimated_monthly_waste",
)


@pytest.fixture
def idle_flow_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.component
class TestIdleDetectionFlowE2E:
    """Idle/zombie list filters and savings grouping against a deployed cluster."""

    def test_container_list_filter_idle_and_zombie(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """filter[category]=zombie,idle returns rows where each has category in (idle, zombie)."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[category]": "zombie,idle", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        items = body["data"]
        assert isinstance(items, list)
        if not items:
            pytest.skip("No idle or zombie containers on cluster")
        for item in items:
            assert item.get("category") in ("idle", "zombie"), (
                f"Expected category in (idle, zombie), got {item.get('category')!r}"
            )

    def test_container_list_filter_active(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """filter[category]=optimized returns rows where each has category=optimized."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[category]": "optimized", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body.get("data") or []
        if not items:
            pytest.skip("No optimized containers on cluster")
        for item in items:
            assert item.get("category") == "optimized", (
                f"Expected category=optimized, got {item.get('category')!r}"
            )

    def test_savings_summary_group_by_idle_state(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=idle_flow_auth,
            params={"group_by[idle_state]": "*"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        assert isinstance(body["data"], list)
        if body["data"]:
            row = body["data"][0]
            assert "category" in row

    def test_container_list_order_by_idle_duration_days(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """order_by=idle_duration_days desc returns items in non-increasing order."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"order_by": "idle_duration_days", "order_how": "desc", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No containers on cluster to verify sort")
        durations = [item.get("idle_duration_days") or 0 for item in items]
        if len(durations) > 1:
            for i in range(len(durations) - 1):
                assert durations[i] >= durations[i + 1], (
                    f"Sort violated at index {i}: {durations[i]} < {durations[i+1]}"
                )

    def test_idle_response_fields(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        data: list = []
        for state_filter in ("idle", "zombie", "idle,zombie"):
            resp = http_session.get(
                endpoint,
                headers=idle_flow_auth,
                params={"filter[category]": state_filter, "limit": 1},
                timeout=60,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json().get("data") or []
            if data:
                break
        if not data:
            pytest.skip("No idle or zombie containers on cluster")
        row = data[0]
        for field in _IDLE_RESPONSE_FIELDS:
            assert field in row, f"Missing {field} on idle row: {row.keys()}"

    def test_idle_order_by_category(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """order_by=category returns 200 and items have category field."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"order_by": "category", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No containers on cluster to verify category sort")
        for item in items:
            assert "category" in item, f"Missing category field: {list(item.keys())}"

    def test_idle_order_by_estimated_monthly_waste(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """order_by=estimated_monthly_waste desc returns items in non-increasing order."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"order_by": "estimated_monthly_waste", "order_how": "desc", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No containers on cluster to verify waste sort")
        wastes = [
            parse_savings_value(item.get("estimated_monthly_waste")) or 0.0
            for item in items
        ]
        if len(wastes) > 1:
            for i in range(len(wastes) - 1):
                assert wastes[i] >= wastes[i + 1], (
                    f"Sort violated at index {i}: {wastes[i]} < {wastes[i + 1]}"
                )

    def test_namespace_filter_category(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """Namespace endpoint accepts filter[category] and returns matching rows."""
        resp = http_session.get(
            _namespaces_endpoint(ros_api_url),
            headers=idle_flow_auth,
            params={"filter[category]": "idle", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        items = body["data"]
        assert isinstance(items, list)
        if not items:
            pytest.skip("No idle namespaces on cluster")
        for item in items:
            assert "project" in item, (
                f"Namespace row missing project field: {list(item.keys())}"
            )
            category = item.get("category")
            if category is not None:
                assert category == "idle", (
                    f"Expected category=idle, got {category!r}"
                )

    def test_gpu_idle_state_filter(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        """filter[gpu_idle_state]=idle,zombie returns GPU containers with matching state."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[gpu_idle_state]": "idle,zombie", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No GPU idle/zombie containers on cluster")
        for item in items:
            gpu = item.get("gpu") or {}
            assert gpu.get("gpu_idle_state") in ("idle", "zombie"), (
                f"Expected gpu_idle_state in (idle, zombie), got {gpu.get('gpu_idle_state')!r}"
            )
