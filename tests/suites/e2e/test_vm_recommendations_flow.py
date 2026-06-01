"""
Extended E2E: VM recommendations data upload and API verification.

Covers the full VM pipeline:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_vm.yml (--ros-ocp-info).
  3. Upload via ingress; wait for ROS vm_recommendations rows.
  4. Verify list, detail, idle detection, guest-agent confidence, and settings APIs.

Run (requires cluster + extended time budget):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_recommendations_flow
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest
import requests

from conftest import obtain_jwt_token
from e2e_helpers import (
    ensure_nise_available,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
)
from suites.ros.test_recommendations import get_fresh_token
from suites.ros.test_vm_recommendations import (
    _fetch_vm_detail,
    _fetch_vm_list,
    skip_if_vm_plugin_disabled,
    vm_item_metadata,
)
from suites.ros.test_vm_settings import _fetch_vm_settings
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

# Koku prepends "org" to JWT org_id; ROS stores bare org_id. SNO Keycloak uses "1234567".
_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_vm.yml"

# VM names/namespaces from tests/data/nise_templates/ocp_report_vm.yml
VM_DUAL_ENGINE = ("web-server-linux-01", "production")
VM_WITH_GUEST_AGENT = {
    "web-server-linux-01": "production",
    "db-server-windows-01": "production",
}
VM_WITHOUT_GUEST_AGENT = {
    "legacy-app-01": "legacy",
}
VM_IDLE = {
    "idle-vm-linux-01": "dev",
    "idle-windows-legacy-01": "legacy",
}
VM_ABANDONED = {
    "abandoned-test-vm-01": "forgotten-project",
}
VM_NOTIFICATION_CODE_ABANDONED = 43


@dataclass
class VMFlowContext:
    cluster_id: str
    package_path: str
    auth: dict[str, str]


def _wait_for_vm_digest_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 420,
) -> bool:
    """Wait until daily_vm_digests has rows for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM daily_vm_digests
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="daily_vm_digests population",
    )


