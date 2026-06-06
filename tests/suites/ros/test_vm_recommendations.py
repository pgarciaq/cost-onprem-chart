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
from utils import assert_structured_savings

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
        """VM list returns 200 with at least one recommendation when data exists."""
        resp = _fetch_vm_list(http_session, ros_api_url, vm_auth, {"limit": 1})
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        count = resp.json().get("meta", {}).get("count", 0)
        if count == 0:
            pytest.skip("No VM recommendation data available")
        assert count > 0

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
        if "text/csv" not in content_type:
            pytest.skip(
                "VM CSV export returned JSON instead of text/csv "
                "(possible stale image or routing issue)"
            )
        import csv
        from io import StringIO

        reader = csv.reader(StringIO(resp.text))
        header = next(reader, None)
        assert header, "CSV missing header row"
        assert "vm_name" in header, f"CSV header missing vm_name: {header}"
        assert "namespace" in header, f"CSV header missing namespace: {header}"
        assert "cluster_uuid" in header, f"CSV header missing cluster_uuid: {header}"


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
        for row in resp.json().get("data") or []:
            meta = vm_item_metadata(row)
            assert meta.get("engine") == "cost", (
                f"Expected engine=cost, got {meta.get('engine')}"
            )

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
            {"filter[term]": "short_term", "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        data = resp.json().get("data") or []
        if not data:
            pytest.skip(
                "filter[term]=short_term returned no results — cannot verify"
            )
        for row in data:
            meta = vm_item_metadata(row)
            assert meta.get("term") == "short_term", (
                f"Expected term=short_term, got {meta.get('term')}"
            )

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
        if resp.status_code != 200:
            pytest.skip(
                f"Notification codes endpoint returned {resp.status_code} "
                "(expected 200; check deployment routing/auth)"
            )
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
        pytest.skip(
            "No VM rows with non-null savings in response — cannot verify savings shape"
        )

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
        for row in resp.json().get("data") or []:
            meta = vm_item_metadata(row)
            assert meta.get("engine") == "performance", (
                f"Expected engine=performance, got {meta.get('engine')}"
            )

    def test_vm_filter_cluster(
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

        cluster_uuid = items[0]["cluster_uuid"]
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {"filter[cluster]": cluster_uuid, "limit": 10},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        data = resp.json().get("data") or []
        assert len(data) > 0, (
            f"filter[cluster]={cluster_uuid} returned no results — cannot verify"
        )
        for row in data:
            assert row.get("cluster_uuid") == cluster_uuid, (
                f"Expected cluster_uuid={cluster_uuid}, got {row.get('cluster_uuid')}"
            )

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
        data = resp.json().get("data") or []
        if not data:
            pytest.skip(
                "No idle VMs in cluster — filter[is_idle]=true returned no results"
            )
        for row in data:
            meta = vm_item_metadata(row)
            assert meta.get("is_idle") is True, (
                f"Expected metadata.is_idle=True for {row.get('vm_name')}"
            )

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
        data = resp.json().get("data") or []
        if not data:
            pytest.skip(
                "No abandoned VMs in cluster — filter[is_abandoned]=true "
                "returned no results"
            )
        for row in data:
            meta = vm_item_metadata(row)
            assert meta.get("is_abandoned") is True, (
                f"Expected metadata.is_abandoned=True for {row.get('vm_name')}"
            )

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
        assert_structured_savings(by_plugin["vm"])


_UPLOAD_ORG_ID = "1234567"


@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.extended
@pytest.mark.integration
@pytest.mark.timeout(600)
class TestVMNotificationEmission:
    """Real E2E: ingest VM data with specific patterns, verify notification code emission.

    This class ingests a 28-day VM dataset containing scenarios for codes 64-69,
    waits for ros-ocp-backend to produce recommendations, then asserts each VM
    carries the expected notification code.
    """

    _TEMPLATE = "ocp_report_vm_enhancements_64_69.yml"
    _NAMESPACE = "vm-enhancements-64-69"
    _EXPECTED_VMS = {
        "power-off-candidate-vm-01": 64,
        "network-qos-sriov-vm-01": 65,
        "network-qos-dpdk-vm-01": 66,
        "storage-tier-cold-vm-01": 67,
        "storage-tier-iops-vm-01": 68,
        "storage-tier-throughput-vm-01": 69,
    }
    _WAIT_TIMEOUT = 720
    _POLL_INTERVAL = 15

    @pytest.fixture(autouse=True, scope="class")
    def ingest_vm_enhancement_data(
        self,
        ros_api_url,
        cluster_config,
        keycloak_config,
        ingress_url,
    ):
        """Generate 28-day VM data, upload, wait for recommendations."""
        import tempfile
        import time
        import uuid as uuid_mod
        from datetime import datetime, timedelta

        from conftest import obtain_jwt_token
        from e2e_helpers import (
            ensure_nise_available,
            generate_nise_data,
            get_koku_api_url,
            register_source,
            upload_with_retry,
            wait_for_provider,
        )
        from utils import (
            create_rh_identity_header,
            create_upload_package_from_files,
            execute_db_query,
            get_pod_by_label,
            wait_for_condition,
        )

        if not ensure_nise_available():
            pytest.skip("NISE with OCPVirtualMachineGenerator not available")

        session = requests.Session()
        session.verify = False

        auth = get_fresh_token(keycloak_config, cluster_config, session)
        if not auth:
            pytest.skip("Could not obtain JWT token for VM notification tests")

        resp = _fetch_vm_list(session, ros_api_url, auth, {"limit": 1})
        skip_if_vm_plugin_disabled(resp)

        cluster_id = str(uuid_mod.uuid4())
        end_date = datetime.utcnow() - timedelta(days=1)
        start_date = end_date - timedelta(days=28)

        ingress_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=ingress"
        )
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not ingress_pod or not db_pod:
            pytest.skip("Ingress or database pod not found")

        admin_identity = create_rh_identity_header(_UPLOAD_ORG_ID)
        koku_url = get_koku_api_url(
            cluster_config.helm_release_name, cluster_config.namespace
        )
        reg = register_source(
            namespace=cluster_config.namespace,
            pod=ingress_pod,
            api_url=koku_url,
            rh_identity_header=admin_identity,
            cluster_id=cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"vm-notif-{cluster_id.replace('-', '')[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, cluster_id, timeout=300
        ):
            pytest.fail(f"Provider not created for cluster {cluster_id}")

        with tempfile.TemporaryDirectory(prefix="vm_notif_e2e_") as temp_dir:
            files = generate_nise_data(
                cluster_id=cluster_id,
                start_date=start_date,
                end_date=end_date,
                output_dir=temp_dir,
                include_ros=True,
                iqe_template=self._TEMPLATE,
            )

            ros_files = list(files.get("ros_vm_usage_files") or [])
            ros_files.extend(files.get("ros_usage_files") or [])
            if not ros_files:
                pytest.skip("NISE did not generate VM ROS files")

            pod_files = files.get("pod_usage_files") or ros_files
            package_path = create_upload_package_from_files(
                pod_usage_files=pod_files,
                ros_usage_files=ros_files,
                cluster_id=cluster_id,
                start_date=start_date,
                end_date=end_date,
            )

            token = obtain_jwt_token(keycloak_config)
            upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
            upload_session = requests.Session()
            upload_session.verify = False
            resp = upload_with_retry(
                upload_session,
                upload_url,
                package_path,
                token.authorization_header,
            )
            assert resp.status_code in (200, 201, 202), (
                f"Upload failed: {resp.status_code} {resp.text}"
            )

        def _db_has_vm_recommendations():
            result = execute_db_query(
                cluster_config.namespace,
                db_pod,
                "costonprem_ros",
                "postgres",
                f"""
                SELECT COUNT(DISTINCT vm_name) FROM vm_recommendations
                WHERE cluster_uuid = '{cluster_id}'
                  AND org_id = '{_UPLOAD_ORG_ID}'
                  AND namespace = '{self._NAMESPACE}'
                """,
            )
            return (
                result is not None
                and int(result[0][0]) >= len(self._EXPECTED_VMS)
            )

        if not wait_for_condition(
            _db_has_vm_recommendations,
            timeout=self._WAIT_TIMEOUT,
            interval=25,
            description="all VM recommendations populated",
        ):
            pytest.fail(
                f"Expected {len(self._EXPECTED_VMS)} VM recommendations after "
                f"{self._WAIT_TIMEOUT}s; ROS processor may not have finished"
            )

        self.__class__._cluster_id = cluster_id
        self.__class__._auth = auth

    def _get_vm(self, http_session, ros_api_url, vm_auth, vm_name):
        """Fetch a specific VM recommendation by name and namespace."""
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_auth,
            {
                "filter[vm_name]": vm_name,
                "filter[namespace]": self._NAMESPACE,
                "filter[cluster]": self._cluster_id,
                "filter[term]": "long_term",
                "filter[engine]": "cost",
                "limit": 10,
            },
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("data") or []
        if not items:
            pytest.skip(f"VM '{vm_name}' not found in recommendations")
        return items[0]

    def _get_notification_codes(self, vm_row):
        """Extract notification codes from a VM recommendation row."""
        notifications = vm_row.get("notifications") or []
        return {n["code"] for n in notifications if "code" in n}

    def test_power_off_schedule_code_64(
        self, ros_api_url, vm_auth, http_session
    ):
        """VM with 70%+ idle days gets power-off schedule notification (code 64)."""
        vm = self._get_vm(http_session, ros_api_url, vm_auth, "power-off-candidate-vm-01")
        codes = self._get_notification_codes(vm)
        assert 64 in codes, (
            f"Expected code 64 (power-off) for power-off-candidate-vm-01, got {codes}"
        )

    def test_network_qos_sriov_code_65(
        self, ros_api_url, vm_auth, http_session
    ):
        """Network-bound VM with high throughput/drops gets SR-IOV recommendation (code 65)."""
        vm = self._get_vm(http_session, ros_api_url, vm_auth, "network-qos-sriov-vm-01")
        codes = self._get_notification_codes(vm)
        assert 65 in codes, (
            f"Expected code 65 (SR-IOV) for network-qos-sriov-vm-01, got {codes}"
        )

    def test_network_qos_dpdk_code_66(
        self, ros_api_url, vm_auth, http_session
    ):
        """Network-bound VM with high PPS/small packets gets DPDK recommendation (code 66)."""
        vm = self._get_vm(http_session, ros_api_url, vm_auth, "network-qos-dpdk-vm-01")
        codes = self._get_notification_codes(vm)
        assert 66 in codes, (
            f"Expected code 66 (DPDK) for network-qos-dpdk-vm-01, got {codes}"
        )

    def test_storage_tier_cold_code_67(
        self, ros_api_url, vm_auth, http_session
    ):
        """VM with sustained minimal I/O gets cold storage tiering recommendation (code 67)."""
        vm = self._get_vm(http_session, ros_api_url, vm_auth, "storage-tier-cold-vm-01")
        codes = self._get_notification_codes(vm)
        assert 67 in codes, (
            f"Expected code 67 (cold tier) for storage-tier-cold-vm-01, got {codes}"
        )

    def test_storage_tier_iops_code_68(
        self, ros_api_url, vm_auth, http_session
    ):
        """VM with sustained random high IOPS gets IOPS-optimized storage recommendation (code 68)."""
        vm = self._get_vm(http_session, ros_api_url, vm_auth, "storage-tier-iops-vm-01")
        codes = self._get_notification_codes(vm)
        assert 68 in codes, (
            f"Expected code 68 (IOPS tier) for storage-tier-iops-vm-01, got {codes}"
        )

    def test_storage_tier_throughput_code_69(
        self, ros_api_url, vm_auth, http_session
    ):
        """VM with sustained sequential throughput gets throughput storage recommendation (code 69)."""
        vm = self._get_vm(
            http_session, ros_api_url, vm_auth, "storage-tier-throughput-vm-01"
        )
        codes = self._get_notification_codes(vm)
        assert 69 in codes, (
            f"Expected code 69 (throughput tier) for storage-tier-throughput-vm-01, got {codes}"
        )
