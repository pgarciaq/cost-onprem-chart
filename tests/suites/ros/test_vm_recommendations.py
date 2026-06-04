"""E2E tests for ROS OpenShift Virtualization (VM) recommendation APIs.

Endpoints:
  GET /api/cost-management/v1/recommendations/openshift/vm
  GET /api/cost-management/v1/recommendations/openshift/vm/detail

Requires ros-ocp-backend VM plugin enabled (Helm ros.api.enabledPlugins includes vm).
When disabled the API returns 404 and tests skip.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token

VALID_VM_CONFIDENCE = frozenset({"high", "moderate", "low"})



def _vm_list_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/vm"
    )


def _vm_detail_url(ros_api_url: str) -> str:
    return f"{_vm_list_url(ros_api_url)}/detail"


def _fetch_vm_list(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _vm_list_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _fetch_vm_detail(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: dict[str, Any],
) -> requests.Response:
    return session.get(
        _vm_detail_url(ros_api_url),
        headers=auth,
        params=params,
        timeout=60,
    )


def _vm_history_url(ros_api_url: str, vm_name: str) -> str:
    base = ros_api_url.rstrip("/")
    return (
        f"{base}/cost-management/v1/recommendations/openshift/vms/"
        f"{vm_name}/history"
    )


def _fetch_vm_history(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    vm_name: str,
    params: dict[str, Any],
) -> requests.Response:
    return session.get(
        _vm_history_url(ros_api_url, vm_name),
        headers=auth,
        params=params,
        timeout=60,
    )


def _vm_instance_types_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/instance-types"
    )


def _vm_settings_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/vm"
    )


def _vm_terms_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/vm/terms"
    )


def _savings_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/savings-summary"
    )


def _notification_codes_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/notification-codes"
    )


def _first_vm_row(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
) -> Optional[dict[str, Any]]:
    resp = _fetch_vm_list(session, ros_api_url, auth, {"limit": 5})
    skip_if_vm_plugin_disabled(resp)
    assert resp.status_code == 200, resp.text
    items = resp.json().get("data") or []
    if not items:
        return None
    return items[0]


def skip_if_vm_plugin_disabled(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip("VM recommendations plugin not enabled (404 on /vm)")


def vm_item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("metadata") or {}


@pytest.fixture
def vm_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestVMRecommendationsE2E:
    """VM recommendation list and detail against a deployed cluster."""

    def test_vm_list_envelope(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 10})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert "links" in body
        assert isinstance(body["data"], list)

    def test_vm_list_required_fields(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No VM recommendation data in cluster")

        item = body["data"][0]
        assert item.get("vm_name")
        assert item.get("namespace")
        assert item.get("cluster_uuid")
        meta = vm_item_metadata(item)
        assert "guest_agent_detected" in meta
        assert meta.get("confidence") in VALID_VM_CONFIDENCE

    def test_vm_filter_tag(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        unfiltered_count = baseline.json().get("meta", {}).get("count", 0)
        if unfiltered_count == 0:
            pytest.skip("No VM recommendation data in cluster")

        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[tag:environment]": "production", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        if resp.status_code == 400:
            pytest.skip("Tag filtering not enabled or invalid tag key")
        assert resp.status_code == 200, resp.text
        filtered_count = resp.json().get("meta", {}).get("count", 0)
        assert filtered_count <= unfiltered_count
        if unfiltered_count > 0 and filtered_count == unfiltered_count:
            pytest.skip("Tag filter did not narrow VM results; no matching tagged namespaces")
        if filtered_count == 0:
            pytest.skip("No VMs match filter[tag:environment]=production")

    def test_vm_list_current_and_recommended(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("meta", {}).get("count", 0) == 0:
            pytest.skip("No VM recommendation data in cluster")

        item = body["data"][0]
        current = item.get("current") or {}
        recommended = item.get("recommended") or {}
        assert "vcpu" in current
        assert "memory_gib" in current
        assert "vcpu" in recommended
        assert "memory_gib" in recommended

    def test_vm_filter_by_vm_name(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No VM recommendation data in cluster")

        vm_name = items[0]["vm_name"]
        filtered = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {f"filter[vm_name]": vm_name, "limit": 10},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("vm_name") == vm_name

    def test_vm_filter_by_namespace(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(baseline)
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No VM recommendation data in cluster")

        namespace = items[0]["namespace"]
        filtered = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {f"filter[namespace]": namespace, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        for row in filtered.json().get("data") or []:
            assert row.get("namespace") == namespace

    def test_vm_recommendations_exist(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        """Default CI: VM list returns 200 and plugin is reachable (data optional)."""
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 1})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert body["meta"].get("count", 0) >= 0

    def test_vm_detail_preference_fields_when_configured(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        """When cluster preferences are ingested, detail exposes preference metadata."""
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 100})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("No VM recommendation data in cluster")

        for row in rows:
            meta = row.get("metadata") or {}
            if not meta.get("preference_name"):
                continue
            detail = _fetch_vm_detail(
                http_session,
                ros_api_url,
                vm_auth,
                {
                    "vm_name": row["vm_name"],
                    "namespace": row["namespace"],
                    "cluster_uuid": row["cluster_uuid"],
                },
            )
            assert detail.status_code == 200, detail.text
            detail_meta = detail.json().get("metadata") or {}
            assert detail_meta.get("preference_name")
            assert detail_meta.get("preference_class")
            return
        pytest.skip(
            "No VM with preference_name in list; upload cluster_instance_types.json "
            "with vm_preferences (see test_vm_preference_flow extended E2E)"
        )

    @pytest.mark.extended
    def test_vm_csv_export(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        """GET list with format=csv returns text/csv."""
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"format": "csv", "limit": 100},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type, content_type
        assert "vm_name" in resp.text


@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.extended
@pytest.mark.integration
@pytest.mark.timeout(60)
class TestVMRecommendationsExtended:
    """Extended VM API coverage (history, settings, fleet savings, filters)."""

    def test_vm_history(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        row = _first_vm_row(http_session, ros_api_url, vm_auth)
        if not row:
            pytest.skip("No VM recommendation data in cluster")

        params: dict[str, Any] = {
            "namespace": row["namespace"],
            "term": "short_term",
            "engine": "cost",
            "limit": 10,
            "offset": 0,
        }
        if row.get("cluster_uuid"):
            params["cluster_uuid"] = row["cluster_uuid"]

        resp = _fetch_vm_history(
            http_session, ros_api_url, vm_auth, row["vm_name"], params
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_vm_history_csv_export(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        row = _first_vm_row(http_session, ros_api_url, vm_auth)
        if not row:
            pytest.skip("No VM recommendation data in cluster")

        params: dict[str, Any] = {
            "namespace": row["namespace"],
            "term": "short_term",
            "engine": "cost",
            "format": "csv",
            "limit": 100,
        }
        if row.get("cluster_uuid"):
            params["cluster_uuid"] = row["cluster_uuid"]

        resp = _fetch_vm_history(
            http_session, ros_api_url, vm_auth, row["vm_name"], params
        )
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type, content_type

    def test_vm_instance_types(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        row = _first_vm_row(http_session, ros_api_url, vm_auth)
        if not row:
            pytest.skip("No VM recommendation data in cluster")

        resp = http_session.get(
            _vm_instance_types_url(ros_api_url),
            headers=vm_auth,
            params={"cluster_uuid": row["cluster_uuid"]},
            timeout=60,
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_settings(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _vm_settings_url(ros_api_url), headers=vm_auth, timeout=30
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        thresholds = body.get("thresholds") or {}
        assert "cpu_percentile_cost" in thresholds
        assert "cpu_percentile_perf" in thresholds

    def test_vm_term_settings(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _vm_terms_url(ros_api_url), headers=vm_auth, timeout=30
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_filter_by_engine(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[engine]": "cost", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_filter_by_term(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[term]": "short", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_savings_in_response(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 20})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip("No VM recommendation data in cluster")
        for row in items:
            assert "savings" in row, row
            savings = row.get("savings")
            if savings is not None:
                assert "value" in savings, row
                assert "units" in savings, row

    def test_vm_notification_codes_catalog(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 1})
        skip_if_vm_plugin_disabled(baseline)

        resp = http_session.get(
            _notification_codes_url(ros_api_url),
            headers=vm_auth,
            params={"filter[plugin]": "vm"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        codes = {entry["code"] for entry in resp.json().get("data") or []}
        assert 64 in codes, f"expected power-off code 64 in catalog, got {codes}"
        assert codes & {65, 66}, (
            f"expected network QoS code 65 or 66 in catalog, got {codes}"
        )
        assert codes & {67, 68, 69}, (
            f"expected storage tiering code 67, 68, or 69 in catalog, got {codes}"
        )

    def test_vm_savings_shape(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"limit": 20, "order_by": "last_recommended_at", "order_how": "desc"},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        for row in resp.json().get("data") or []:
            savings = row.get("savings")
            if savings is not None:
                assert "value" in savings, row
                assert "units" in savings, row
                return
        # All null savings is acceptable when estimates are disabled or rates missing.

    def test_vm_filter_engine_performance(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[engine]": "performance", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_filter_cluster(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[cluster]": "*", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_idle_filter(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[is_idle]": "true", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_abandoned_filter(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[is_abandoned]": "true", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_vm_fleet_savings_by_plugin(
        self,
        ros_api_url: str,
        vm_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=vm_auth,
            params={"engine": "cost", "term": "medium"},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        by_plugin = resp.json().get("by_plugin") or {}
        assert "vm" in by_plugin
        assert isinstance(by_plugin["vm"], (int, float))
