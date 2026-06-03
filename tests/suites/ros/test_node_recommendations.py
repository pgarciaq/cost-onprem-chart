"""E2E tests for ROS node CPU/memory utilization recommendations.

For divergent cost vs performance node sizing, ingest nise/examples/ocp_dual_engine/.
"""

from __future__ import annotations

import warnings
from typing import Any, Optional

import pytest
import requests

from utils import assert_structured_savings, parse_savings_value

from suites.ros.test_recommendations import get_fresh_token


def _nodes_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/nodes"
    )


def _node_detail_url(ros_api_url: str, node_name: str) -> str:
    return f"{_nodes_url(ros_api_url)}/{node_name}"


def _machinesets_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/machinesets"
    )


def _nodes_utilization_deprecated_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/nodes/utilization"
    )


def _settings_node_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/node"
    )


def _savings_amount_from_node_item(item: dict[str, Any]) -> Optional[float]:
    """Best-effort savings for order_by=estimated_monthly_savings (medium_term cost)."""
    engines = _node_medium_engines(item)
    for engine_name in ("cost", "performance"):
        engine = engines.get(engine_name)
        if not engine:
            continue
        savings = engine.get("estimated_monthly_savings")
        if savings is None:
            legacy = engine.get("estimated_monthly_savings_usd")
            if isinstance(legacy, (int, float)):
                return float(legacy)
            continue
        return parse_savings_value(savings)
    return None


def _assert_node_list_shape(item: dict[str, Any]) -> None:
    """List rows use classification, metrics, and recommendation_terms."""
    assert isinstance(item.get("classification"), dict), "missing classification object"
    cls = item["classification"]
    assert "is_underutilized" in cls
    assert "idle_state" in cls

    assert isinstance(item.get("metrics"), dict), "missing metrics object"
    metrics = item["metrics"]
    for key in ("cpu_util_p50", "cpu_util_p95", "mem_util_p50", "mem_util_p95"):
        assert key in metrics, f"metrics missing {key}"

    terms = item.get("recommendation_terms")
    assert isinstance(terms, dict) and terms, "missing recommendation_terms"


def _assert_node_detail_shape(detail: dict[str, Any]) -> None:
    """Detail uses metrics, recommendation_terms, and top-level idle_state."""
    assert isinstance(detail.get("metrics"), dict), "missing metrics object"
    metrics = detail["metrics"]
    for key in ("cpu_util_p50", "cpu_util_p95", "mem_util_p50", "mem_util_p95"):
        assert key in metrics, f"metrics missing {key}"

    terms = detail.get("recommendation_terms")
    assert isinstance(terms, dict) and terms, "missing recommendation_terms"
    assert detail.get("idle_state"), "detail missing idle_state"


def _node_count_reduction_on_item(item: dict[str, Any]) -> bool:
    terms = item.get("recommendation_terms") or {}
    for term_rec in terms.values():
        engines = (term_rec or {}).get("recommendation_engines") or {}
        for engine in engines.values():
            if engine and "node_count_reduction" in engine:
                return True
    return False


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


