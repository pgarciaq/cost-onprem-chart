"""E2E tests for ROS GPU recommendation summary and list APIs."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from suites.ros.test_container_detail import _container_detail_url

GPU_MIG_NOTIFICATION_CODES = frozenset({10, 26, 27, 28})
GPU_SETTINGS_THRESHOLD_KEYS = frozenset(
    {
        "idle_threshold",
        "underutilized_sm_threshold",
        "fb_headroom_factor",
        "mig_fb_percentile",
    }
)


def _gpu_base(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/gpu"
    )


def _fetch_gpu(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    path: str = "",
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    url = _gpu_base(ros_api_url)
    if path:
        url = f"{url}/{path.lstrip('/')}"
    return session.get(url, headers=auth, params=params or {}, timeout=60)


def _gpu_settings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/gpu"
    )


@pytest.fixture
def gpu_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestGPURecommendationsE2E:
    """GPU summary, time-slicing, MIG, and container enrichment."""

    def test_gpu_summary_returns_200(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled (404 on /gpu)")
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json(), dict)

    def test_gpu_summary_has_counts(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "timeslicing" in body
        assert "mig" in body
        assert "count" in body["timeslicing"]
        assert "count" in body["mig"]
        assert isinstance(body["timeslicing"]["count"], int)
        assert isinstance(body["mig"]["count"], int)
        assert body["timeslicing"]["count"] >= 0
        assert body["mig"]["count"] >= 0

    def test_gpu_timeslicing_list_returns_200(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert summary.status_code == 200, summary.text
        if summary.json().get("timeslicing", {}).get("count", 0) == 0:
            pytest.skip("No GPU time-slicing recommendations in cluster")

        resp = _fetch_gpu(
            http_session, ros_api_url, gpu_auth, "timeslicing", {"limit": 10}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_gpu_timeslicing_data_fields(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("timeslicing", {}).get("count", 0) == 0:
            pytest.skip("No GPU time-slicing recommendations in cluster")

        resp = _fetch_gpu(
            http_session, ros_api_url, gpu_auth, "timeslicing", {"limit": 5}
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("GPU timeslicing list returned empty data")

        item = items[0]
        assert "node_name" in item
        assert "gpu_model" in item
        utilization_fields = (
            "confidence",
            "candidate_containers",
            "impacted_containers",
        )
        assert any(field in item for field in utilization_fields), (
            "timeslicing item should expose utilization-related fields "
            f"(one of {utilization_fields})"
        )

    def test_gpu_timeslicing_filter_tag(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("timeslicing", {}).get("count", 0) == 0:
            pytest.skip("No GPU time-slicing recommendations in cluster")

        baseline = _fetch_gpu(
            http_session, ros_api_url, gpu_auth, "timeslicing", {"limit": 5}
        )
        assert baseline.status_code == 200, baseline.text
        unfiltered_count = baseline.json().get("meta", {}).get("count", 0)
        if unfiltered_count == 0:
            pytest.skip("No GPU timeslicing list data despite summary count")

        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "timeslicing",
            {"filter[tag:environment]": "production", "limit": 10},
        )
        if resp.status_code == 400:
            pytest.skip("Tag filtering not enabled or invalid tag key")
        assert resp.status_code == 200, resp.text
        filtered_count = resp.json().get("meta", {}).get("count", 0)
        assert filtered_count <= unfiltered_count
        if unfiltered_count > 0 and filtered_count == unfiltered_count:
            pytest.skip("Tag filter did not narrow GPU timeslicing results")
        if filtered_count == 0:
            pytest.skip("No GPU timeslicing rows match filter[tag:environment]=production")

    def test_gpu_mig_list_returns_200(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert summary.status_code == 200, summary.text
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        resp = _fetch_gpu(http_session, ros_api_url, gpu_auth, "mig", {"limit": 10})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body

    def test_gpu_mig_data_fields(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        resp = _fetch_gpu(http_session, ros_api_url, gpu_auth, "mig", {"limit": 5})
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("GPU MIG list returned empty data")

        item = items[0]
        assert "container" in item
        assert "namespace" in item
        assert "recommended_gpu_profile" in item

    def test_gpu_filter_by_cluster(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("timeslicing", {}).get("count", 0) == 0:
            pytest.skip("No GPU time-slicing recommendations in cluster")

        baseline = _fetch_gpu(
            http_session, ros_api_url, gpu_auth, "timeslicing", {"limit": 5}
        )
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("GPU timeslicing list returned empty data")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "timeslicing item must include cluster_uuid"

        filtered = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "timeslicing",
            {"cluster_uuid": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        filtered_items = filtered.json().get("data") or []
        if filtered_items:
            for item in filtered_items:
                assert item.get("cluster_uuid") == cluster_uuid

    def test_gpu_container_enrichment(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        ts_count = summary.json().get("timeslicing", {}).get("count", 0)
        mig_count = summary.json().get("mig", {}).get("count", 0)
        if ts_count == 0 and mig_count == 0:
            pytest.skip("No GPU recommendation data in cluster")

        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=gpu_auth,
            params={"has_gpu": "true", "limit": 20},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No GPU-enriched container recommendations in cluster")

        for item in items:
            gpu_block = item.get("gpu") or {}
            if not gpu_block:
                continue
            for term_gpu in gpu_block.values():
                if not isinstance(term_gpu, dict):
                    continue
                model_name = term_gpu.get("current_gpu_model")
                if model_name:
                    assert isinstance(model_name, str)
                    assert model_name.strip()
                    return
        pytest.skip("Container list has_gpu rows but none expose current_gpu_model")

    def test_gpu_filter_tag(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        mig_count = summary.json().get("mig", {}).get("count", 0)
        if mig_count == 0:
            pytest.skip("No GPU MIG recommendation data in cluster")

        baseline = _fetch_gpu(
            http_session, ros_api_url, gpu_auth, "mig", {"limit": 5},
        )
        assert baseline.status_code == 200, baseline.text
        unfiltered_count = baseline.json().get("meta", {}).get("count", 0)
        if unfiltered_count == 0:
            pytest.skip("No GPU MIG list data despite summary count")

        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"filter[tag:environment]": "production", "limit": 10},
        )
        if resp.status_code == 400:
            pytest.skip("Tag filtering not enabled or invalid tag key")
        assert resp.status_code == 200, resp.text
        filtered = resp.json()
        filtered_count = filtered.get("meta", {}).get("count", 0)
        assert filtered_count <= unfiltered_count
        if unfiltered_count > 0 and filtered_count == unfiltered_count:
            pytest.skip("Tag filter did not narrow GPU MIG results")
        if filtered_count == 0:
            pytest.skip("No GPU MIG rows match filter[tag:environment]=production")

    def test_gpu_mig_order_by_confidence(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"order_by": "confidence", "order_how": "desc", "limit": 50},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if len(items) < 2:
            pytest.skip("Need at least two MIG rows to verify sort order")
        for i in range(1, len(items)):
            prev_conf = items[i - 1].get("confidence")
            curr_conf = items[i].get("confidence")
            assert prev_conf is not None and curr_conf is not None
            assert prev_conf >= curr_conf

    def test_gpu_mig_filter_project(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        baseline = _fetch_gpu(http_session, ros_api_url, gpu_auth, "mig", {"limit": 10})
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("GPU MIG list returned empty data")

        project = items[0].get("namespace")
        assert project, "MIG row must include namespace"

        filtered = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"filter[project]": project, "limit": 50},
        )
        assert filtered.status_code == 200, filtered.text
        filtered_items = filtered.json().get("data") or []
        assert filtered_items, f"Expected MIG rows for project {project}"
        for item in filtered_items:
            assert item.get("namespace") == project

    def test_gpu_mig_csv_export(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"format": "csv", "limit": 100},
        )
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type
        body = resp.text.strip()
        assert body
        assert "cluster_uuid" in body.splitlines()[0]

    def test_gpu_mig_rbac_unauthorized_cluster(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        """filter[cluster] for an unknown cluster returns empty (RBAC-safe empty list)."""
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")

        denied_cluster = "00000000-0000-0000-0000-000000000099"
        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"filter[cluster]": denied_cluster, "limit": 20},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("meta", {}).get("count", -1) == 0
        assert body.get("data") == []

    def test_gpu_mig_filter_cluster(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        baseline = _fetch_gpu(http_session, ros_api_url, gpu_auth, "mig", {"limit": 10})
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("GPU MIG list returned empty data")

        cluster_uuid = items[0].get("cluster_uuid")
        assert cluster_uuid, "MIG row must include cluster_uuid"

        filtered = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"filter[cluster]": cluster_uuid, "limit": 50},
        )
        assert filtered.status_code == 200, filtered.text
        filtered_items = filtered.json().get("data") or []
        assert filtered_items, f"Expected MIG rows for cluster {cluster_uuid}"
        for item in filtered_items:
            assert item.get("cluster_uuid") == cluster_uuid

    def test_gpu_mig_filter_idle_state(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_auth,
            "mig",
            {"filter[gpu_idle_state]": "idle", "limit": 20},
        )
        assert resp.status_code == 200, resp.text

    def test_gpu_mig_settings(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")

        resp = http_session.get(
            _gpu_settings_url(ros_api_url),
            headers=gpu_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        missing = GPU_SETTINGS_THRESHOLD_KEYS - set(body.keys())
        assert not missing, f"Missing GPU settings keys {missing}: {body}"

    def test_gpu_mig_pagination(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        resp = _fetch_gpu(http_session, ros_api_url, gpu_auth, "mig", {"limit": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("meta", {}).get("count", 0) > 0
        data = body.get("data") or []
        assert len(data) == 1

    def test_gpu_mig_notifications_on_detail(
        self,
        ros_api_url: str,
        gpu_auth: dict,
        http_session: requests.Session,
    ):
        summary = _fetch_gpu(http_session, ros_api_url, gpu_auth)
        if summary.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        if summary.json().get("mig", {}).get("count", 0) == 0:
            pytest.skip("No GPU MIG recommendations in cluster")

        mig_resp = _fetch_gpu(http_session, ros_api_url, gpu_auth, "mig", {"limit": 5})
        assert mig_resp.status_code == 200, mig_resp.text
        mig_items = mig_resp.json().get("data") or []
        if not mig_items:
            pytest.skip("GPU MIG list returned empty data")

        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=gpu_auth,
            params={"has_gpu": "true", "limit": 50},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        containers = list_resp.json().get("data") or []
        if not containers:
            pytest.skip("No GPU-enriched container recommendations in cluster")

        mig_keys = {
            (
                row.get("cluster_uuid"),
                row.get("namespace"),
                row.get("workload"),
                row.get("container"),
            )
            for row in mig_items
        }
        detail_id = None
        for item in containers:
            key = (
                item.get("cluster_uuid"),
                item.get("project"),
                item.get("workload"),
                item.get("container"),
            )
            if key in mig_keys:
                detail_id = item.get("id")
                break
        if not detail_id:
            pytest.skip("No container list row matches a GPU MIG recommendation")

        detail_resp = http_session.get(
            _container_detail_url(ros_api_url, detail_id),
            headers=gpu_auth,
            timeout=60,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        detail = detail_resp.json()
        gpu_block = detail.get("gpu") or {}
        if not gpu_block:
            pytest.skip("Container detail missing gpu enrichment block")

        found_codes: set[int] = set()
        for term_gpu in gpu_block.values():
            if not isinstance(term_gpu, dict):
                continue
            for code in term_gpu.get("notifications") or []:
                found_codes.add(int(code))

        assert found_codes & GPU_MIG_NOTIFICATION_CODES, (
            f"Expected GPU MIG notification codes in gpu.*.notifications, got {found_codes}"
        )
