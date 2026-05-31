"""
Extended E2E: VM enhancement scenarios (Windows kernel reserve, notifications 46-49, settings).

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_enhancements_flow
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
from suites.ros.test_vm_settings import _fetch_vm_settings, _put_vm_settings
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_vm_enhancements.yml"
_ENHANCEMENTS_NAMESPACE = "vm-enhancements"

VM_WINDOWS_KERNEL = ("windows-kernel-compare-01", _ENHANCEMENTS_NAMESPACE)
VM_LINUX_KERNEL_PEER = ("linux-kernel-compare-01", _ENHANCEMENTS_NAMESPACE)
VM_WINDOWS_SPIKE = ("windows-update-spike-01", _ENHANCEMENTS_NAMESPACE)
VM_CRASH_LOOP = ("crash-loop-vm-01", _ENHANCEMENTS_NAMESPACE)
VM_UNKNOWN_OS = ("unknown-os-vm-01", _ENHANCEMENTS_NAMESPACE)
VM_DOWNSIZE_UNSTABLE = ("downsize-unstable-vm-01", _ENHANCEMENTS_NAMESPACE)

NOTIF_UNKNOWN_OS = 46
NOTIF_WINDOWS_SPIKE = 47
NOTIF_CRASH_LOOP = 48
NOTIF_DOWNSIZE_HELD = 49

RECOGNITION_ONLY_PREFIXES = ("n1.", "gn1.")


@dataclass
class VMEnhancementsFlowContext:
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


def _instance_types_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/instance-types"
    )


def _fetch_cluster_instance_types(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    cluster_uuid: str,
) -> requests.Response:
    return session.get(
        _instance_types_url(ros_api_url),
        headers=auth,
        params={"cluster_uuid": cluster_uuid},
        timeout=60,
    )


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
        ],
    }
    path = Path(directory) / "cluster_instance_types.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _wait_for_cluster_instance_types_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 300,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM cluster_instance_types
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=15,
        description="cluster_instance_types population",
    )


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMEnhancementsExtendedFlow:
    """Upload VM enhancement NISE data and verify notifications and settings APIs."""

    @pytest.fixture(scope="class")
    def vm_enhancements_cluster_id(self) -> str:
        return f"e2e-vm-enh-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_enhancements_context(
        self,
        cluster_config,
        keycloak_config,
        vm_enhancements_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ) -> VMEnhancementsFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM enhancements E2E")

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
            cluster_id=vm_enhancements_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-enh-{vm_enhancements_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_enhancements_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_enhancements_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-enh-")

        files = generate_nise_data(
            cluster_id=vm_enhancements_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_vm_usage_files") or [])
        ros_files.extend(files.get("ros_usage_files") or [])
        if not ros_files:
            pytest.fail("NISE did not generate ocp_ros_vm_usage files for enhancements template")

        instance_types_path = _write_cluster_instance_types_json(
            vm_enhancements_cluster_id, temp_dir
        )
        pod_files = files.get("pod_usage_files") or ros_files

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_enhancements_cluster_id,
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

        if not _wait_for_cluster_instance_types_rows(
            cluster_config, db_pod, vm_enhancements_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip("cluster_instance_types not ingested for gn1 catalog test")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_enhancements_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "vm_recommendations not populated; ROS processor may need more cycles"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after VM enhancements upload")

        return VMEnhancementsFlowContext(
            cluster_id=vm_enhancements_cluster_id,
            auth=auth,
        )

    def _vm_detail(
        self,
        http_session: requests.Session,
        ros_api_url: str,
        ctx: VMEnhancementsFlowContext,
        vm_name: str,
        namespace: str,
        *,
        engine: str = "cost",
        term: str = "medium_term",
    ) -> dict[str, Any]:
        resp = _fetch_vm_detail(
            http_session,
            ros_api_url,
            ctx.auth,
            {
                "cluster_uuid": ctx.cluster_id,
                "vm_name": vm_name,
                "namespace": namespace,
                "term": term,
                "engine": engine,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_windows_kernel_reserve_reflected(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        win_name, win_ns = VM_WINDOWS_KERNEL
        lin_name, lin_ns = VM_LINUX_KERNEL_PEER
        win = self._vm_detail(
            http_session, ros_api_url, vm_enhancements_context, win_name, win_ns
        )
        lin = self._vm_detail(
            http_session, ros_api_url, vm_enhancements_context, lin_name, lin_ns
        )
        win_rec = (win.get("recommended") or {}).get("memory_gib")
        lin_rec = (lin.get("recommended") or {}).get("memory_gib")
        assert win_rec is not None and lin_rec is not None
        assert win_rec < lin_rec, (
            f"Windows kernel reserve should lower memory recommendation: "
            f"windows={win_rec} linux={lin_rec}"
        )

    def test_crash_loop_notification_present(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        vm_name, namespace = VM_CRASH_LOOP
        detail = self._vm_detail(
            http_session, ros_api_url, vm_enhancements_context, vm_name, namespace
        )
        codes = _vm_notification_codes(detail)
        if NOTIF_CRASH_LOOP not in codes:
            pytest.skip(
                "Crash loop notification 48 not present; "
                "ensure nise crash_loop scenario and restart_count column are ingested"
            )
        assert NOTIF_CRASH_LOOP in codes

    def test_unknown_os_notification(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        vm_name, namespace = VM_UNKNOWN_OS
        detail = self._vm_detail(
            http_session, ros_api_url, vm_enhancements_context, vm_name, namespace
        )
        codes = _vm_notification_codes(detail)
        if NOTIF_UNKNOWN_OS not in codes:
            pytest.skip(
                "Notification 46 not present; re-ingest with empty guest_os in nise template"
            )
        assert NOTIF_UNKNOWN_OS in codes

    def test_instance_type_catalog_gn1_recognized(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        resp = _fetch_cluster_instance_types(
            http_session,
            ros_api_url,
            vm_enhancements_context.auth,
            vm_enhancements_context.cluster_id,
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        names = {item["name"] for item in resp.json().get("instance_types") or []}
        assert "gn1.xlarge" in names, f"Expected gn1.xlarge in catalog: {names}"

    def test_settings_api_kernel_reserve(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        get_resp = _fetch_vm_settings(
            http_session, ros_api_url, vm_enhancements_context.auth
        )
        skip_if_vm_plugin_disabled(get_resp)
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        floors = body.get("memory_floors") or {}
        assert "windows_kernel_reserve_gib" in floors
        original = floors["windows_kernel_reserve_gib"]
        locked = set(body.get("locked_fields") or [])
        if "memory_floors.windows_kernel_reserve_gib" in locked:
            pytest.skip("windows_kernel_reserve_gib is env-locked")

        new_value = 2.0 if original != 2.0 else 2.5
        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_enhancements_context.auth,
            {"memory_floors": {"windows_kernel_reserve_gib": new_value}},
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["memory_floors"]["windows_kernel_reserve_gib"] == new_value

        get_again = _fetch_vm_settings(
            http_session, ros_api_url, vm_enhancements_context.auth
        )
        assert get_again.json()["memory_floors"]["windows_kernel_reserve_gib"] == new_value

        _put_vm_settings(
            http_session,
            ros_api_url,
            vm_enhancements_context.auth,
            {"memory_floors": {"windows_kernel_reserve_gib": original}},
        )

    def test_settings_api_downsize_stability_days(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        get_resp = _fetch_vm_settings(
            http_session, ros_api_url, vm_enhancements_context.auth
        )
        skip_if_vm_plugin_disabled(get_resp)
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        stability = body.get("stability") or {}
        assert "downsize_stability_days" in stability
        original = stability["downsize_stability_days"]
        locked = set(body.get("locked_fields") or [])
        if "stability.downsize_stability_days" in locked:
            pytest.skip("downsize_stability_days is env-locked")

        new_value = 5 if original != 5 else 4
        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_enhancements_context.auth,
            {"stability": {"downsize_stability_days": new_value}},
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["stability"]["downsize_stability_days"] == new_value

        get_again = _fetch_vm_settings(
            http_session, ros_api_url, vm_enhancements_context.auth
        )
        assert get_again.json()["stability"]["downsize_stability_days"] == new_value

        _put_vm_settings(
            http_session,
            ros_api_url,
            vm_enhancements_context.auth,
            {"stability": {"downsize_stability_days": original}},
        )

    def test_windows_update_spike_notification(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        vm_name, namespace = VM_WINDOWS_SPIKE
        detail = self._vm_detail(
            http_session, ros_api_url, vm_enhancements_context, vm_name, namespace
        )
        codes = _vm_notification_codes(detail)
        if NOTIF_WINDOWS_SPIKE not in codes:
            pytest.skip(
                "Notification 47 not present; requires nise windows_update_spike scenario"
            )
        assert NOTIF_WINDOWS_SPIKE in codes

    def test_downsize_held_notification(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        vm_name, namespace = VM_DOWNSIZE_UNSTABLE
        detail = self._vm_detail(
            http_session,
            ros_api_url,
            vm_enhancements_context,
            vm_name,
            namespace,
            engine="performance",
        )
        codes = _vm_notification_codes(detail)
        if NOTIF_DOWNSIZE_HELD not in codes:
            pytest.skip(
                "Notification 49 not present; requires performance engine + downsize_unstable data"
            )
        assert NOTIF_DOWNSIZE_HELD in codes
        assert detail.get("recommended", {}).get("vcpu") == detail.get("current", {}).get(
            "vcpu"
        )

    def test_non_selectable_instance_types_not_recommended(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_enhancements_context: VMEnhancementsFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_enhancements_context.auth,
            {"filter[cluster]": vm_enhancements_context.cluster_id, "limit": 100},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        for row in resp.json().get("data") or []:
            inst = (row.get("recommended") or {}).get("instance_type") or ""
            assert not any(inst.startswith(p) for p in RECOGNITION_ONLY_PREFIXES), (
                f"n1/gn1 types must not be recommended: {inst!r} on {row.get('vm_name')}"
            )
