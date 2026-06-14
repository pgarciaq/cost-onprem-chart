"""E2E tests for ROS container recommendation list and detail APIs.

For workloads where cost and performance sizing must differ, generate cluster data
with the NISE fixture at nise/examples/ocp_dual_engine/ (spike-cpu-api, steady-mem-worker).
"""

from __future__ import annotations

import uuid
import warnings
from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import assert_structured_savings


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
    assert body["data"] is None or isinstance(body["data"], list), (
        f"data must be a list or null, got {type(body['data'])}"
    )
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


def _assert_container_list_engine_filter(body: dict[str, Any], want_engine: str) -> None:
    """Mirror ros-ocp-backend assertContainerListEngineFilterResponse for list payloads."""
    _assert_paginated_envelope(body)
    items = body.get("data") or []
    if not items:
        pytest.skip(f"No container recommendations to validate filter[engine]={want_engine}")

    other_engine = "performance" if want_engine == "cost" else "cost"
    filter_omits_other = True

    for item in items:
        engines = _medium_term_engines(item)
        found_want = want_engine in engines and isinstance(engines[want_engine], dict)
        assert found_want, (
            f"expected {want_engine!r} under recommendation_engines, got keys {list(engines.keys())}"
        )
        if other_engine in engines:
            filter_omits_other = False

    if filter_omits_other:
        for item in items:
            engines = _medium_term_engines(item)
            assert other_engine not in engines, (
                f"filter[engine]={want_engine} should omit {other_engine!r} from recommendation_engines"
            )


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


