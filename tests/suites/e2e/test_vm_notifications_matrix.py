"""
E2E: VM notification codes 37–57 — direct assertions and cross-references.

Run direct matrix ingest (codes 37–42):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_notifications_matrix

Run full catalog (includes cross-reference skips):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k TestVMNotificationMatrix
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
    _fetch_vm_list,
    skip_if_vm_plugin_disabled,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_vm_notifications.yml"
_NOTIF_NAMESPACE = "vm-notifications"

# VM notification codes 37–57 (see ros-ocp-backend docs/architecture/notification-codes.md)
NOTIF_DISK_GROWING_HYPERVISOR = 37
NOTIF_NO_GUEST_AGENT = 38
NOTIF_HIGH_IO = 39
NOTIF_DISK_FILLING_GUEST = 40
NOTIF_INSTANCE_TYPE_REC = 41
NOTIF_DISK_CRITICAL = 42
NOTIF_ABANDONED = 43
NOTIF_GUEST_AGENT_INTERRUPTED = 44
NOTIF_INSUFFICIENT_DATA = 45
NOTIF_UNKNOWN_OS = 46
NOTIF_WINDOWS_UPDATE_SPIKE = 47
NOTIF_CRASH_LOOP = 48
NOTIF_DOWNSIZE_HELD = 49
NOTIF_GPU_IDLE = 50
NOTIF_GPU_UNDERUTIL = 51
NOTIF_GPU_MEMORY_SAT = 52
NOTIF_GPU_COMPUTE_SAT = 53
NOTIF_MULTI_GPU_IDLE = 54
NOTIF_NETWORK_SATURATED = 55
NOTIF_VGPU_PROFILE = 56
NOTIF_TIMESLICE_UNSAFE_FB = 57

VM_NOTIF_CODES_DIRECT = range(37, 43)
VM_NOTIF_CODES_ALL = range(37, 58)

# VM names from tests/data/nise_templates/ocp_report_vm_notifications.yml
VM_NOTIF_EXPECTATIONS: dict[tuple[str, str], set[int]] = {
    ("disk-grow-hypervisor-01", _NOTIF_NAMESPACE): {NOTIF_DISK_GROWING_HYPERVISOR},
    ("no-guest-agent-01", _NOTIF_NAMESPACE): {NOTIF_NO_GUEST_AGENT},
    ("high-io-vm-01", _NOTIF_NAMESPACE): {NOTIF_HIGH_IO},
    ("disk-filling-guest-01", _NOTIF_NAMESPACE): {NOTIF_DISK_FILLING_GUEST},
    ("instance-type-rec-01", _NOTIF_NAMESPACE): {NOTIF_INSTANCE_TYPE_REC},
    ("disk-critical-01", _NOTIF_NAMESPACE): {NOTIF_DISK_CRITICAL},
}

# Cross-references for codes not asserted in this file's NISE fixture
VM_NOTIF_E2E_CROSS_REF: dict[int, str] = {
    NOTIF_ABANDONED: "test_vm_recommendations_flow.py::TestVMRecommendationsExtendedFlow::test_vm_abandoned_notification_43",
    NOTIF_GUEST_AGENT_INTERRUPTED: "ros-ocp-backend unit: vm_recommender_test.go (guest agent interrupted); IQE: test_vm_notification_code_44_agent_interrupted",
    NOTIF_INSUFFICIENT_DATA: "ros-ocp-backend unit: vm_recommender_test.go (insufficient data); IQE: test_vm_notification_code_45_insufficient_data",
    NOTIF_UNKNOWN_OS: "test_vm_enhancements_flow.py::TestVMEnhancementsExtendedFlow::test_unknown_os_notification",
    NOTIF_WINDOWS_UPDATE_SPIKE: "test_vm_enhancements_flow.py::TestVMEnhancementsExtendedFlow::test_windows_update_spike_notification",
    NOTIF_CRASH_LOOP: "test_vm_enhancements_flow.py::TestVMEnhancementsExtendedFlow::test_crash_loop_notification_present",
    NOTIF_DOWNSIZE_HELD: "test_vm_enhancements_flow.py::TestVMEnhancementsExtendedFlow::test_downsize_held_notification",
    NOTIF_GPU_IDLE: "test_vm_gpu_flow.py::TestVMGPUExtendedFlow::test_vm_gpu_idle_classification_and_notification",
    NOTIF_GPU_UNDERUTIL: "test_vm_gpu_flow.py::TestVMGPUExtendedFlow::test_vm_gpu_underutil_notification_51",
    NOTIF_GPU_MEMORY_SAT: "test_vm_gpu_flow.py::TestVMGPUExtendedFlow::test_vm_gpu_memory_saturated_notification_52",
    NOTIF_GPU_COMPUTE_SAT: "test_vm_gpu_flow.py::TestVMGPUExtendedFlow::test_vm_gpu_compute_saturated_notification_53",
    NOTIF_MULTI_GPU_IDLE: "test_vm_mvp_promotions_flow.py::test_04_multi_gpu_partial_idle",
    NOTIF_NETWORK_SATURATED: "test_vm_network_flow.py::TestVMNetworkExtendedFlow::test_network_notification_code_55",
    NOTIF_VGPU_PROFILE: "test_vm_gpu_timeslicing_flow.py::TestVMGPUTimesliceExtendedFlow::test_notification_code_56_vgpu_profile",
    NOTIF_TIMESLICE_UNSAFE_FB: "test_vm_gpu_timeslicing_flow.py::TestVMGPUTimesliceExtendedFlow::test_notification_code_57_fb_pressure",
}


@dataclass
class VMNotificationsFlowContext:
    cluster_id: str
    auth: dict[str, str]


def _wait_for_vm_recommendation_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 720,
) -> bool:
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
class TestVMNotificationMatrix:
    """Comprehensive notification code coverage for VM recommendations.

    Codes 37–42: Tested directly via ocp_report_vm_notifications.yml ingest.
    Codes 43–49: Cross-referenced to dedicated extended E2E flows (or unit/IQE).
    Codes 50–57: GPU/network/time-slicing — cross-referenced to dedicated test files.
    """

    @pytest.fixture(scope="class")
    def vm_notif_cluster_id(self) -> str:
        return f"e2e-vm-notif-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_notif_context(
        self,
        cluster_config,
        keycloak_config,
        vm_notif_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMNotificationsFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM notification E2E")

        probe_auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if probe_auth:
            probe = _fetch_vm_list(
                e2e_http_session, ros_api_url, probe_auth, {"limit": 1}
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
            cluster_id=vm_notif_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-notif-{vm_notif_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_notif_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_notif_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=21)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-notif-")

        files = generate_nise_data(
            cluster_id=vm_notif_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_vm_usage_files") or [])
        ros_files.extend(files.get("ros_usage_files") or [])
        if not ros_files:
            pytest.fail("NISE did not generate ocp_ros_vm_usage files for notifications template")

        pod_files = files.get("pod_usage_files") or ros_files
        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_notif_cluster_id,
            start_date=start_date,
            end_date=end_date,
        )

        upload_with_retry(
            ingress_url=ingress_url,
            package_path=package_path,
            identity_header=admin_identity,
            cluster_id=vm_notif_cluster_id,
        )

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_notif_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.fail("vm_recommendations not populated after notification upload")

        auth = obtain_jwt_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.fail("Could not obtain JWT for VM notification E2E")
        return VMNotificationsFlowContext(cluster_id=vm_notif_cluster_id, auth=auth)

    def test_notification_catalog_covers_codes_37_through_57(self):
        """Registry must list every VM notification code (direct or cross-ref)."""
        covered = set(VM_NOTIF_CODES_DIRECT) | set(VM_NOTIF_E2E_CROSS_REF)
        assert covered == set(VM_NOTIF_CODES_ALL)

    def test_notification_codes_37_through_42(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_notif_context: VMNotificationsFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_notif_context.auth,
            {
                "filter[cluster]": vm_notif_context.cluster_id,
                "limit": 100,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []

        for (vm_name, namespace), expected_codes in VM_NOTIF_EXPECTATIONS.items():
            row = _find_vm_row(rows, vm_name, namespace)
            assert row, f"Missing VM {vm_name}/{namespace} in recommendations list"
            codes = _vm_notification_codes(row)
            missing = expected_codes - codes
            assert not missing, (
                f"VM {vm_name}/{namespace}: expected notification codes {sorted(expected_codes)}, "
                f"got {sorted(codes)}"
            )

    @pytest.mark.parametrize(
        "vm_name,expected_code",
        [
            ("instance-type-rec-01", NOTIF_INSTANCE_TYPE_REC),
            ("disk-critical-01", NOTIF_DISK_CRITICAL),
        ],
    )
    def test_notification_codes_41_and_42_isolated(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_notif_context: VMNotificationsFlowContext,
        vm_name: str,
        expected_code: int,
    ):
        """Codes 41 and 42 must appear alone on their dedicated NISE VMs (no co-occurring alerts)."""
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_notif_context.auth,
            {
                "filter[cluster]": vm_notif_context.cluster_id,
                "filter[vm_name]": vm_name,
                "filter[namespace]": _NOTIF_NAMESPACE,
                "limit": 10,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        row = _find_vm_row(rows, vm_name, _NOTIF_NAMESPACE)
        assert row, f"Missing dedicated VM {vm_name}/{_NOTIF_NAMESPACE}"
        codes = _vm_notification_codes(row)
        assert codes == {expected_code}, (
            f"VM {vm_name}/{_NOTIF_NAMESPACE}: expected only code {expected_code}, got {sorted(codes)}"
        )

    def test_notification_code_43_abandoned(self):
        """Abandoned VM (zero usage). See test_vm_recommendations_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_ABANDONED])

    def test_notification_code_44_guest_agent_interrupted(self):
        """Guest agent removed mid-window. See unit tests and IQE."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_GUEST_AGENT_INTERRUPTED])

    def test_notification_code_45_insufficient_data(self):
        """Low confidence / short history. See unit tests and IQE."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_INSUFFICIENT_DATA])

    def test_notification_code_46_unknown_os(self):
        """Empty guest_os uses Linux thresholds. See test_vm_enhancements_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_UNKNOWN_OS])

    def test_notification_code_47_windows_update_spike(self):
        """Windows P99≫P95 spread. See test_vm_enhancements_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_WINDOWS_UPDATE_SPIKE])

    def test_notification_code_48_crash_loop(self):
        """Elevated restart_count in window. See test_vm_enhancements_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_CRASH_LOOP])

    def test_notification_code_49_downsize_held(self):
        """Performance engine stability hold. See test_vm_enhancements_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_DOWNSIZE_HELD])

    def test_notification_code_50_gpu_idle(self):
        """Idle GPU classification. See test_vm_gpu_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_GPU_IDLE])

    def test_notification_code_51_gpu_underutil(self):
        """Underutilized GPU. See test_vm_gpu_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_GPU_UNDERUTIL])

    def test_notification_code_52_gpu_memory_saturated(self):
        """Memory-saturated GPU. See test_vm_gpu_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_GPU_MEMORY_SAT])

    def test_notification_code_53_gpu_compute_saturated(self):
        """Compute-saturated GPU. See test_vm_gpu_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_GPU_COMPUTE_SAT])

    def test_notification_code_54_multi_gpu_mixed_idle(self):
        """Some GPUs idle, others active. See test_vm_mvp_promotions_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_MULTI_GPU_IDLE])

    def test_notification_code_55_network_saturated(self):
        """Network-bound n1 recommendation. See test_vm_network_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_NETWORK_SATURATED])

    def test_notification_code_56_vgpu_profile(self):
        """vGPU profile recommended. See test_vm_gpu_timeslicing_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_VGPU_PROFILE])

    def test_notification_code_57_timeslice_unsafe_fb(self):
        """Time-slicing unsafe due to frame-buffer pressure. See test_vm_gpu_timeslicing_flow.py."""
        pytest.skip(VM_NOTIF_E2E_CROSS_REF[NOTIF_TIMESLICE_UNSAFE_FB])
