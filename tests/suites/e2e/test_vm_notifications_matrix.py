"""
E2E: assert VM notification codes 37–42 on dedicated NISE scenarios.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_notifications_matrix
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

NOTIF_DISK_GROWING_HYPERVISOR = 37
NOTIF_NO_GUEST_AGENT = 38
NOTIF_HIGH_IO = 39
NOTIF_DISK_FILLING_GUEST = 40
NOTIF_INSTANCE_TYPE_REC = 41
NOTIF_DISK_CRITICAL = 42

# VM names from tests/data/nise_templates/ocp_report_vm_notifications.yml
VM_NOTIF_EXPECTATIONS: dict[tuple[str, str], set[int]] = {
    ("disk-grow-hypervisor-01", _NOTIF_NAMESPACE): {NOTIF_DISK_GROWING_HYPERVISOR},
    ("no-guest-agent-01", _NOTIF_NAMESPACE): {NOTIF_NO_GUEST_AGENT},
    ("high-io-vm-01", _NOTIF_NAMESPACE): {NOTIF_HIGH_IO},
    ("disk-filling-guest-01", _NOTIF_NAMESPACE): {NOTIF_DISK_FILLING_GUEST},
    ("instance-type-rec-01", _NOTIF_NAMESPACE): {NOTIF_INSTANCE_TYPE_REC},
    ("disk-critical-01", _NOTIF_NAMESPACE): {NOTIF_DISK_CRITICAL},
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
class TestVMNotificationsMatrixFlow:
    """Ingest notification-scenario NISE data and assert codes 37–42 per VM."""

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
        http_session: requests.Session,
    ) -> VMNotificationsFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM notification E2E")

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

        auth = obtain_jwt_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.fail("Could not obtain JWT for VM notification E2E")
        return VMNotificationsFlowContext(cluster_id=vm_notif_cluster_id, auth=auth)

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
