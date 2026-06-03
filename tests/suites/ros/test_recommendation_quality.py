"""E2E tests for ROS recommendation quality metrics API."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token


def _quality_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/quality"
    )


def _fetch_quality(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _quality_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


@pytest.fixture
def quality_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestRecommendationQualityE2E:
    """Recommendation quality metrics list and CSV export."""

    def test_quality_returns_200(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quality(http_session, ros_api_url, quality_auth, {"limit": 10})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_quality_data_fields(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quality(http_session, ros_api_url, quality_auth, {"limit": 5})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No recommendation quality records in cluster")

        row = body["data"][0]
        assert "measured_at" in row
        assert "stability_pct" in row
        assert "adoption_detected" in row
        assert isinstance(row["adoption_detected"], bool)

    def test_quality_stability_range(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quality(http_session, ros_api_url, quality_auth, {"limit": 20})
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("No recommendation quality records in cluster")

        checked = False
        for row in rows:
            stability = row.get("stability_pct")
            if stability is None:
                continue
            checked = True
            assert 0 <= stability <= 1, f"stability_pct out of range (expected 0.0-1.0): {stability}"
        assert checked, "expected at least one row with non-null stability_pct"

    def test_quality_filter_by_cluster(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_quality(http_session, ros_api_url, quality_auth, {"limit": 5})
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No recommendation quality records in cluster")

        cluster = items[0].get("cluster_alias") or items[0].get("cluster_uuid")
        assert cluster, "quality row must include cluster identifier"

        filtered = _fetch_quality(
            http_session,
            ros_api_url,
            quality_auth,
            {"cluster": cluster, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert (
                row.get("cluster_alias") == cluster or row.get("cluster_uuid") == cluster
            )

    def test_quality_filter_by_container(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_quality(http_session, ros_api_url, quality_auth, {"limit": 5})
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No recommendation quality records in cluster")

        container = items[0].get("container_name")
        assert container, "quality row must include container_name"

        filtered = _fetch_quality(
            http_session,
            ros_api_url,
            quality_auth,
            {"container": container, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("container_name") == container

    def test_quality_filter_engine(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        for engine in ("cost", "performance"):
            resp = _fetch_quality(
                http_session,
                ros_api_url,
                quality_auth,
                {"filter[engine]": engine, "limit": 5},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "meta" in body
            assert "data" in body
            if body.get("meta", {}).get("count", 0) == 0:
                continue
            for row in body.get("data") or []:
                assert row.get("engine") == engine

    def test_quality_csv_export(
        self,
        ros_api_url: str,
        quality_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_quality(
            http_session,
            ros_api_url,
            quality_auth,
            {"format": "csv", "limit": 50},
        )
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type
        body = resp.text
        assert body.startswith("measured_at,"), "CSV must include header row"
        lines = [line for line in body.strip().splitlines() if line]
        assert len(lines) >= 1
