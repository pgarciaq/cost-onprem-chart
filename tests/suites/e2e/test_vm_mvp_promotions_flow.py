"""
E2E: VM MVP promotion scenarios (adaptive margin, instance type, GPU, history).

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_mvp_promotions_flow
"""

from __future__ import annotations

import re
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
_NISE_TEMPLATE = "ocp_report_vm_mvp_promotions.yml"
_NAMESPACE = "batch-jobs"

VM_VARIABLE_CPU = ("variable-cpu-vm", "batch-jobs")
VM_U1_MEDIUM = ("u1-medium-vm", "production")
VM_GPU_TIME_SLICE = ("gpu-time-slice-vm", "inference")
VM_MULTI_GPU = ("multi-gpu-mixed-vm", "ml-training")
VM_MIG_OPTIMAL = ("gpu-mig-optimal-vm", "inference")

NOTIF_MULTI_GPU_IDLE = 54


@dataclass
class VMMVPPromotionsContext:
    cluster_id: str
    auth: dict[str, str]
    ros_api_url: str


@pytest.fixture(scope="module")
def mvp_promotions_context(
    cluster_config,
    keycloak_config,
    ros_api_url: str,
    e2e_http_session: requests.Session,
) -> VMMVPPromotionsContext:
    ensure_nise_available()
    probe_auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
    if probe_auth:
        probe = _fetch_vm_list(
            e2e_http_session, ros_api_url, probe_auth, {"limit": 1}
        )
        skip_if_vm_plugin_disabled(probe)
    cluster_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=14)
    end_date = now - timedelta(days=1)
    with tempfile.TemporaryDirectory() as tmp:
        files = generate_nise_data(
            cluster_id=cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=tmp,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_vm_usage_files") or [])
        ros_files.extend(files.get("ros_vm_gpu_device_files") or [])
        ros_files.extend(files.get("ros_usage_files") or [])
        if not ros_files:
            pytest.fail("NISE did not generate VM ROS files for MVP promotions template")
        pod_files = files.get("pod_usage_files") or ros_files
        tarball = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=cluster_id,
            start_date=start_date,
            end_date=end_date,
        )
        register_source(cluster_config, cluster_id, org_id=_UPLOAD_ORG_ID)
        upload_with_retry(cluster_config, tarball, org_id=_UPLOAD_ORG_ID)
        wait_for_provider(cluster_config, cluster_id, org_id=_UPLOAD_ORG_ID, timeout=600)
    token = obtain_jwt_token(cluster_config)
    auth = create_rh_identity_header(org_id=_UPLOAD_ORG_ID)
    ros_api = cluster_config.ros_api_url.rstrip("/")
    return VMMVPPromotionsContext(cluster_id=cluster_id, auth=auth, ros_api_url=ros_api)


def _wait_for_vm_recs(cluster_config, db_pod: str, cluster_id: str, timeout: int = 720) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"SELECT COUNT(*) FROM vm_recommendations WHERE cluster_uuid = '{cluster_id}'",
        )
        return result and int(result[0][0]) > 0

    return wait_for_condition(check, timeout=timeout, interval=25, description="vm_recommendations")


def _find_vm(rows: list[dict[str, Any]], name: str, namespace: str) -> Optional[dict[str, Any]]:
    for row in rows:
        if row.get("vm_name") == name and row.get("namespace") == namespace:
            return row
    return None


