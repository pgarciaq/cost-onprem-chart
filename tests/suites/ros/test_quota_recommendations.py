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

VALID_QUOTA_RECOMMENDATION_TYPES = frozenset({"tighten", "raise", "optimal", "none"})
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
        if item.get("quota_name") is not None:
            assert isinstance(item["quota_name"], str), "quota_name must be a string"

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
                "storage_request_bytes",
                "pods",
            ):
                if key in block:
                    assert isinstance(block[key], int), f"{block_name}.{key} must be int"

        capacity_freed = item.get("capacity_freed")
        if capacity_freed is not None:
            assert isinstance(capacity_freed, dict), "capacity_freed must be an object"
            for key in (
                "cpu_millicores",
                "memory_bytes",
                "storage_request_bytes",
                "pods_freed",
            ):
                if key in capacity_freed:
                    assert isinstance(capacity_freed[key], int), (
                        f"capacity_freed.{key} must be int"
                    )
                    assert capacity_freed[key] >= 0, (
                        f"capacity_freed.{key} must be non-negative"
                    )

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

    def test_quota_capacity_freed_on_tighten(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 50})
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No quota recommendation data in cluster")

        tighten_rows = [
            item for item in items if item.get("recommendation_type") == "tighten"
        ]
        if not tighten_rows:
            pytest.skip("No tighten quota rows with capacity_freed data")

        saw_capacity = False
        for item in tighten_rows:
            capacity_freed = item.get("capacity_freed")
            if not capacity_freed:
                continue
            saw_capacity = True
            assert isinstance(capacity_freed.get("cpu_millicores"), int)
            assert isinstance(capacity_freed.get("memory_bytes"), int)
            assert capacity_freed["cpu_millicores"] >= 0
            assert capacity_freed["memory_bytes"] >= 0
            if "storage_request_bytes" in capacity_freed:
                assert isinstance(capacity_freed["storage_request_bytes"], int)
                assert capacity_freed["storage_request_bytes"] >= 0
            if "pods_freed" in capacity_freed:
                assert isinstance(capacity_freed["pods_freed"], int)
                assert capacity_freed["pods_freed"] >= 0

        if not saw_capacity:
            pytest.skip("No capacity_freed on tighten quota rows")

    def test_quota_filter_by_quota_name(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 20})
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No quota recommendation data in cluster")

        quota_name = None
        for item in items:
            name = item.get("quota_name")
            if name:
                quota_name = name
                break
        if not quota_name:
            pytest.skip("No quota rows with quota_name populated")

        filtered = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"filter[quota_name]": quota_name, "limit": 50},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("quota_name") == quota_name

    def test_quota_filter_resource_quota_name_alias(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 20})
        _skip_if_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No quota recommendation data in cluster")

        quota_name = None
        for item in items:
            name = item.get("quota_name")
            if name:
                quota_name = name
                break
        if not quota_name:
            pytest.skip("No quota rows with quota_name populated")

        by_name = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"filter[quota_name]": quota_name, "limit": 50},
        )
        by_alias = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"filter[resource_quota_name]": quota_name, "limit": 50},
        )
        assert by_name.status_code == 200, by_name.text
        assert by_alias.status_code == 200, by_alias.text
        page1_keys = {
            (i["cluster_uuid"], i["namespace"], i.get("quota_name", ""))
            for i in by_name.json().get("data") or []
        }
        page2_keys = {
            (i["cluster_uuid"], i["namespace"], i.get("quota_name", ""))
            for i in by_alias.json().get("data") or []
        }
        assert page1_keys == page2_keys

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

    def test_quota_order_by_quota_name(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"limit": 20, "order_by": "quota_name", "order_how": "asc"},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if len(items) < 2:
            pytest.skip("Need multiple quota rows for order_by test")
        names = [i.get("quota_name") or "" for i in items]
        assert names == sorted(names)

    def test_quota_filter_recommendation_type_tighten(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"filter[recommendation_type]": "tighten", "limit": 50},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No tighten quota rows in cluster")
        for item in items:
            assert item.get("recommendation_type") == "tighten"

    def test_quota_group_by_cluster(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"group_by[cluster]": "*", "limit": 20},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No quota recommendation data in cluster")

        for item in body.get("data") or []:
            assert item.get("cluster_uuid"), "group_by row must include cluster_uuid"
            assert item.get("count", 0) >= 1, "group_by row must include aggregated count"

    def test_quota_group_by_project(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"group_by[project]": "*", "limit": 20},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No quota recommendation data in cluster")

        for item in body.get("data") or []:
            assert item.get("namespace"), "group_by row must include namespace"
            assert item.get("count", 0) >= 1, "group_by row must include aggregated count"

    def test_quota_notification_codes_filter(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        """GET notification-codes?filter[plugin]=quota returns codes 70-72."""
        baseline = _fetch_quota(http_session, ros_api_url, quota_auth, {"limit": 1})
        _skip_if_plugin_disabled(baseline)

        url = (
            f"{ros_api_url.rstrip('/')}/cost-management/v1/"
            "recommendations/openshift/notification-codes"
        )
        resp = http_session.get(
            url,
            headers=quota_auth,
            params={"filter[plugin]": "quota"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        codes = {entry["code"] for entry in body["data"]}
        assert codes == {70, 71, 72}

    def test_quota_filter_recommendation_type_none(
        self,
        ros_api_url: str,
        quota_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quota(
            http_session,
            ros_api_url,
            quota_auth,
            {"filter[recommendation_type]": "none", "limit": 50},
        )
        _skip_if_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_quota_detail_endpoint(
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

        row = items[0]
        cluster_uuid = row["cluster_uuid"]
        namespace = row["namespace"]
        params: dict[str, str] = {
            "cluster_uuid": cluster_uuid,
            "namespace": namespace,
        }
        if row.get("quota_name"):
            params["quota_name"] = row["quota_name"]

        detail_url = ros_api_url.rstrip("/").replace(
            "/recommendations/openshift/quota",
            "/recommendations/openshift/quota/detail",
        )
        detail = http_session.get(
            detail_url,
            headers=quota_auth,
            params=params,
            timeout=60,
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body.get("cluster_uuid") == cluster_uuid
        assert body.get("namespace") == namespace
        assert body.get("recommendation_type") in VALID_QUOTA_RECOMMENDATION_TYPES
        assert body.get("risk_level") in VALID_QUOTA_RISK_LEVELS
        assert "history" in body
        assert isinstance(body["history"], list)
        if body["history"]:
            entry = body["history"][0]
            assert entry.get("recorded_at")
            assert entry.get("resource")
            assert entry.get("recommendation_type") in VALID_QUOTA_RECOMMENDATION_TYPES
            assert entry.get("risk_level") in VALID_QUOTA_RISK_LEVELS
