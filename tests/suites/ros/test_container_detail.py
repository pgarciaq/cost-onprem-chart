"""E2E tests for ROS container recommendation list and detail APIs."""

from __future__ import annotations

import uuid
from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint


def _fresh_auth(
    keycloak_config, cluster_config, http_session: requests.Session
) -> dict[str, str]:
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.fail("Could not obtain JWT token")
    return auth


def _container_detail_url(ros_api_url: str, recommendation_id: str) -> str:
    base = get_recommendations_endpoint(ros_api_url)
    return f"{base}/{recommendation_id}"


def _assert_paginated_envelope(body: dict[str, Any]) -> None:
    assert "meta" in body, "response must include meta"
    assert "data" in body, "response must include data"
    assert "links" in body, "response must include links"
    assert isinstance(body["data"], list)
    meta = body["meta"]
    assert isinstance(meta, dict)
    assert "count" in meta


def _medium_term_engines(item: dict[str, Any]) -> dict[str, Any]:
    recs = item.get("recommendations") or {}
    terms = recs.get("recommendation_terms") or {}
    medium = terms.get("medium_term") or {}
    return medium.get("recommendation_engines") or {}


def _engine_cpu_memory(engine: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    config = engine.get("config") or {}
    requests_block = config.get("requests") or {}
    cpu = requests_block.get("cpu") or {}
    memory = requests_block.get("memory") or {}
    return cpu.get("amount"), memory.get("amount")


@pytest.fixture
def container_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestContainerDetailE2E:
    """Container recommendation list/detail and dual-engine validation."""

    def test_container_list_returns_paginated_envelope(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 5},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        _assert_paginated_envelope(resp.json())

    def test_container_detail_valid_id(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        rec_id = items[0].get("id")
        assert rec_id, "list item must include id"

        detail_resp = http_session.get(
            _container_detail_url(ros_api_url, rec_id),
            headers=container_auth,
            timeout=60,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        body = detail_resp.json()
        assert body.get("id") == rec_id
        recs = body.get("recommendations") or {}
        assert "recommendation_terms" in recs, "detail must include recommendation_terms"
        assert isinstance(recs["recommendation_terms"], dict)
        assert recs["recommendation_terms"], "recommendation_terms must not be empty"

    def test_container_detail_invalid_uuid_returns_400(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _container_detail_url(ros_api_url, "not-a-valid-uuid"),
            headers=container_auth,
            timeout=60,
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        assert "recommendation" in body.get("message", "").lower()

    def test_container_detail_not_found_returns_404(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        missing_id = str(uuid.uuid4())
        resp = http_session.get(
            _container_detail_url(ros_api_url, missing_id),
            headers=container_auth,
            timeout=60,
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body.get("status") == "not_found"

    def test_container_detail_has_monitoring_end_time(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        rec_id = items[0]["id"]
        detail_resp = http_session.get(
            _container_detail_url(ros_api_url, rec_id),
            headers=container_auth,
            timeout=60,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        recs = detail_resp.json().get("recommendations") or {}
        assert "monitoring_end_time" in recs

    def test_container_detail_has_recommendation_engines(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        rec_id = items[0]["id"]
        detail_resp = http_session.get(
            _container_detail_url(ros_api_url, rec_id),
            headers=container_auth,
            timeout=60,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        engines = _medium_term_engines(detail_resp.json())
        assert engines, "medium_term must expose recommendation_engines"

    def test_dual_engine_cost_performance_both_present(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 20},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        for item in items:
            engines = _medium_term_engines(item)
            if "cost" in engines and "performance" in engines:
                assert isinstance(engines["cost"], dict)
                assert isinstance(engines["performance"], dict)
                return
        pytest.skip("No container with both cost and performance engines in sample")

    def test_dual_engine_has_cpu_memory_values(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 20},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No container recommendations in cluster")

        for item in items:
            engines = _medium_term_engines(item)
            for engine_name in ("cost", "performance"):
                engine = engines.get(engine_name)
                if not engine:
                    continue
                cpu, memory = _engine_cpu_memory(engine)
                if cpu is not None and memory is not None:
                    assert cpu >= 0
                    assert memory >= 0
                    return
        pytest.skip("No engine with CPU and memory recommendation values in sample")

    def test_container_list_filter_engine_cost(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"engine": "cost", "limit": 5},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        _assert_paginated_envelope(resp.json())

    def test_container_list_filter_engine_performance(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"engine": "performance", "limit": 5},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        _assert_paginated_envelope(resp.json())
