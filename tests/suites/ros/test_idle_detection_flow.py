"""E2E tests for ROS idle-detection list and savings APIs.

Validates filter[idle_state], group_by[idle_state], and order_by=idle_duration_days
return 200 with well-formed JSON (no specific idle rows required).
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