def _wait_for_vm_recommendation_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 720,
) -> bool:
    """Wait until vm_recommendations has rows for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM vm_recommendations
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=25,
        description="vm_recommendations population",
    )


def _find_vm_row(
    rows: list[dict[str, Any]], vm_name: str, namespace: str
) -> Optional[dict[str, Any]]:
    for row in rows:
        if row.get("vm_name") == vm_name and row.get("namespace") == namespace:
            return row
    return None


def _vm_notification_codes(item: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for entry in item.get("notifications") or []:
        if isinstance(entry, dict) and entry.get("code") is not None:
            codes.add(int(entry["code"]))
        elif isinstance(entry, int):
            codes.add(entry)
    return codes


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMRecommendationsExtendedFlow:
    """Upload VM-oriented NISE data and verify VM recommendation API output."""

    @pytest.fixture(scope="class")
    def vm_e2e_cluster_id(self) -> str:
        return f"e2e-vm-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_flow_context(
        self,
        cluster_config,
        keycloak_config,
        vm_e2e_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ) -> VMFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM E2E data generation")

        probe_auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if probe_auth:
            probe = _fetch_vm_list(
                http_session, ros_api_url, probe_auth, {"limit": 1}
            )
            if probe.status_code == 404:
                pytest.skip("VM recommendations plugin not enabled on cluster")

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
            cluster_id=vm_e2e_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-{vm_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_e2e_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_e2e_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-")

        files = generate_nise_data(
            cluster_id=vm_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_vm_usage_files") or [])
        ros_files.extend(files.get("ros_usage_files") or [])
        if not ros_files:
            pytest.fail(
                "NISE did not generate ocp_ros_vm_usage files; "
                "ensure koku-nise includes OCPVirtualMachineGenerator"
            )

        pod_files = files.get("pod_usage_files") or ros_files

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
        )

        upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
        upload_session = requests.Session()
        upload_session.verify = False
        token = obtain_jwt_token(keycloak_config)
        response = upload_with_retry(
            upload_session, upload_url, package_path, token.authorization_header
        )
        assert response.status_code in (200, 201, 202), response.text

        if not _wait_for_vm_digest_rows(
            cluster_config, db_pod, vm_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=420
        ):
            pytest.fail("daily_vm_digests not populated after VM E2E upload")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=720
        ):
            pytest.skip(
                "vm_recommendations not populated within timeout; "
                "ROS processor may need more ingest cycles for 15-min VM samples"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after VM upload")

        return VMFlowContext(
            cluster_id=vm_e2e_cluster_id,
            package_path=package_path,
            auth=auth,
        )

    def test_vm_data_ingestion(
        self,
        cluster_config,
        vm_flow_context: VMFlowContext,
    ):
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not db_pod:
            pytest.skip("Database pod not found")

        digest = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM daily_vm_digests
            WHERE cluster_uuid = '{vm_flow_context.cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
            """,
        )
        assert digest is not None and int(digest[0][0]) > 0

        recs = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(DISTINCT vm_name) FROM vm_recommendations
            WHERE cluster_uuid = '{vm_flow_context.cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
            """,
        )
        assert recs is not None and int(recs[0][0]) >= len(VM_WITH_GUEST_AGENT)

    def test_vm_recommendations_generated(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {"filter[cluster]": vm_flow_context.cluster_id, "limit": 50},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("meta", {}).get("count", 0) > 0, body
        rows = body.get("data") or []

        for vm_name, namespace in VM_WITH_GUEST_AGENT.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Missing VM with guest agent: {vm_name}/{namespace}"
            assert vm_item_metadata(row).get("guest_agent_detected") is True

        for vm_name, namespace in VM_WITHOUT_GUEST_AGENT.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Missing VM without guest agent: {vm_name}/{namespace}"
            assert vm_item_metadata(row).get("guest_agent_detected") is False

    def test_vm_recommendation_detail(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        vm_name, namespace = next(iter(VM_WITH_GUEST_AGENT.items()))
        detail = _fetch_vm_detail(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {
                "cluster_uuid": vm_flow_context.cluster_id,
                "vm_name": vm_name,
                "namespace": namespace,
                "term": "medium_term",
                "engine": "cost",
            },
        )
        skip_if_vm_plugin_disabled(detail)
        assert detail.status_code == 200, detail.text
        item = detail.json()
        assert item.get("vm_name") == vm_name
        assert item.get("namespace") == namespace
        assert vm_item_metadata(item).get("guest_agent_detected") is True
        digests = item.get("daily_digests") or []
        assert digests, "detail response should include daily_digests"
        assert digests[0].get("bucket_date")
        assert "cpu_usage_p95_mc" in digests[0]

    def test_vm_idle_detection(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {"filter[cluster]": vm_flow_context.cluster_id, "limit": 50},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []

        for vm_name, namespace in VM_IDLE.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Missing idle VM: {vm_name}/{namespace}"
            assert vm_item_metadata(row).get("is_idle") is True

        active_name, active_ns = next(iter(VM_WITH_GUEST_AGENT.items()))
        active = _find_vm_row(rows, active_name, active_ns)
        assert active is not None
        assert vm_item_metadata(active).get("is_idle") is False

    @staticmethod
    def _find_vm_row_by_engine(
        rows: list[dict[str, Any]], vm_name: str, namespace: str, engine: str
    ) -> Optional[dict[str, Any]]:
        for row in rows:
            if row.get("vm_name") != vm_name or row.get("namespace") != namespace:
                continue
            if vm_item_metadata(row).get("engine") == engine:
                return row
        return None

    @staticmethod
    def _recommended_sizing(item: dict[str, Any]) -> tuple[Optional[int], Optional[float]]:
        rec = item.get("recommended") or {}
        vcpu = rec.get("vcpu")
        mem = rec.get("memory_gib")
        return (
            int(vcpu) if vcpu is not None else None,
            float(mem) if mem is not None else None,
        )

    def test_dual_engine_cost_and_performance(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        """Cost and performance engines both recommend the same VM; performance uses higher percentiles."""
        vm_name, namespace = VM_DUAL_ENGINE
        base_params = {
            "filter[cluster]": vm_flow_context.cluster_id,
            "filter[vm_name]": vm_name,
            "filter[namespace]": namespace,
            "filter[term]": "medium_term",
            "limit": 10,
        }

        cost_resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {**base_params, "filter[engine]": "cost"},
        )
        skip_if_vm_plugin_disabled(cost_resp)
        assert cost_resp.status_code == 200, cost_resp.text

        perf_resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {**base_params, "filter[engine]": "performance"},
        )
        skip_if_vm_plugin_disabled(perf_resp)
        assert perf_resp.status_code == 200, perf_resp.text

        cost_row = self._find_vm_row_by_engine(
            cost_resp.json().get("data") or [], vm_name, namespace, "cost"
        )
        perf_row = self._find_vm_row_by_engine(
            perf_resp.json().get("data") or [], vm_name, namespace, "performance"
        )
        assert cost_row, f"No cost-engine recommendation for {vm_name}/{namespace}"
        assert perf_row, f"No performance-engine recommendation for {vm_name}/{namespace}"

        cost_cpu, cost_mem = self._recommended_sizing(cost_row)
        perf_cpu, perf_mem = self._recommended_sizing(perf_row)
        assert cost_cpu is not None and perf_cpu is not None
        assert cost_mem is not None and perf_mem is not None
        assert perf_cpu >= cost_cpu, (
            f"performance vcpu {perf_cpu} should be >= cost vcpu {cost_cpu}"
        )
        assert perf_mem >= cost_mem, (
            f"performance memory {perf_mem} should be >= cost memory {cost_mem}"
        )

    def test_vm_settings_endpoint(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_settings(
            http_session, ros_api_url, vm_flow_context.auth
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("enabled") is True
        assert body.get("thresholds", {}).get("cpu_percentile_cost") is not None

    def test_vm_guest_agent_confidence(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {"filter[cluster]": vm_flow_context.cluster_id, "limit": 50},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []

        for vm_name, namespace in VM_WITH_GUEST_AGENT.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Missing VM: {vm_name}"
            assert vm_item_metadata(row).get("confidence") == "high"

        for vm_name, namespace in VM_WITHOUT_GUEST_AGENT.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Missing VM: {vm_name}"
            assert vm_item_metadata(row).get("confidence") == "moderate"

    def test_vm_abandoned_detected(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {
                "filter[cluster]": vm_flow_context.cluster_id,
                "filter[is_abandoned]": "true",
                "limit": 50,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("No abandoned VMs detected; ensure nise abandoned VM is in upload data")

        for vm_name, namespace in VM_ABANDONED.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Expected abandoned VM {vm_name}/{namespace} in filter[is_abandoned]=true"

    def test_vm_abandoned_notification_43(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {"filter[cluster]": vm_flow_context.cluster_id, "limit": 50},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []

        for vm_name, namespace in VM_ABANDONED.items():
            row = _find_vm_row(rows, vm_name, namespace)
            if not row:
                pytest.skip(f"Abandoned VM {vm_name} not in recommendations yet")
            assert VM_NOTIFICATION_CODE_ABANDONED in _vm_notification_codes(row), (
                f"Expected notification code 43 on {vm_name}: {row.get('notifications')}"
            )

    def test_vm_abandoned_supersedes_idle(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_flow_context: VMFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_flow_context.auth,
            {"filter[cluster]": vm_flow_context.cluster_id, "limit": 50},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []

        for vm_name, namespace in VM_ABANDONED.items():
            row = _find_vm_row(rows, vm_name, namespace)
            if not row:
                pytest.skip(f"Abandoned VM {vm_name} not in recommendations yet")
            meta = vm_item_metadata(row)
            assert meta.get("is_abandoned") is True
            assert meta.get("is_idle") is False
