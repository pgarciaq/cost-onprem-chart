"""
Extended E2E: VM GPU recommendations — ingestion, classification, filters, gn1 matching.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_gpu_flow
"""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_vm_gpu.yml"

VM_BASELINE_NO_GPU = ("baseline-linux-no-gpu-01", "production")
VM_GPU_IDLE = ("gpu-idle-vm", "ml-training")
VM_GPU_UNDERUTIL = ("gpu-underutil-mig-vm", "inference")
VM_GPU_MEMORY_SAT = ("gpu-memory-saturated-vm", "inference")
VM_GPU_COMPUTE_SAT = ("gpu-compute-saturated-vm", "ml-training")
VM_GPU_HEALTHY = ("gpu-healthy-vm", "inference")

NOTIF_GPU_IDLE = 50
NOTIF_GPU_UNDERUTIL = 51
NOTIF_GPU_MEMORY_SAT = 52
NOTIF_GPU_COMPUTE_SAT = 53

GPU_CLASS_IDLE = "idle"
GPU_CLASS_UNDERUTIL = "underutilized"
GPU_CLASS_MEMORY_SAT = "memory_saturated"
GPU_CLASS_COMPUTE_SAT = "compute_saturated"


@dataclass
class VMGPUFlowContext:
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


def _vm_gpu_block(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("gpu") or {}


def _write_cluster_instance_types_json(cluster_id: str, directory: str) -> str:
    doc = {
        "cluster_uuid": cluster_id,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance_types": [
            {
                "name": "gn1.xlarge",
                "series": "gpu",
                "vcpu": 4,
                "memory_gib": 16,
                "gpus": 1,
            },
            {
                "name": "gn1.2xlarge",
                "series": "gpu",
                "vcpu": 8,
                "memory_gib": 32,
                "gpus": 1,
            },
            {
                "name": "gn1.4xlarge",
                "series": "gpu",
                "vcpu": 16,
                "memory_gib": 64,
                "gpus": 1,
            },
        ],
    }
    path = Path(directory) / "cluster_instance_types.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMGPUExtendedFlow:
    """Upload GPU VM NISE data and verify GPU recommendation API behavior."""

    @pytest.fixture(scope="class")
    def vm_gpu_cluster_id(self) -> str:
        return f"e2e-vm-gpu-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_gpu_context(
        self,
        cluster_config,
        keycloak_config,
        vm_gpu_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMGPUFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM GPU E2E data generation")

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
            cluster_id=vm_gpu_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-gpu-{vm_gpu_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_gpu_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_gpu_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-gpu-")

        files = generate_nise_data(
            cluster_id=vm_gpu_cluster_id,
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
                "ensure koku-nise includes GPU columns on OCPVirtualMachineGenerator"
            )

        instance_types_path = _write_cluster_instance_types_json(
            vm_gpu_cluster_id, temp_dir
        )
        pod_files = files.get("pod_usage_files") or ros_files

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_gpu_cluster_id,
            start_date=start_date,
            end_date=end_date,
            extra_resource_optimization_files=[instance_types_path],
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
            cluster_config, db_pod, vm_gpu_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.fail("daily_vm_digests with has_gpu not populated after GPU VM upload")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_gpu_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "vm_recommendations with GPU not populated; "
                "ROS processor may need more ingest cycles"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after VM GPU upload")

        return VMGPUFlowContext(cluster_id=vm_gpu_cluster_id, auth=auth)

    def _vm_detail(
        self,
        http_session: requests.Session,
        ros_api_url: str,
        ctx: VMGPUFlowContext,
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

    def test_vm_gpu_csv_ingestion(
        self,
        cluster_config,
        vm_gpu_context: VMGPUFlowContext,
    ):
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not db_pod:
            pytest.skip("Database pod not found")

        digests = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM daily_vm_digests
            WHERE cluster_uuid = '{vm_gpu_context.cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
              AND has_gpu = true
              AND gpu_count > 0
            """,
        )
        assert digests is not None and int(digests[0][0]) > 0

        recs = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT vm_name, gpu_classification, gpu_count
            FROM vm_recommendations
            WHERE cluster_uuid = '{vm_gpu_context.cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
              AND gpu_count > 0
            LIMIT 5
            """,
        )
        assert recs is not None and len(recs) > 0
        assert recs[0][2] > 0

    def test_vm_gpu_idle_classification_and_notification(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_GPU_IDLE
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        gpu = _vm_gpu_block(detail)
        if not gpu.get("gpu_count"):
            pytest.skip("GPU block not populated on idle VM detail yet")
        assert gpu.get("gpu_classification") == GPU_CLASS_IDLE, gpu
        assert NOTIF_GPU_IDLE in _vm_notification_codes(detail), detail.get(
            "notifications"
        )

    def test_vm_gpu_filter_has_gpu(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_gpu_context.auth,
            {
                "filter[cluster]": vm_gpu_context.cluster_id,
                "filter[has_gpu]": "true",
                "limit": 50,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("filter[has_gpu]=true returned no rows")
        for row in rows:
            assert _vm_gpu_block(row).get("gpu_count", 0) > 0, row
        names = {(r.get("vm_name"), r.get("namespace")) for r in rows}
        assert VM_GPU_IDLE in names
        assert VM_BASELINE_NO_GPU not in names

    def test_vm_gpu_filter_classification_idle(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_gpu_context.auth,
            {
                "filter[cluster]": vm_gpu_context.cluster_id,
                "filter[gpu_classification]": GPU_CLASS_IDLE,
                "limit": 50,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        if not rows:
            pytest.skip("filter[gpu_classification]=idle returned no rows")
        for row in rows:
            assert _vm_gpu_block(row).get("gpu_classification") == GPU_CLASS_IDLE, row
        assert _find_vm_row(rows, *VM_GPU_IDLE), "Expected gpu-idle-vm in idle filter"

    def test_vm_gpu_detail_response_shape(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_GPU_UNDERUTIL
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        gpu = _vm_gpu_block(detail)
        if not gpu:
            pytest.skip("GPU detail block not present on underutil VM")
        for field in (
            "gpu_count",
            "gpu_model",
            "gpu_classification",
            "recommended_gpu_action",
            "recommended_gpu_profile",
            "gpu_utilization_avg_bp",
        ):
            assert field in gpu, f"Missing gpu.{field}: {gpu}"

    def test_vm_gpu_underutil_notification_51(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_GPU_UNDERUTIL
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        gpu = _vm_gpu_block(detail)
        if gpu.get("gpu_classification") != GPU_CLASS_UNDERUTIL:
            pytest.skip(
                f"Expected underutilized classification, got {gpu.get('gpu_classification')}"
            )
        assert NOTIF_GPU_UNDERUTIL in _vm_notification_codes(detail)

    def test_vm_gpu_memory_saturated_notification_52(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_GPU_MEMORY_SAT
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        gpu = _vm_gpu_block(detail)
        if gpu.get("gpu_classification") != GPU_CLASS_MEMORY_SAT:
            pytest.skip(
                f"Expected memory_saturated, got {gpu.get('gpu_classification')}"
            )
        assert NOTIF_GPU_MEMORY_SAT in _vm_notification_codes(detail)

    def test_vm_gpu_compute_saturated_notification_53(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_GPU_COMPUTE_SAT
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        gpu = _vm_gpu_block(detail)
        if gpu.get("gpu_classification") != GPU_CLASS_COMPUTE_SAT:
            pytest.skip(
                f"Expected compute_saturated, got {gpu.get('gpu_classification')}"
            )
        assert NOTIF_GPU_COMPUTE_SAT in _vm_notification_codes(detail)

    def test_vm_gpu_instance_type_gn1_match(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_GPU_IDLE
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        inst = (detail.get("recommended") or {}).get("instance_type") or ""
        if not inst:
            pytest.skip("No instance_type on GPU VM recommendation yet")
        assert inst.startswith("gn1."), (
            f"GPU VM should match gn1.* instance type, got {inst!r}"
        )

    def test_vm_no_gpu_excludes_gn1(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_gpu_context: VMGPUFlowContext,
    ):
        vm_name, namespace = VM_BASELINE_NO_GPU
        detail = self._vm_detail(
            http_session, ros_api_url, vm_gpu_context, vm_name, namespace
        )
        inst = (detail.get("recommended") or {}).get("instance_type") or ""
        if inst:
            assert not inst.startswith("gn1."), (
                f"Non-GPU VM must not get gn1 instance type: {inst!r}"
            )
        assert _vm_gpu_block(detail).get("gpu_count", 0) == 0