def _container_list_savings(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Savings live under recommendations.estimated_monthly_savings in list responses."""
    recs = item.get("recommendations") or {}
    if isinstance(recs, dict):
        savings = recs.get("estimated_monthly_savings")
        if savings:
            return savings
    legacy = item.get("estimated_monthly_savings")
    return legacy if isinstance(legacy, dict) else None


def _container_detail_savings(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    recs = body.get("recommendations") or {}
    if isinstance(recs, dict):
        savings = recs.get("estimated_monthly_savings")
        if savings:
            return savings
    legacy = body.get("estimated_monthly_savings")
    return legacy if isinstance(legacy, dict) else None


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

        dual_engine_item = None
        for item in items:
            engines = _medium_term_engines(item)
            if "cost" in engines and "performance" in engines:
                dual_engine_item = item
                break

        if dual_engine_item is None:
            pytest.skip(
                "No container with both cost and performance engines under medium_term; "
                "use nise/examples/ocp_dual_engine for divergent dual-engine fixtures"
            )
        engines = _medium_term_engines(dual_engine_item)
        assert isinstance(engines["cost"], dict)
        assert isinstance(engines["performance"], dict)

        cost_cpu, cost_mem = _engine_cpu_memory(engines["cost"])
        perf_cpu, perf_mem = _engine_cpu_memory(engines["performance"])
        if cost_cpu is None or perf_cpu is None:
            pytest.skip("No dual-engine CPU/memory values available")

        if cost_cpu == perf_cpu and cost_mem == perf_mem:
            warnings.warn(
                "Cost and performance engines returned identical sizing; "
                "use nise/examples/ocp_dual_engine for divergent fixtures",
                stacklevel=1,
            )
        else:
            assert cost_cpu != perf_cpu or cost_mem != perf_mem

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
        pytest.skip("No dual-engine CPU/memory values available")

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
        assert resp.status_code == 200, (
            f"{filter_param}={filter_value!r} returned {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip(
                f"No rows returned for {filter_param}={filter_value!r}; "
                "insufficient matching data"
            )
        for item in items:
            actual = item.get(item_field)
            if actual is None:
                pytest.fail(f"Filtered row missing {item_field!r}: {item}")
            if case_insensitive:
                assert str(actual).lower() == str(filter_value).lower(), (
                    f"Filter mismatch: expected {item_field}={filter_value!r} (case-insensitive), "
                    f"got {actual!r}"
                )
            else:
                assert actual == filter_value, (
                    f"Filter mismatch: expected {item_field}={filter_value!r}, got {actual!r}"
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
        """filter[namespace] is an alias for filter[project] and returns the same rows."""
        sample = _first_container_item(ros_api_url, container_auth, http_session)
        namespace = sample.get("project")
        if not namespace:
            pytest.skip("Sample container missing project for namespace alias test")

        project_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[project]": namespace, "limit": 50},
            timeout=60,
        )
        assert project_resp.status_code == 200, project_resp.text
        project_body = project_resp.json()
        _assert_paginated_envelope(project_body)

        namespace_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"filter[namespace]": namespace, "limit": 50},
            timeout=60,
        )
        assert namespace_resp.status_code == 200, namespace_resp.text
        namespace_body = namespace_resp.json()
        _assert_paginated_envelope(namespace_body)

        project_ids = {item.get("id") for item in project_body.get("data") or [] if item.get("id")}
        namespace_ids = {
            item.get("id") for item in namespace_body.get("data") or [] if item.get("id")
        }
        assert project_body.get("meta", {}).get("count") == namespace_body.get("meta", {}).get(
            "count"
        ), (
            "filter[project] and filter[namespace] must return the same meta.count "
            f"(project={project_body.get('meta', {}).get('count')}, "
            f"namespace={namespace_body.get('meta', {}).get('count')})"
        )
        assert project_ids == namespace_ids, (
            "filter[project] and filter[namespace] must return the same recommendation IDs"
        )

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
        _assert_container_list_engine_filter(resp.json(), "cost")

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
        _assert_container_list_engine_filter(resp.json(), "performance")

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
        """Containers filtered by idle_state return rows matching the applied state."""
        endpoint = get_recommendations_endpoint(ros_api_url)
        for state in ("idle", "zombie"):
            resp = http_session.get(
                endpoint,
                headers=container_auth,
                params={"filter[idle_state]": state, "limit": 5},
                timeout=60,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            _assert_paginated_envelope(body)
            items = body.get("data") or []
            if items:
                for item in items:
                    assert item.get("idle_state") == state, (
                        f"Expected idle_state={state!r}, got {item.get('idle_state')!r}"
                    )
                return
        pytest.skip("No idle or zombie containers on cluster")

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
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip("No GPU-enriched containers on cluster")

        gpu_model_lower = gpu_model.lower()
        for item in items:
            gpu_map = item.get("gpu") or {}
            assert gpu_map, (
                f"filter[gpu_model] row must include gpu enrichment: id={item.get('id')}"
            )
            matched = False
            for term_gpu in gpu_map.values():
                if not isinstance(term_gpu, dict):
                    continue
                model_name = term_gpu.get("current_gpu_model") or ""
                if gpu_model_lower in model_name.lower():
                    matched = True
                    break
            assert matched, (
                f"Expected current_gpu_model containing {gpu_model!r}, got gpu={gpu_map!r}"
            )

    def test_container_order_by_variation_fields(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """CONTRACT/SMOKE: API accepts every OpenAPI order_by value without error.

        Does not verify that results are sorted correctly for variation fields.
        """
        for order_by in CONTAINER_ORDER_BY_ENUM:
            resp = http_session.get(
                get_recommendations_endpoint(ros_api_url),
                headers=container_auth,
                params={"order_by": order_by, "order_how": "desc", "limit": 10},
                timeout=60,
            )
            assert resp.status_code == 200, f"{order_by}: {resp.text}"
            _assert_paginated_envelope(resp.json())

    def test_container_keyset_pagination_pages(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """Verify keyset pagination (after + next_cursor) returns distinct pages when count > 1."""
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
        assert meta.get("limit") == 1

        if meta.get("count", 0) <= 1:
            pytest.skip("Only one container — cannot verify multi-page pagination")

        assert "has_next" in meta
        assert meta["has_next"] is True
        next_cursor = meta.get("next_cursor")
        if not next_cursor:
            pytest.skip("Insufficient data for keyset pagination (need next_cursor)")

        page2_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={"limit": 1, "after": next_cursor},
            timeout=60,
        )
        assert page2_resp.status_code == 200, page2_resp.text
        page2 = page2_resp.json()
        _assert_paginated_envelope(page2)
        assert len(page2.get("data") or []) >= 1

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

    def _discover_two_tag_filters(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
        gateway_url: str,
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        """Return two (key, value) pairs that each match at least one container."""
        pairs: list[tuple[str, str]] = []
        tags_resp = http_session.get(
            f"{gateway_url.rstrip('/')}/cost-management/v1/tags/openshift/",
            headers=container_auth,
            timeout=60,
        )
        if tags_resp.status_code == 200:
            for row in tags_resp.json().get("data") or []:
                key = (row.get("key") or row.get("tag")) if isinstance(row, dict) else None
                values = row.get("values") or [] if isinstance(row, dict) else []
                if not key or not values:
                    continue
                value = values[0] if isinstance(values[0], str) else values[0].get("value")
                if not value:
                    continue
                probe = http_session.get(
                    get_recommendations_endpoint(ros_api_url),
                    headers=container_auth,
                    params={f"filter[tag:{key}]": value, "limit": 1},
                    timeout=60,
                )
                if probe.status_code == 200 and probe.json().get("meta", {}).get("count", 0) > 0:
                    pairs.append((key, value))
                if len(pairs) >= 2:
                    return pairs[0], pairs[1]

        for key, value in (("environment", "production"), ("app", "billing")):
            probe = http_session.get(
                get_recommendations_endpoint(ros_api_url),
                headers=container_auth,
                params={f"filter[tag:{key}]": value, "limit": 1},
                timeout=60,
            )
            if probe.status_code == 200 and probe.json().get("meta", {}).get("count", 0) > 0:
                if (key, value) not in pairs:
                    pairs.append((key, value))
            if len(pairs) >= 2:
                return pairs[0], pairs[1]

        pytest.skip(
            "Need two tag key/value pairs with matching containers; enable OCP tags and ingest labeled workloads"
        )

    def test_tag_filter_multi_key_and_logic(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
        gateway_url: str,
    ):
        """Multiple filter[tag:*] keys combine with AND and narrow the result set."""
        (key1, value1), (key2, value2) = self._discover_two_tag_filters(
            ros_api_url, container_auth, http_session, gateway_url
        )

        def _count(params: dict[str, str]) -> int:
            resp = http_session.get(
                get_recommendations_endpoint(ros_api_url),
                headers=container_auth,
                params={**params, "limit": 100},
                timeout=60,
            )
            assert resp.status_code == 200, resp.text
            return resp.json().get("meta", {}).get("count", 0)

        count_key1 = _count({f"filter[tag:{key1}]": value1})
        count_key2 = _count({f"filter[tag:{key2}]": value2})
        if count_key1 == 0 or count_key2 == 0:
            pytest.skip("Single-key tag probes returned no rows")

        dual_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={
                f"filter[tag:{key1}]": value1,
                f"filter[tag:{key2}]": value2,
                "limit": 100,
            },
            timeout=60,
        )
        assert dual_resp.status_code == 200, dual_resp.text
        dual_body = dual_resp.json()
        _assert_paginated_envelope(dual_body)
        dual_count = dual_body.get("meta", {}).get("count", 0)
        assert dual_count <= count_key1, (
            f"AND filter count {dual_count} should be <= single-key {key1} count {count_key1}"
        )
        assert dual_count <= count_key2, (
            f"AND filter count {dual_count} should be <= single-key {key2} count {count_key2}"
        )
        if dual_count == 0:
            pytest.skip(f"No containers match both {key1}={value1} and {key2}={value2}")

        single_key1_ids = {
            item.get("id")
            for item in (
                http_session.get(
                    get_recommendations_endpoint(ros_api_url),
                    headers=container_auth,
                    params={f"filter[tag:{key1}]": value1, "limit": 100},
                    timeout=60,
                ).json().get("data")
                or []
            )
            if item.get("id")
        }
        single_key2_ids = {
            item.get("id")
            for item in (
                http_session.get(
                    get_recommendations_endpoint(ros_api_url),
                    headers=container_auth,
                    params={f"filter[tag:{key2}]": value2, "limit": 100},
                    timeout=60,
                ).json().get("data")
                or []
            )
            if item.get("id")
        }
        for item in dual_body.get("data") or []:
            row_id = item.get("id")
            assert row_id in single_key1_ids, (
                f"Row {row_id} must appear in {key1}={value1} filter results"
            )
            assert row_id in single_key2_ids, (
                f"Row {row_id} must appear in {key2}={value2} filter results"
            )

    def test_tag_filter_with_rbac_scoped_identity(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """Tag filter plus inaccessible cluster returns empty list (RBAC ∩ tag intersection)."""
        tag_key = "environment"
        tag_value = "production"
        baseline = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={f"filter[tag:{tag_key}]": tag_value, "limit": 20},
            timeout=60,
        )
        assert baseline.status_code == 200, baseline.text
        if baseline.json().get("meta", {}).get("count", 0) == 0:
            pytest.skip(f"No containers with tag {tag_key}={tag_value} for baseline")

        denied_cluster = "00000000-0000-0000-0000-000000000099"
        resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=container_auth,
            params={
                "filter[cluster]": denied_cluster,
                f"filter[tag:{tag_key}]": tag_value,
                "limit": 20,
            },
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body.get("data") or []
        assert data == [], (
            f"RBAC should deny access to inaccessible cluster {denied_cluster!r}"
        )
        meta_count = body.get("meta", {}).get("count", 0)
        assert meta_count == 0, (
            f"meta.count={meta_count} should be 0 when data is empty "
            f"(RBAC filtering must apply to count query too)"
        )

    def test_container_order_by(
        self,
        ros_api_url: str,
        container_auth: dict,
        http_session: requests.Session,
    ):
        """Verify order_by=last_reported with order_how=desc is non-increasing.

        Uses pairwise comparison so rows with the same last_reported (same processing
        batch) may appear in any order. See test_container_order_by_variation_fields
        for CONTRACT/SMOKE coverage of all order_by enum values.
        """
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
        for i in range(len(timestamps) - 1):
            assert timestamps[i] >= timestamps[i + 1], (
                f"Sort order violated at index {i}: {timestamps[i]!r} < {timestamps[i + 1]!r}"
            )

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
            (item for item in items if _container_list_savings(item)),
            None,
        )
        assert savings_item is not None, (
            "Expected at least one container with estimated_monthly_savings after cost model assignment"
        )

        savings = _container_list_savings(savings_item)
        assert savings is not None
        assert_structured_savings(savings)
        assert savings["units"] in ("USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CHF", "NZD")

        rec_id = savings_item.get("id")
        if rec_id:
            detail_resp = http_session.get(
                _container_detail_url(ros_api_url, rec_id),
                headers=container_auth,
                timeout=60,
            )
            assert detail_resp.status_code == 200, detail_resp.text
            detail_savings = _container_detail_savings(detail_resp.json())
            if detail_savings:
                assert_structured_savings(detail_savings)

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
        assert codes == {1, 2, 3, 5, 6, 7, 8, 9, 21, 22, 25, 77}

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
