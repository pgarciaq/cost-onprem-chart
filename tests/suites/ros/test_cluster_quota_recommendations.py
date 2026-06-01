"""E2E tests for ROS OpenShift ClusterResourceQuota recommendation list API.

Endpoint: GET /api/cost-management/v1/recommendations/openshift/cluster-quota/

Requires ros-ocp-backend cluster-quota plugin enabled (Helm values.ros.api.enabledPlugins).
When the plugin is disabled the API returns 404 and tests skip.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from utils import assert_structured_savings, parse_savings_value

from suites.ros.test_recommendations import get_fresh_token

VALID_CLUSTER_QUOTA_RECOMMENDATION_TYPES = frozenset({"tighten", "raise", "optimal"})
VALID_CLUSTER_QUOTA_RISK_LEVELS = frozenset({"high", "medium", "low", "none"})


def _cluster_quota_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/cluster-quota"
    )


def _fetch_cluster_quota(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _cluster_quota_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _skip_if_plugin_disabled(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip(
            "Cluster quota recommendations plugin not enabled (404 on /cluster-quota)"
        )


def _list_row_keys(body: dict) -> set[tuple[str, str]]:
    return {
        (row["cluster_uuid"], row["cluster_quota_name"])
        for row in body.get("data", [])
        if row.get("cluster_uuid") and row.get("cluster_quota_name")
    }


@pytest.fixture
def cluster_quota_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestClusterQuotaRecommendationsE2E:
    """ClusterResourceQuota recommendation list endpoint against a deployed cluster."""

    def test_cluster_quota_list_returns_200(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 10}
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_cluster_quota_list_data_fields(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 5}
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No cluster quota recommendation data in cluster")

        item = body["data"][0]
        assert item.get("cluster_quota_name"), "row must include cluster_quota_name"
        assert item.get("cluster_uuid"), "row must include cluster_uuid"
        assert item.get("recommendation_type") in VALID_CLUSTER_QUOTA_RECOMMENDATION_TYPES
        assert item.get("risk_level") in VALID_CLUSTER_QUOTA_RISK_LEVELS
        if "namespaces" in item:
            assert isinstance(item["namespaces"], list)

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

    def test_cluster_quota_filter_by_cluster(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 5}
        )
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "row must include cluster_uuid for filter test"

        filtered = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"cluster": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_uuid") == cluster_uuid

    def test_cluster_quota_filter_by_namespace(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 20}
        )
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        target_ns = None
        target_row = None
        for item in items:
            namespaces = item.get("namespaces") or []
            if namespaces:
                target_ns = namespaces[0]
                target_row = item
                break
        if not target_ns:
            pytest.skip("No cluster quota rows with namespaces membership populated")

        filtered = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"filter[namespace]": target_ns, "limit": 50},
        )
        assert filtered.status_code == 200, filtered.text
        filtered_items = filtered.json().get("data") or []
        assert filtered_items, "filter[namespace] should return at least one CRQ"
        matched = [
            row
            for row in filtered_items
            if row.get("cluster_uuid") == target_row.get("cluster_uuid")
            and row.get("cluster_quota_name") == target_row.get("cluster_quota_name")
        ]
        assert matched, (
            f"Expected CRQ {target_row.get('cluster_quota_name')} for namespace {target_ns}"
        )
        for row in filtered_items:
            assert target_ns in (row.get("namespaces") or [])

    def test_cluster_quota_filter_by_cluster_quota_name(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 5}
        )
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        crq_name = items[0].get("cluster_quota_name")
        assert crq_name, "row must include cluster_quota_name for filter test"

        filtered = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"cluster_quota_name": crq_name, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_quota_name") == crq_name

    def test_cluster_quota_filter_crq_alias_matches_cluster_quota_name(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 5}
        )
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        crq_name = items[0].get("cluster_quota_name")
        assert crq_name, "row must include cluster_quota_name for alias test"

        by_name = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"filter[cluster_quota_name]": crq_name, "limit": 50},
        )
        by_crq = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"filter[crq]": crq_name, "limit": 50},
        )
        assert by_name.status_code == 200, by_name.text
        assert by_crq.status_code == 200, by_crq.text
        assert _list_row_keys(by_name.json()) == _list_row_keys(by_crq.json())

    def test_cluster_quota_filter_project_alias_matches_namespace(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 20}
        )
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        target_ns = None
        for item in items:
            namespaces = item.get("namespaces") or []
            if namespaces:
                target_ns = namespaces[0]
                break
        if not target_ns:
            pytest.skip("No cluster quota rows with namespaces membership populated")

        by_namespace = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"filter[namespace]": target_ns, "limit": 50},
        )
        by_project = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"filter[project]": target_ns, "limit": 50},
        )
        assert by_namespace.status_code == 200, by_namespace.text
        assert by_project.status_code == 200, by_project.text
        assert _list_row_keys(by_namespace.json()) == _list_row_keys(by_project.json())

    def test_cluster_quota_savings_when_present(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 20}
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        tighten_rows = [
            item for item in items if item.get("recommendation_type") == "tighten"
        ]
        if not tighten_rows:
            pytest.skip("No tighten cluster quota rows with potential savings")

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
            pytest.skip("No estimated_savings on tighten cluster quota rows")

    def test_cluster_quota_filter_empty_for_unknown_cluster(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
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

    def test_cluster_quota_pagination(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        first = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 2, "offset": 0}
        )
        _skip_if_plugin_disabled(first)
        assert first.status_code == 200, first.text
        total = first.json().get("meta", {}).get("count", 0)
        if total == 0:
            pytest.skip("No cluster quota recommendation data in cluster")
        if total <= 2:
            pytest.skip(
                "Need more than two cluster quota recommendations for pagination"
            )

        second = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 2, "offset": 2}
        )
        assert second.status_code == 200, second.text
        page1_keys = {
            (i["cluster_uuid"], i["cluster_quota_name"]) for i in first.json()["data"]
        }
        page2_keys = {
            (i["cluster_uuid"], i["cluster_quota_name"]) for i in second.json()["data"]
        }
        assert page1_keys.isdisjoint(page2_keys)

    def test_cluster_quota_order_by_cluster_quota_name(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"limit": 20, "order_by": "cluster_quota_name", "order_how": "asc"},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")
        names = [i.get("cluster_quota_name") for i in items]
        assert names == sorted(names)

    def test_cluster_quota_order_by_utilization_desc(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            cluster_quota_auth,
            {"limit": 20, "order_by": "utilization", "order_how": "desc"},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if len(items) < 2:
            pytest.skip("Need multiple cluster quota rows for order_by test")

        def max_util(row: dict) -> int:
            util = row.get("utilization") or {}
            vals = [
                util.get("cpu_request_percent"),
                util.get("memory_request_percent"),
                util.get("storage_request_percent"),
                util.get("pods_percent"),
            ]
            return max((v for v in vals if v is not None), default=0)

        utils = [max_util(i) for i in items]
        assert utils == sorted(utils, reverse=True)

    def test_cluster_quota_detail_endpoint(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_cluster_quota(
            http_session, ros_api_url, cluster_quota_auth, {"limit": 5}
        )
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No cluster quota recommendation data in cluster")

        row = items[0]
        cluster_uuid = row["cluster_uuid"]
        crq_name = row["cluster_quota_name"]

        detail_url = ros_api_url.rstrip("/").replace(
            "/recommendations/openshift/cluster-quota",
            "/recommendations/openshift/cluster-quota/detail",
        )
        detail = http_session.get(
            detail_url,
            headers=cluster_quota_auth,
            params={"cluster_uuid": cluster_uuid, "cluster_quota_name": crq_name},
            timeout=60,
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body.get("cluster_uuid") == cluster_uuid
        assert body.get("cluster_quota_name") == crq_name
        assert body.get("recommendation_type") in VALID_CLUSTER_QUOTA_RECOMMENDATION_TYPES
        assert body.get("risk_level") in VALID_CLUSTER_QUOTA_RISK_LEVELS
        assert "history" in body
        assert isinstance(body["history"], list)
        assert len(body["history"]) > 0, "history should contain snapshots after recommendation runs"
        entry = body["history"][0]
        assert entry.get("recorded_at")
        assert entry.get("resource")
        assert entry.get("recommendation_type") in VALID_CLUSTER_QUOTA_RECOMMENDATION_TYPES
        assert entry.get("risk_level") in VALID_CLUSTER_QUOTA_RISK_LEVELS

    def test_cluster_quota_detail_not_found(
        self,
        ros_api_url: str,
        cluster_quota_auth: dict,
        http_session: requests.Session,
    ):
        detail_url = ros_api_url.rstrip("/").replace(
            "/recommendations/openshift/cluster-quota",
            "/recommendations/openshift/cluster-quota/detail",
        )
        detail = http_session.get(
            detail_url,
            headers=cluster_quota_auth,
            params={
                "cluster_uuid": "00000000-0000-0000-0000-000000000099",
                "cluster_quota_name": "nonexistent-crq-name",
            },
            timeout=60,
        )
        _skip_if_plugin_disabled(detail)
        assert detail.status_code == 404
