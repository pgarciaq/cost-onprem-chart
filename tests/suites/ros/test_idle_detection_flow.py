"""E2E tests for ROS idle-detection list and savings APIs.

Validates filter[idle_state], filter[gpu_idle_state], namespace idle filters,
group_by[idle_state], order_by idle fields, and response shape (no specific idle rows required).
"""

from __future__ import annotations

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint


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
    "idle_state",
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
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[idle_state]": "zombie,idle", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        assert isinstance(body["data"], list)

    def test_container_list_filter_active(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[idle_state]": "active", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body.get("data"), list)

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
            assert "idle_state" in row

    def test_container_list_order_by_idle_duration_days(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"order_by": "idle_duration_days", "order_how": "desc", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body.get("data"), list)

    def test_idle_response_fields(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[idle_state]": "idle,zombie", "limit": 1},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json().get("data") or []
        if not data:
            return
        row = data[0]
        for field in _IDLE_RESPONSE_FIELDS:
            assert field in row, f"Missing {field} on idle row: {row.keys()}"

    def test_idle_order_by_idle_state(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"order_by": "idle_state", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json().get("data"), list)

    def test_idle_order_by_estimated_monthly_waste(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"order_by": "estimated_monthly_waste", "order_how": "desc", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json().get("data"), list)

    def test_namespace_filter_idle_state(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _namespaces_endpoint(ros_api_url),
            headers=idle_flow_auth,
            params={"filter[idle_state]": "idle", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_gpu_idle_state_filter(
        self,
        ros_api_url: str,
        idle_flow_auth: dict,
        http_session: requests.Session,
    ):
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(
            endpoint,
            headers=idle_flow_auth,
            params={"filter[gpu_idle_state]": "idle,zombie", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json().get("data"), list)
