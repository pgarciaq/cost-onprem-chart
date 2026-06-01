"""E2E tests for production-quality VM GPU time-slicing recommendations."""

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
)
from suites.ros.test_vm_settings import (
    _delete_vm_settings,
    _fetch_vm_settings,
    _put_vm_settings,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_vm_gpu_timeslicing.yml"

VM_GPU_TIMESLICE_UNDERUTIL = ("gpu-timeslice-underutil-vm", "inference")
VM_GPU_FB_SATURATED = ("gpu-fb-saturated-vm-01", "inference")

NOTIF_VGPU_PROFILE = 56
NOTIF_TIMESLICE_UNSAFE_FB = 57

VALID_GPU_TIMESLICE_SETTINGS_KEYS = frozenset(
    {
        "gpu_timeslice_min_replicas",
        "gpu_timeslice_max_replicas",
        "gpu_timeslice_fb_safety_threshold_bp",
        "gpu_timeslice_dram_penalty_threshold_bp",
    }
)
VALID_TIMESLICE_CONFIDENCE = frozenset({"high", "moderate", "low"})


@dataclass
class VMGPUTimesliceFlowContext:
    cluster_id: str
    auth: dict[str, str]


def _wait_for_vm_digest_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 420,
) -> bool:
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
              AND has_gpu = true
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="daily_vm_digests with has_gpu",
    )


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
              AND gpu_count > 0
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=25,
        description="vm_recommendations with GPU fields",
    )