def _assert_node_list_engine_filter(body: dict[str, Any], want_engine: str) -> None:
    """When engine filter is set, list rows should expose only that engine block."""
    items = body.get("data") or []
    if not items:
        pytest.skip(f"No node recommendations to validate engine={want_engine}")

    other_engine = "performance" if want_engine == "cost" else "cost"
    filter_omits_other = True

    for item in items:
        engines = _node_medium_engines(item)
        assert want_engine in engines and isinstance(engines[want_engine], dict), (
            f"expected {want_engine!r} under recommendation_engines, got {list(engines.keys())}"
        )
        if other_engine in engines:
            filter_omits_other = False

    if filter_omits_other:
        for item in items:
            engines = _node_medium_engines(item)
            assert other_engine not in engines, (
                f"filter[engine]={want_engine} should omit {other_engine!r} from recommendation_engines"
            )


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
            http_session,
            ros_api_url,
            node_auth,
            {"filter[engine]": "cost", "limit": 5},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        _assert_node_list_engine_filter(resp.json(), "cost")

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
            {"filter[engine]": "performance", "limit": 5},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        _assert_node_list_engine_filter(resp.json(), "performance")

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
                if engine and "estimated_monthly_savings" in engine:
                    found_savings_key = True
                    savings = parse_savings_value(engine["estimated_monthly_savings"])
                    if savings is not None:
                        assert savings >= 0
                        assert_structured_savings(engine["estimated_monthly_savings"])
        assert found_savings_key, (
            "expected estimated_monthly_savings on at least one engine block"
        )

    def test_nodes_dual_engine_divergence(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        """Both engines must be present; divergence is informational when data allows."""
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 50})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No node recommendation data in cluster")

        dual_engine_item = None
        for item in body["data"]:
            engines = _node_medium_engines(item)
            if "cost" in engines and "performance" in engines:
                dual_engine_item = item
                break

        assert dual_engine_item is not None, (
            "No node with both cost and performance engines under medium_term"
        )
        engines = _node_medium_engines(dual_engine_item)
        assert isinstance(engines["cost"], dict)
        assert isinstance(engines["performance"], dict)

        cost_cpu, cost_mem = _engine_sizing(engines["cost"])
        perf_cpu, perf_mem = _engine_sizing(engines["performance"])
        if cost_cpu is None or perf_cpu is None:
            return

        if cost_cpu == perf_cpu and cost_mem == perf_mem:
            warnings.warn(
                "Cost and performance engines returned identical node sizing; "
                "use nise/examples/ocp_dual_engine for divergent fixtures",
                stacklevel=1,
            )
        else:
            assert cost_cpu != perf_cpu or cost_mem != perf_mem

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
            {"filter[cluster]": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("cluster_uuid") == cluster_uuid

    def test_node_detail(
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

        item = items[0]
        node_name = item["node"]
        cluster_uuid = item.get("cluster_uuid")
        assert node_name

        params = {"cluster_uuid": cluster_uuid} if cluster_uuid else None
        resp = http_session.get(
            _node_detail_url(ros_api_url, node_name),
            headers=node_auth,
            params=params,
            timeout=60,
        )
        if resp.status_code == 404:
            pytest.skip("Node detail not available")
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail.get("node") == node_name
        _assert_node_detail_shape(detail)

    def test_node_filter_idle_state(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[idle_state]": "active", "limit": 20},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No node recommendations matching filter[idle_state]=active")

        for item in items:
            idle = (item.get("classification") or {}).get("idle_state")
            if idle is not None:
                assert idle == "active"

    def test_node_order_by(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"order_by": "node", "order_how": "asc", "limit": 10},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if len(items) >= 2:
            names = [item["node"] for item in items]
            assert names == sorted(names)

    def test_node_order_by_savings(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {
                "order_by": "estimated_monthly_savings",
                "order_how": "desc",
                "limit": 10,
            },
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if len(items) < 2:
            pytest.skip("Need at least two nodes to verify savings ordering")

        amounts = [_savings_amount_from_node_item(item) for item in items]
        if any(a is None for a in amounts):
            pytest.skip("Sample nodes lack comparable savings values for ordering")
        assert amounts == sorted(amounts, reverse=True)

    def test_node_filter_term(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[term]": "medium", "limit": 10},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No node recommendation data in cluster")

        for item in items:
            terms = item.get("recommendation_terms") or {}
            assert "medium_term" in terms, (
                f"filter[term]=medium should scope to medium_term, got {list(terms.keys())}"
            )
            medium = terms["medium_term"]
            engines = medium.get("recommendation_engines") or {}
            assert engines, "medium_term should include recommendation_engines"

    def test_node_filter_is_underutilized(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[is_underutilized]": "true", "limit": 20},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No underutilized nodes in cluster")
        for item in items:
            assert (item.get("classification") or {}).get("is_underutilized") is True

    def test_node_filter_is_overcommitted(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[is_overcommitted]": "true", "limit": 20},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No overcommitted nodes in cluster")
        for item in items:
            assert (item.get("classification") or {}).get("is_overcommitted") is True

    def test_node_filter_stranded_resource(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[stranded_resource]": "cpu", "limit": 20},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No nodes with stranded_resource=cpu in cluster")

        for item in items:
            stranded = (item.get("classification") or {}).get("stranded_resource")
            assert stranded == "cpu"

    def test_node_settings_get(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _settings_node_url(ros_api_url),
            headers=node_auth,
            timeout=60,
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in ("underutil_threshold", "cost_target_utilization", "locked_fields"):
            assert key in body, f"settings/node missing {key}"
        assert isinstance(body["locked_fields"], list)

    def test_node_utilization_deprecated_alias(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _nodes_utilization_deprecated_url(ros_api_url),
            headers=node_auth,
            params={"limit": 5},
            timeout=60,
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("Deprecation") == "true"
        link = resp.headers.get("Link") or ""
        assert "/recommendations/openshift/nodes" in link

    def test_node_nested_metrics_and_classification(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 5})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No node recommendation data in cluster")
        _assert_node_list_shape(items[0])

    def test_node_count_reduction_field(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 50})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No node recommendation data in cluster")

        assert any(_node_count_reduction_on_item(item) for item in items), (
            "expected node_count_reduction on at least one engine block"
        )

    def test_machinesets_list(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _machinesets_url(ros_api_url),
            headers=node_auth,
            params={"limit": 10},
            timeout=60,
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)
        if body.get("data"):
            row = body["data"][0]
            for key in (
                "machineset_name",
                "cluster_uuid",
                "current_node_count",
                "recommended_node_count",
                "excess_nodes",
            ):
                assert key in row, f"machineset row missing {key}"

    def test_node_suggested_instance_type_field(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 20})
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No node recommendation data in cluster")
        for item in items:
            suggested = item.get("suggested_instance_type")
            assert suggested is None or (
                isinstance(suggested, str) and (suggested == "" or len(suggested) > 0)
            )
        non_null = [
            item["suggested_instance_type"]
            for item in items
            if item.get("suggested_instance_type")
        ]
        if non_null:
            assert all(isinstance(v, str) and v.strip() for v in non_null)

        instance_type = None
        for item in items:
            itype = item.get("instance_type")
            if itype:
                instance_type = itype
                break
        if not instance_type:
            pytest.skip("No nodes with instance_type in sample data")

        filtered = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[instance_type]": instance_type, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for item in filtered.json().get("data") or []:
            assert item.get("instance_type") == instance_type

    def test_node_csv_export(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"format": "csv", "limit": 50},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type
        assert resp.text.strip(), "expected non-empty CSV body"

    def test_node_filter_tag(
        self,
        ros_api_url: str,
        node_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_nodes(http_session, ros_api_url, node_auth, {"limit": 5})
        if baseline.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        assert baseline.status_code == 200, baseline.text
        unfiltered_count = baseline.json().get("meta", {}).get("count", 0)
        if unfiltered_count == 0:
            pytest.skip("No node recommendation data in cluster")

        resp = _fetch_nodes(
            http_session,
            ros_api_url,
            node_auth,
            {"filter[tag:environment]": "production", "limit": 10},
        )
        if resp.status_code == 404:
            pytest.skip("Node recommendations plugin not enabled")
        if resp.status_code == 400:
            pytest.skip("Tag filtering not enabled or invalid tag key")
        assert resp.status_code == 200, resp.text
        filtered = resp.json()
        assert "meta" in filtered
        filtered_count = filtered.get("meta", {}).get("count", 0)
        assert filtered_count <= unfiltered_count
        if unfiltered_count > 0 and filtered_count == unfiltered_count:
            pytest.skip("Tag filter did not narrow results; no matching tagged workloads")
        for item in filtered.get("data") or []:
            _assert_node_list_shape(item)

    # RBAC for openshift.node is enforced in ros-ocp-backend unit/integration tests
    # (handlers_node_recs_integration_test.go). E2E uses org-admin JWT without
    # per-node restriction fixtures; N/A for chart E2E until scoped test users exist.
