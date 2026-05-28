"""E2E tests for ROS OpenShift quota recommendation list API.

Endpoint: GET /api/cost-management/v1/recommendations/openshift/quota/

Requires ros-ocp-backend quota plugin enabled (Helm values.ros.api.enabledPlugins).
When the plugin is disabled the API returns 404 and tests skip.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from utils import assert_structured_savings, parse_savings_value

from suites.ros.test_recommendations import get_fresh_token

VALID_QUOTA_RECOMMENDATION_TYPES = frozenset({"tighten", "raise", "optimal"})
VALID_QUOTA_RISK_LEVELS = frozenset({"high", "medium", "low", "none"})


def _quota_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/quota"
    )


def _fetch_quota(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _quota_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _skip_if_plugin_disabled(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip("Quota recommendations plugin not enabled (404 on /quota)")


@pytest.fixture
def quota_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestQuotaRecommendationsE2E:
    """Quota recommendation list endpoint against a deployed cluster."""

    def test_quota_list_returns_200(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 10})
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_quota_list_data_fields(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 5})
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No quota recommendation data in cluster")

        item = body["data"][0]
        assert item.get("namespace"), "quota row must include namespace"
        assert item.get("cluster_uuid"), "quota row must include cluster_uuid"
        assert item.get("recommendation_type") in VALID_QUOTA_RECOMMENDATION_TYPES
        assert item.get("risk_level") in VALID_QUOTA_RISK_LEVELS

        for block_name in ("quota_hard", "quota_used", "quota_recommended"):
            block = item.get(block_name)
            if block is None:
                continue
            assert isinstance(block, dict), f"{block_name} must be an object"
            for key in (
                "cpu_request_millicores",
                "cpu_limit_millicores",
                "memory_request_bytes",
                "memory_limit_bytes",
            ):
                if key in block:
                    assert isinstance(block[key], int), f"{block_name}.{key} must be int"

    def test_quota_filter_by_cluster(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 5})
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No quota recommendation data in cluster")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "quota row must include cluster_uuid for filter test"

        filtered = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"cluster": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_uuid") == cluster_uuid

    def test_quota_filter_by_project(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 5})
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No quota recommendation data in cluster")

        namespace = items[0].get("namespace")
        assert namespace, "quota row must include namespace for filter test"

        filtered = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"project": namespace, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("namespace") == namespace

    def test_quota_savings_when_present(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 20})
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No quota recommendation data in cluster")

        tighten_rows = [
            item for item in items if item.get("recommendation_type") == "tighten"
        ]
        if not tighten_rows:
            pytest.skip("No tighten quota rows with potential savings")

        saw_savings = False
        for item in tighten_rows:
            savings_obj = item.get("estimated_savings")
            if savings_obj is None:
                continue
            saw_savings = True
            assert_structured_savings(savings_obj)
            savings = parse_savings_value(savings_obj)
            assert savings is not None and savings >= 0

        if not saw_savings:
            pytest.skip("No estimated_savings on tighten quota rows")

    def test_quota_filter_empty_for_unknown_cluster(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {
                "cluster": "00000000-0000-0000-0000-000000000000",
                "limit": 10,
            },
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("meta", {}).get("count", 0) == 0
        assert body.get("data") == []

    def test_quota_pagination(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        first = _fetch_quota(
            http_session, ros_api_url, quota_auth, {"limit": 2, "offset": 0}
        )
        _skip_if_plugin_disabled(first)
        assert first.status_code == 200, first.text
        total = first.json().get("meta", {}).get("count", 0)
        if total == 0:
            pytest.skip("No quota recommendation data in cluster")
        if total <= 2:
            pytest.skip("Need more than two quota recommendations for pagination")

        second = _fetch_quota(
            http_session, ros_api_url, quota_auth, {"limit": 2, "offset": 2}
        )
        assert second.status_code == 200, second.text
        page1_keys = {
            (i["cluster_uuid"], i["namespace"]) for i in first.json()["data"]
        }
        page2_keys = {
            (i["cluster_uuid"], i["namespace"]) for i in second.json()["data"]
        }
        assert page1_keys.isdisjoint(page2_keys)