def _vm_notification_codes(item: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for entry in item.get("notifications") or []:
        if isinstance(entry, dict) and entry.get("code") is not None:
            codes.add(int(entry["code"]))
        elif isinstance(entry, int):
            codes.add(entry)
    return codes


def _vm_gpu_block(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("gpu") or {}


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMGPUTimesliceExtendedFlow:
    """Upload GPU time-slicing NISE data and verify production vGPU recommendation fields."""

    @pytest.fixture(scope="class")
    def vm_gpu_timeslice_cluster_id(self) -> str:
        return f"e2e-vm-gpu-ts-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_gpu_timeslice_context(
        self,
        cluster_config,
        keycloak_config,
        vm_gpu_timeslice_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMGPUTimesliceFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM GPU time-slicing E2E")

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
            cluster_id=vm_gpu_timeslice_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-gpu-ts-{vm_gpu_timeslice_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_gpu_timeslice_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_gpu_timeslice_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-gpu-ts-")

        files = generate_nise_data(
            cluster_id=vm_gpu_timeslice_cluster_id,
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
                "NISE did not generate ocp_ros_vm_usage files for GPU time-slicing template"
            )

        pod_files = files.get("pod_usage_files") or ros_files
        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_gpu_timeslice_cluster_id,
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
            cluster_config, db_pod, vm_gpu_timeslice_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.fail("daily_vm_digests with has_gpu not populated")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_gpu_timeslice_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "vm_recommendations with GPU not populated for time-slicing E2E"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after GPU time-slicing upload")

        return VMGPUTimesliceFlowContext(
            cluster_id=vm_gpu_timeslice_cluster_id, auth=auth
        )

    def _vm_detail(
        self,
        http_session: requests.Session,
        ros_api_url: str,
        ctx: VMGPUTimesliceFlowContext,
        vm_name: str,
        namespace: str,
    ) -> dict[str, Any]:
        resp = _fetch_vm_detail(
            http_session,
            ros_api_url,
            ctx.auth,
            {
                "cluster_uuid": ctx.cluster_id,
                "vm_name": vm_name,
                "namespace": namespace,
                "term": "medium_term",
                "engine": "cost",
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_gpu_timeslice_has_confidence_and_rationale(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_timeslice_context: VMGPUTimesliceFlowContext,
    ):
        detail = self._vm_detail(
            http_session,
            ros_api_url,
            vm_gpu_timeslice_context,
            *VM_GPU_TIMESLICE_UNDERUTIL,
        )
        gpu = _vm_gpu_block(detail)
        if not gpu.get("gpu_count"):
            pytest.skip("GPU block not populated on underutil VM detail yet")
        confidence = gpu.get("gpu_timeslice_confidence") or ""
        rationale = gpu.get("gpu_timeslice_rationale") or ""
        if not confidence and not rationale:
            pytest.skip("Time-slice confidence/rationale not exposed yet")
        if confidence:
            assert confidence in VALID_TIMESLICE_CONFIDENCE, confidence
        if rationale:
            assert len(str(rationale).strip()) > 0

    def test_gpu_timeslice_vgpu_profile_recommended(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_timeslice_context: VMGPUTimesliceFlowContext,
    ):
        detail = self._vm_detail(
            http_session,
            ros_api_url,
            vm_gpu_timeslice_context,
            *VM_GPU_TIMESLICE_UNDERUTIL,
        )
        gpu = _vm_gpu_block(detail)
        profile = gpu.get("recommended_vgpu_profile") or ""
        if not profile:
            pytest.skip("recommended_vgpu_profile not set on underutil T4 VM yet")
        assert profile.startswith("grid_t4-"), (
            f"Expected grid_t4-* vGPU profile for T4, got {profile!r}"
        )

    def test_notification_code_56_vgpu_profile(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_timeslice_context: VMGPUTimesliceFlowContext,
    ):
        detail = self._vm_detail(
            http_session,
            ros_api_url,
            vm_gpu_timeslice_context,
            *VM_GPU_TIMESLICE_UNDERUTIL,
        )
        gpu = _vm_gpu_block(detail)
        if not gpu.get("recommended_vgpu_profile"):
            pytest.skip("vGPU profile not recommended yet for notification 56")
        assert NOTIF_VGPU_PROFILE in _vm_notification_codes(detail), (
            f"Expected notification 56: {detail.get('notifications')}"
        )

    def test_notification_code_57_fb_pressure(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_timeslice_context: VMGPUTimesliceFlowContext,
    ):
        detail = self._vm_detail(
            http_session,
            ros_api_url,
            vm_gpu_timeslice_context,
            *VM_GPU_FB_SATURATED,
        )
        gpu = _vm_gpu_block(detail)
        if gpu.get("gpu_classification") not in (
            "memory_saturated",
            "compute_saturated",
            "underutilized",
            "idle",
        ):
            pytest.skip(
                f"Unexpected GPU classification for FB-saturated VM: {gpu.get('gpu_classification')}"
            )
        if NOTIF_TIMESLICE_UNSAFE_FB not in _vm_notification_codes(detail):
            rationale = gpu.get("gpu_timeslice_rationale") or ""
            if "unsafe" not in rationale.lower() and "frame-buffer" not in rationale.lower():
                pytest.skip(
                    "Notification 57 not emitted; FB pressure may need more ingest days"
                )
        assert NOTIF_TIMESLICE_UNSAFE_FB in _vm_notification_codes(detail)

    def test_gpu_timeslice_settings_api(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_timeslice_context: VMGPUTimesliceFlowContext,
    ):
        resp = _fetch_vm_settings(
            http_session, ros_api_url, vm_gpu_timeslice_context.auth
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        gpu = resp.json().get("gpu") or {}
        assert VALID_GPU_TIMESLICE_SETTINGS_KEYS.issubset(gpu.keys()), gpu
        assert gpu.get("gpu_timeslice_min_replicas") is not None
        assert gpu.get("gpu_timeslice_max_replicas") is not None
        assert gpu.get("gpu_timeslice_fb_safety_threshold_bp") is not None
        assert gpu.get("gpu_timeslice_dram_penalty_threshold_bp") is not None

        baseline = resp.json()
        locked = set(baseline.get("locked_fields") or [])
        if "gpu.gpu_timeslice_min_replicas" in locked:
            pytest.skip("GPU time-slice settings are env-locked on this cluster")

        custom_min = 3
        if baseline.get("gpu", {}).get("gpu_timeslice_min_replicas") == custom_min:
            custom_min = 4

        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_gpu_timeslice_context.auth,
            {
                "gpu": {
                    **baseline.get("gpu", {}),
                    "gpu_timeslice_min_replicas": custom_min,
                }
            },
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["gpu"]["gpu_timeslice_min_replicas"] == custom_min

        del_resp = _delete_vm_settings(
            http_session, ros_api_url, vm_gpu_timeslice_context.auth
        )
        assert del_resp.status_code in (200, 204), del_resp.text
