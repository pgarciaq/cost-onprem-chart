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


CONTAINER_ORDER_BY_ENUM = (
    "cluster",
    "project",
    "workload_type",
    "workload",
    "container",
    "last_reported",
    "cpu_request_current",
    "memory_request_current",
    "cpu_variation_short_cost",
    "cpu_variation_short_performance",
    "cpu_variation_medium_cost",
    "cpu_variation_medium_performance",
    "cpu_variation_long_cost",
    "cpu_variation_long_performance",
    "memory_variation_short_cost",
    "memory_variation_short_performance",
    "memory_variation_medium_cost",
    "memory_variation_medium_performance",
    "memory_variation_long_cost",
    "memory_variation_long_performance",
)


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


def _first_container_item(
    ros_api_url: str,
    container_auth: dict,
    http_session: requests.Session,
) -> dict[str, Any]:
    resp = http_session.get(
        get_recommendations_endpoint(ros_api_url),
        headers=container_auth,
        params={"limit": 1},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json().get("data") or []
    if not items:
        pytest.skip("No container recommendations in cluster")
    return items[0]


def _assert_filtered_items_match(
    items: list[dict[str, Any]],
    item_field: str,
    expected: str,
    *,
    case_insensitive: bool = False,
) -> None:
    if not items:
        return
    for item in items:
        actual = item.get(item_field)
        if actual is None:
            pytest.fail(f"Filtered row missing {item_field!r}: {item}")
        if case_insensitive:
            assert str(actual).lower() == str(expected).lower(), (
                f"Expected {item_field}={expected!r}, got {actual!r}"
            )
        else:
            assert actual == expected, (
                f"Expected {item_field}={expected!r}, got {actual!r}"
            )


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

    @pytest.mark.parametrize(
        "filter_param,item_field,case_insensitive",
        [
            ("filter[project]", "project", False),
            ("filter[workload]", "workload", False),
            ("filter[container]", "container", False),
            ("filter[workload_type]", "workload_type", True),
        ],
    )
    def test_container_list_filter_results_match(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
        filter_param: str,
        item_field: str,
        case_insensitive: bool,
    ):
        """Filtered list rows match the applied filter value when data is returned."""
        sample = _first_container_item(ros_api_url, container_auth, http_session)
        expected = sample.get(item_field)
        if not expected:
            pytest.skip(f"Sample container missing {item_field} for filter validation")

        filter_value = str(expected).lower() if case_insensitive else expected
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={filter_param: filter_value, "limit": 50},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip(
                f"No rows returned for {filter_param}={filter_value!r}; "
                "insufficient matching data"
            )
        _assert_filtered_items_match(
            items, item_field, filter_value, case_insensitive=case_insensitive
        )

    def test_container_list_filter_cluster_results_match(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """filter[cluster] returns rows for the same cluster UUID when data exists."""
        sample = _first_container_item(ros_api_url, container_auth, http_session)
        cluster_uuid = sample.get("cluster_uuid")
        if not cluster_uuid:
            pytest.skip("Sample container missing cluster_uuid")

        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[cluster]": cluster_uuid, "limit": 50},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip(f"No rows returned for filter[cluster]={cluster_uuid}")

        for item in items:
            assert item.get("cluster_uuid") == cluster_uuid, (
                f"Expected cluster_uuid={cluster_uuid!r}, got {item.get('cluster_uuid')!r}"
            )

    def test_container_filter_namespace_alias(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """filter[namespace] is an alias for filter[project] and returns 200."""
        sample = _first_container_item(ros_api_url, container_auth, http_session)
        namespace = sample.get("project")
        if not namespace:
            pytest.skip("Sample container missing project for namespace alias test")

        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[namespace]": namespace, "limit": 50},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        _assert_paginated_envelope(resp.json())

    def test_container_list_filter_engine_cost(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[engine]": "cost", "limit": 5},
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
            params={"filter[engine]": "performance", "limit": 5},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        _assert_paginated_envelope(resp.json())

    def test_container_filter_idle_state_active(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """When active rows exist, each returned row must have idle_state=active."""
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[idle_state]": "active", "limit": 20},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip("No active container recommendations in cluster")
        for item in items:
            assert item.get("idle_state") == "active", (
                f"Expected idle_state=active, got {item.get('idle_state')!r}"
            )

    def test_container_filter_idle_state(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[idle_state]": "idle", "limit": 5},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        for item in body.get("data") or []:
            assert item.get("idle_state") == "idle"

    def test_container_filter_idle_state_zombie(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """When zombie rows exist, each returned row must have idle_state=zombie."""
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[idle_state]": "zombie", "limit": 20},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip("No zombie container recommendations in cluster")
        for item in items:
            assert item.get("idle_state") == "zombie", (
                f"Expected idle_state=zombie, got {item.get('idle_state')!r}"
            )

    def test_container_filter_gpu_model(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """filter[gpu_model] returns 200 when GPU data exists."""
        gpu_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[has_gpu]": "true", "limit": 1},
            timeout=60,
        )
        assert gpu_resp.status_code == 200, gpu_resp.text
        gpu_items = gpu_resp.json().get("data") or []
        if not gpu_items:
            pytest.skip("No GPU container data in cluster")

        gpu_block = gpu_items[0].get("gpu") or {}
        gpu_model = gpu_block.get("current_gpu_model")
        if not gpu_model:
            pytest.skip("Sample GPU container missing current_gpu_model")

        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[gpu_model]": gpu_model, "filter[has_gpu]": "true", "limit": 20},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        _assert_paginated_envelope(resp.json())

    def test_container_order_by_variation_fields(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """All OpenAPI order_by enum values return 200."""
        for order_by in CONTAINER_ORDER_BY_ENUM:
            resp = http_session.get(
                get_recommendations_endpoint(ros_api_url),
                headers=container_auth,
                params={"order_by": order_by, "order_how": "desc", "limit": 10},
                timeout=60,
            )
            assert resp.status_code == 200, f"{order_by}: {resp.text}"
            _assert_paginated_envelope(resp.json())

    def test_container_offset_pagination_pages(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """limit=1&offset=0 and offset=1 return distinct rows when count > 1."""
        page1_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1, "offset": 0},
            timeout=60,
        )
        assert page1_resp.status_code == 200, page1_resp.text
        page1 = page1_resp.json()
        _assert_paginated_envelope(page1)
        meta = page1.get("meta") or {}
        assert meta.get("limit") == 1
        assert meta.get("offset") == 0

        if meta.get("count", 0) <= 1:
            pytest.skip("Need more than one container for offset pagination")

        page2_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1, "offset": 1},
            timeout=60,
        )
        assert page2_resp.status_code == 200, page2_resp.text
        page2 = page2_resp.json()
        _assert_paginated_envelope(page2)
        assert page2.get("meta", {}).get("offset") == 1

        ids1 = {item.get("id") for item in page1.get("data") or []}
        ids2 = {item.get("id") for item in page2.get("data") or []}
        assert ids1.isdisjoint(ids2), "Offset page 2 must not repeat page 1 rows"

    def test_container_keyset_pagination(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        page1_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1},
            timeout=60,
        )
        assert page1_resp.status_code == 200, page1_resp.text
        page1 = page1_resp.json()
        _assert_paginated_envelope(page1)
        meta = page1.get("meta") or {}
        if not meta.get("has_next") or not meta.get("next_cursor"):
            pytest.skip("Insufficient data for keyset pagination (need has_next and next_cursor)")

        page2_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1, "after": meta["next_cursor"]},
            timeout=60,
        )
        assert page2_resp.status_code == 200, page2_resp.text
        page2 = page2_resp.json()
        _assert_paginated_envelope(page2)
        ids1 = {item.get("id") for item in page1.get("data") or []}
        ids2 = {item.get("id") for item in page2.get("data") or []}
        assert ids1.isdisjoint(ids2), "Keyset page 2 must not repeat page 1 rows"

    def test_container_filter_tag(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        tag_key = "environment"
        tag_value = "production"
        unfiltered_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 100},
            timeout=60,
        )
        assert unfiltered_resp.status_code == 200, unfiltered_resp.text
        unfiltered_count = unfiltered_resp.json().get("meta", {}).get("count", 0)

        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={f"filter[tag:{tag_key}]": tag_value, "limit": 100},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        if body.get("meta", {}).get("count", 0) == 0:
            warnings = (body.get("meta") or {}).get("warnings") or []
            if warnings:
                pytest.skip(f"No containers with tag {tag_key}={tag_value} in cluster")
            pytest.skip("No tagged container data; ROS_TAGS_ENABLED may be off")

        filtered_count = body.get("meta", {}).get("count", 0)
        assert filtered_count > 0
        assert filtered_count <= unfiltered_count, (
            "Tag filter should narrow or match the unfiltered result set"
        )
        items = body.get("data") or []
        assert items, "Expected data rows when meta.count > 0"

    def test_container_order_by(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"order_by": "last_reported", "order_how": "desc", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if len(items) < 2:
            pytest.skip("Need at least 2 containers to verify sort order")
        timestamps = [item.get("last_reported") for item in items if item.get("last_reported")]
        if len(timestamps) < 2:
            pytest.skip("Containers missing last_reported for sort verification")
        assert timestamps == sorted(timestamps, reverse=True)

    def test_container_savings_shape(
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

        savings_item = next(
            (item for item in items if item.get("estimated_monthly_savings")),
            None,
        )
        if savings_item is None:
            pytest.skip("No containers with estimated_monthly_savings in sample")

        savings = savings_item["estimated_monthly_savings"]
        assert isinstance(savings, dict)
        assert "value" in savings
        assert "units" in savings
        assert savings["units"] in ("USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CHF", "NZD")

        rec_id = savings_item.get("id")
        if rec_id:
            detail_resp = http_session.get(
                _container_detail_url(ros_api_url, rec_id),
                headers=container_auth,
                timeout=60,
            )
            assert detail_resp.status_code == 200, detail_resp.text
            detail_savings = detail_resp.json().get("estimated_monthly_savings")
            if detail_savings:
                assert "value" in detail_savings
                assert "units" in detail_savings

    def test_container_notification_codes_catalog(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        url = (
            f"{ros_api_url.rstrip('/')}/cost-management/v1/"
            "recommendations/openshift/notification-codes"
        )
        resp = http_session.get(
            url,
            headers=container_auth,
            params={"filter[plugin]": "container"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        codes = {entry["code"] for entry in body["data"]}
        assert codes == {1, 2, 3, 5, 6, 7, 8, 9, 21, 22, 25}

    def test_container_csv_export(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"format": "csv", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type
        assert len(resp.text) > 0
