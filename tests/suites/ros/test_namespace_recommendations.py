"""E2E tests for ROS namespace recommendation list and detail APIs."""

from __future__ import annotations

import uuid
from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token


def _namespaces_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/namespaces"
    )


def _namespace_detail_url(ros_api_url: str, recommendation_id: str) -> str:
    return f"{_namespaces_url(ros_api_url)}/{recommendation_id}"


def _fetch_namespaces(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _namespaces_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


@pytest.fixture
def namespace_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestNamespaceRecommendationsE2E:
    """Namespace recommendation list and detail endpoints."""

    def test_namespace_list_returns_200(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_namespaces(http_session, ros_api_url, namespace_auth, {"limit": 10})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_namespace_list_data_fields(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_namespaces(http_session, ros_api_url, namespace_auth, {"limit": 5})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No namespace recommendation data in cluster")

        item = body["data"][0]
        namespace = item.get("project") or item.get("namespace")
        cluster = item.get("cluster_uuid") or item.get("cluster")
        assert namespace, "namespace list item must include project/namespace"
        assert cluster, "namespace list item must include cluster_uuid/cluster"
        recs = item.get("recommendations") or {}
        assert "recommendation_terms" in recs
        assert isinstance(recs["recommendation_terms"], dict)

    def test_namespace_detail_valid_id(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 1}
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No namespace recommendation data in cluster")

        rec_id = items[0].get("id")
        assert rec_id, "namespace list item must include id"

        detail_resp = http_session.get(
            _namespace_detail_url(ros_api_url, rec_id),
            headers=namespace_auth,
            timeout=60,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        body = detail_resp.json()
        assert body.get("id") == rec_id
        recs = body.get("recommendations") or {}
        assert "recommendation_terms" in recs

    def test_namespace_detail_not_found_returns_404(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        missing_id = str(uuid.uuid4())
        resp = http_session.get(
            _namespace_detail_url(ros_api_url, missing_id),
            headers=namespace_auth,
            timeout=60,
        )
        assert resp.status_code == 404, resp.text

    def test_namespace_filter_by_cluster(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 5}
        )
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No namespace recommendation data in cluster")

        cluster = items[0].get("cluster_uuid") or items[0].get("cluster_alias")
        if not cluster:
            pytest.skip("Namespace item missing cluster identifier for filter test")

        filtered = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"cluster": cluster, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        assert isinstance(filtered.json().get("data"), list)

    def test_namespace_pagination(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        first = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 2, "offset": 0}
        )
        assert first.status_code == 200, first.text
        total = first.json().get("meta", {}).get("count", 0)
        if total == 0:
            pytest.skip("No namespace recommendation data in cluster")
        if total <= 2:
            pytest.skip("Need more than two namespace recommendations for pagination")

        second = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 2, "offset": 2}
        )
        assert second.status_code == 200, second.text
        page1_ids = {item.get("id") for item in first.json()["data"]}
        page2_ids = {item.get("id") for item in second.json()["data"]}
        assert page1_ids.isdisjoint(page2_ids)

    def test_namespace_filter_by_engine(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"engine": "cost", "limit": 5},
        )
        assert resp.status_code == 200, resp.text
        assert "data" in resp.json()
