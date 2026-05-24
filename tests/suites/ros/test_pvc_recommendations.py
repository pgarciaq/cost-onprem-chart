"""E2E tests for ROS PVC/storage recommendations."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token


def _pvcs_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/pvcs"
    )


def _fetch_pvcs(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _pvcs_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


@pytest.fixture
def pvc_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestPVCRecommendationsE2E:
    """PVC recommendation list API."""

    def test_pvc_list_returns_200(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 10})
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled (404 on /pvcs)")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_pvc_list_data_fields(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No PVC recommendation data in cluster")

        item = body["data"][0]
        assert "namespace" in item
        assert "persistentvolumeclaim" in item
        assert "capacity_bytes" in item
        assert isinstance(item["capacity_bytes"], int)
        assert item["capacity_bytes"] >= 0

    def test_pvc_filter_by_cluster(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        if baseline.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No PVC recommendation data in cluster")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "PVC item must include cluster_uuid"

        filtered = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"cluster_uuid": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_uuid") == cluster_uuid

    def test_pvc_filter_by_namespace(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 5})
        if baseline.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No PVC recommendation data in cluster")

        namespace = items[0].get("namespace")
        assert namespace, "PVC item must include namespace"

        filtered = _fetch_pvcs(
            http_session,
            ros_api_url,
            pvc_auth,
            {"namespace": namespace, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("namespace") == namespace

    def test_pvc_savings_non_negative(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_pvcs(http_session, ros_api_url, pvc_auth, {"limit": 20})
        if resp.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No PVC recommendation data in cluster")

        saw_savings = False
        for item in items:
            savings = item.get("estimated_monthly_savings_usd")
            if savings is None:
                continue
            saw_savings = True
            assert savings >= 0, f"negative savings on PVC {item.get('persistentvolumeclaim')}"
        if not saw_savings:
            # Savings may be absent/null for non-actionable PVC rows; field presence is optional.
            assert "estimated_monthly_savings_usd" in items[0]

    def test_pvc_pagination(
        self,
        ros_api_url: str,
        pvc_auth: dict,
        http_session: requests.Session,
    ):
        first = _fetch_pvcs(
            http_session, ros_api_url, pvc_auth, {"limit": 2, "offset": 0}
        )
        if first.status_code == 404:
            pytest.skip("PVC recommendations plugin not enabled")
        assert first.status_code == 200, first.text
        total = first.json().get("meta", {}).get("count", 0)
        if total == 0:
            pytest.skip("No PVC recommendation data in cluster")
        if total <= 2:
            pytest.skip("Need more than two PVC recommendations for pagination")

        second = _fetch_pvcs(
            http_session, ros_api_url, pvc_auth, {"limit": 2, "offset": 2}
        )
        assert second.status_code == 200, second.text
        page1_keys = {
            (i["cluster_uuid"], i["namespace"], i["persistentvolumeclaim"])
            for i in first.json()["data"]
        }
        page2_keys = {
            (i["cluster_uuid"], i["namespace"], i["persistentvolumeclaim"])
            for i in second.json()["data"]
        }
        assert page1_keys.isdisjoint(page2_keys)
