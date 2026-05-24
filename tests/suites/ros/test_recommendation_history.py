"""E2E tests for ROS recommendation history API."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token


def _history_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/history"
    )


def _fetch_history(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _history_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


@pytest.fixture
def history_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestRecommendationHistoryE2E:
    """Recommendation history list and CSV export."""

    def test_history_returns_200(
        self,
        ros_api_url: str,
        history_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_history(http_session, ros_api_url, history_auth, {"limit": 10})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_history_data_fields(
        self,
        ros_api_url: str,
        history_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_history(http_session, ros_api_url, history_auth, {"limit": 5})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No recommendation history records in cluster")

        row = body["data"][0]
        for field in (
            "recorded_at",
            "namespace",
            "container_name",
            "term",
            "engine",
        ):
            assert field in row, f"history row missing {field!r}"
        value_fields = (
            "rec_cpu_request_millicores",
            "rec_memory_request_kib",
            "rec_cpu_limit_millicores",
            "rec_memory_limit_kib",
        )
        assert any(row.get(f) is not None for f in value_fields), (
            "history row should include at least one recommendation value field"
        )

    def test_history_filter_by_cluster(
        self,
        ros_api_url: str,
        history_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_history(http_session, ros_api_url, history_auth, {"limit": 5})
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No recommendation history records in cluster")

        cluster = items[0].get("cluster_alias") or items[0].get("cluster_uuid")
        assert cluster, "history row must include cluster identifier"

        filtered = _fetch_history(
            http_session,
            ros_api_url,
            history_auth,
            {"cluster": cluster, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert (
                row.get("cluster_alias") == cluster or row.get("cluster_uuid") == cluster
            )

    def test_history_filter_by_project(
        self,
        ros_api_url: str,
        history_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_history(http_session, ros_api_url, history_auth, {"limit": 5})
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No recommendation history records in cluster")

        project = items[0].get("namespace")
        assert project, "history row must include namespace/project"

        filtered = _fetch_history(
            http_session,
            ros_api_url,
            history_auth,
            {"project": project, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("namespace") == project

    def test_history_filter_by_engine(
        self,
        ros_api_url: str,
        history_auth: dict,
        http_session: requests.Session,
    ):
        for engine in ("cost", "performance"):
            resp = _fetch_history(
                http_session,
                ros_api_url,
                history_auth,
                {"engine": engine, "limit": 10},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            if body.get("meta", {}).get("count", 0) == 0:
                continue
            for row in body.get("data") or []:
                assert row.get("engine") == engine
            return
        pytest.skip("No recommendation history records for cost or performance engine")

    def test_history_csv_export(
        self,
        ros_api_url: str,
        history_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_history(
            http_session,
            ros_api_url,
            history_auth,
            {"format": "csv", "limit": 50},
        )
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type
        body = resp.text
        assert body.startswith("recorded_at,"), "CSV must include header row"
        lines = [line for line in body.strip().splitlines() if line]
        assert len(lines) >= 1
