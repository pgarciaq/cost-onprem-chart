"""E2E tests for ROS node CPU/memory utilization recommendations."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token


def _nodes_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/nodes"
    )


def _fresh_auth(
    keycloak_config, cluster_config, http_session: requests.Session
) -> dict[str, str]:
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.fail("Could not obtain JWT token")
    return auth


def _fetch_nodes(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _nodes_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _node_medium_engines(item: dict[str, Any]) -> dict[str, Any]:
    terms = item.get("recommendation_terms") or {}
    medium = terms.get("medium_term") or {}
    engines = medium.get("recommendation_engines") or {}
    return engines


def _engine_sizing(engine: Optional[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    if not engine:
        return None, None
    return engine.get("recommended_cpu_cores"), engine.get("recommended_memory_gib")


@pytest.fixture
def node_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestNodeRecommendationsE2E:
    """Node utilization recommendation list API."""

    def test_nodes_list_returns_200(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 10})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled (404 on /nodes)")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_nodes_data_has_expected_fields(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 5})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No node recommendation data in cluster")

        item = body["data"][0]
        assert "node" in item
        assert "cluster_uuid" in item
        assert "recommendation_type" in item
        assert "recommendation_terms" in item
        assert isinstance(item["recommendation_terms"], dict)

    def test_nodes_filter_by_engine_cost(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session, ros_api_url, node_auth, {"engine": "cost", "limit": 5}
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text

    def test_nodes_filter_by_engine_performance(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"engine": "performance", "limit": 5},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text

    def test_nodes_savings_fields_present(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 10})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No node recommendation data in cluster")

        found_savings_key = False
        for item in body["data"]:
            engines = _node_medium_engines(item)
            for engine_name in ("cost", "performance"):
                engine = engines.get(engine_name)
                if engine and "estimated_monthly_savings_usd" in engine:
                    found_savings_key = True
                    savings = engine["estimated_monthly_savings_usd"]
                    if savings is not None:
                        assert savings >= 0
        assert found_savings_key, (
            "expected estimated_monthly_savings_usd on at least one engine block"
        )

    def test_nodes_dual_engine_divergence(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 50})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No node recommendation data in cluster")

        for item in body["data"]:
            engines = _node_medium_engines(item)
            cost = engines.get("cost")
            perf = engines.get("performance")
            if not cost or not perf:
                continue
            cost_cpu, cost_mem = _engine_sizing(cost)
            perf_cpu, perf_mem = _engine_sizing(perf)
            if cost_cpu is None or perf_cpu is None:
                continue
            if cost_cpu != perf_cpu or cost_mem != perf_mem:
                return
        pytest.skip(
            "Cost and performance engines returned identical sizing for all sampled nodes"
        )

    def test_nodes_pagination(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        first = _fetch_nodes(
            http_session, ros_api_url, node_auth, {"limit": 2, "offset": 0}
        )
        if first.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert first.status_code == 200, first.text
        meta = first.json().get("meta", {})
        total = meta.get("count", 0)
        if total == 0:
            pytest.skip("No node recommendation data in cluster")
        if total <= 2:
            pytest.skip("Need more than two node recommendations to test pagination")

        second = _fetch_nodes(
            http_session, ros_api_url, node_auth, {"limit": 2, "offset": 2}
        )
        assert second.status_code == 200, second.text
        page1_ids = {(r["node"], r["cluster_uuid"]) for r in first.json()["data"]}
        page2_ids = {(r["node"], r["cluster_uuid"]) for r in second.json()["data"]}
        assert page1_ids.isdisjoint(page2_ids), "paginated pages must not overlap"

    def test_nodes_filter_by_cluster(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 5})
        if baseline.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No node recommendation data in cluster")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "node item must include cluster_uuid"

        filtered = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"cluster_uuid": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_uuid") == cluster_uuid