def _vm_detail(ctx: VMMVPPromotionsContext, vm_name: str, namespace: str) -> dict[str, Any]:
    session = requests.Session()
    session.verify = False
    resp = _fetch_vm_detail(
        session,
        ctx.ros_api_url,
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


@pytest.mark.extended
def test_01_adaptive_margin_applied(mvp_promotions_context, cluster_config):
    """Variable CPU VM should get a non-default adaptive margin in detail."""
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    detail = _vm_detail(mvp_promotions_context, *VM_VARIABLE_CPU)
    margin = detail.get("cpu_margin_percent") or (detail.get("cpu") or {}).get("margin_percent")
    if margin is None:
        pytest.skip("cpu_margin_percent not exposed in VM detail API")
    assert float(margin) > 0


@pytest.mark.extended
def test_02_current_instance_type_populated(mvp_promotions_context, cluster_config):
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    detail = _vm_detail(mvp_promotions_context, *VM_U1_MEDIUM)
    assert detail.get("current_instance_type"), detail


@pytest.mark.extended
def test_03_time_slicing_recommendation(mvp_promotions_context, cluster_config):
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    detail = _vm_detail(mvp_promotions_context, *VM_GPU_TIME_SLICE)
    action = detail.get("recommended_gpu_action") or (detail.get("gpu") or {}).get("recommended_action")
    if action is None:
        pytest.skip("GPU recommendation not present for time-slice VM")
    assert action == "enable_time_slicing"


@pytest.mark.extended
def test_04_multi_gpu_partial_idle(mvp_promotions_context, cluster_config):
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    session = requests.Session()
    session.verify = False
    list_resp = _fetch_vm_list(
        session,
        mvp_promotions_context.ros_api_url,
        mvp_promotions_context.auth,
        {"cluster": mvp_promotions_context.cluster_id, "limit": 100},
    )
    skip_if_vm_plugin_disabled(list_resp)
    assert list_resp.status_code == 200, list_resp.text
    rows = list_resp.json().get("data") or []
    row = _find_vm(rows, *VM_MULTI_GPU)
    assert row is not None
    codes = {int(n.get("code", n)) for n in (row.get("notifications") or []) if n}
    assert NOTIF_MULTI_GPU_IDLE in codes


@pytest.mark.extended
def test_05_mig_optimal_profile(mvp_promotions_context, cluster_config):
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    detail = _vm_detail(mvp_promotions_context, *VM_MIG_OPTIMAL)
    profile = detail.get("recommended_gpu_profile") or (detail.get("gpu") or {}).get("recommended_profile")
    if not profile:
        pytest.skip("MIG profile recommendation not present")
    assert re.match(r"^\d+g\.\d+gb$", str(profile)) or profile in ("full_gpu",)


@pytest.mark.extended
def test_06_disk_projection_30_days(mvp_promotions_context, cluster_config):
    """Disk expansion logic uses 30-day projection window from settings."""
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    result = execute_db_query(
        cluster_config.namespace,
        db_pod,
        "costonprem_ros",
        "postgres",
        "SELECT value FROM vm_rec_config WHERE key = 'disk.projection_window_days'",
    )
    if not result:
        pytest.skip("vm_rec_config not available")
    assert int(result[0][0]) == 30


@pytest.mark.extended
def test_gpu_device_detail_response(mvp_promotions_context, cluster_config):
    """Detail includes gpu_devices array with per-UUID breakdown after device CSV ingest."""
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    detail = _vm_detail(mvp_promotions_context, *VM_MULTI_GPU)
    devices = detail.get("gpu_devices") or []
    if not devices:
        pytest.skip("gpu_devices not populated on multi-GPU VM detail yet")
    assert len(devices) >= 2, devices
    by_uuid = {d.get("gpu_uuid"): d for d in devices if d.get("gpu_uuid")}
    assert "GPU-aaa-111" in by_uuid and "GPU-bbb-222" in by_uuid
    for entry in by_uuid.values():
        assert entry.get("gpu_model"), entry
        assert entry.get("gpu_classification") or entry.get("classification"), entry


@pytest.mark.extended
def test_07_recommendation_history_api(mvp_promotions_context, cluster_config):
    db_pod = get_pod_by_label(cluster_config.namespace, "database")
    assert _wait_for_vm_recs(cluster_config, db_pod, mvp_promotions_context.cluster_id)
    url = (
        f"{mvp_promotions_context.ros_api_url}/cost-management/v1/"
        f"recommendations/openshift/vms/{VM_VARIABLE_CPU[0]}/history"
        f"?namespace={VM_VARIABLE_CPU[1]}&limit=10"
    )
    resp = requests.get(url, headers=mvp_promotions_context.auth, timeout=60)
    if resp.status_code == 404:
        pytest.skip("VM history endpoint not deployed")
    resp.raise_for_status()
    body = resp.json()
    assert isinstance(body.get("data"), list)
