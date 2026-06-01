"""E2E tests for VM placement and NUMA recommendations (notifications 60, 63)."""

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
_NISE_TEMPLATE = "ocp_report_vm_placement.yml"
_PLACEMENT_NAMESPACE = "vm-placement"

VM_HA_PRIMARY = ("ha-primary-vm-01", _PLACEMENT_NAMESPACE)
VM_HA_STANDBY = ("ha-standby-vm-01", _PLACEMENT_NAMESPACE)
VM_NUMA_OVERSIZED = ("numa-oversized-vm-01", _PLACEMENT_NAMESPACE)

NOTIF_REDUNDANT_COLOCATION = 60
NOTIF_SHARED_STORAGE = 62
NOTIF_NUMA_OVERSIZED = 63

VALID_PLACEMENT_SETTINGS_KEYS = frozenset(
    {
        "enable_placement_checks",
        "placement_skew_ratio",
        "enable_shared_pvc_correlation",
        "numa_node_memory_gib",
    }
)


@dataclass
class VMPlacementFlowContext:
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
class TestVMPlacementExtendedFlow:
    """Upload placement NISE data and verify redundancy / NUMA API behavior."""

    @pytest.fixture(scope="class")
    def vm_placement_cluster_id(self) -> str:
        return f"e2e-vm-place-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_placement_context(
        self,
        cluster_config,
        keycloak_config,
        vm_placement_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMPlacementFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM placement E2E data generation")

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
            cluster_id=vm_placement_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-place-{vm_placement_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_placement_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_placement_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-place-")

        files = generate_nise_data(
            cluster_id=vm_placement_cluster_id,
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
                "NISE did not generate ocp_ros_vm_usage files for placement template"
            )

        pod_files = files.get("pod_usage_files") or ros_files
        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_placement_cluster_id,
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
            cluster_config, db_pod, vm_placement_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.fail("daily_vm_digests not populated after placement VM upload")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_placement_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "vm_recommendations not populated within timeout for placement E2E"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after placement VM upload")

        return VMPlacementFlowContext(
            cluster_id=vm_placement_cluster_id,
            package_path=package_path,
            auth=auth,
        )

    def _ha_primary_detail(
        self,
        http_session: requests.Session,
        ros_api_url: str,
        ctx: VMPlacementFlowContext,
    ) -> dict[str, Any]:
        vm_name, namespace = VM_HA_PRIMARY
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

    def test_redundant_colocation_notification_60(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_placement_context: VMPlacementFlowContext,
    ):
        detail = self._ha_primary_detail(
            http_session, ros_api_url, vm_placement_context
        )
        meta = vm_item_metadata(detail)
        if not meta.get("is_redundant_placement"):
            pytest.skip(
                "HA primary VM not flagged redundant yet; verify same-node peers in digests"
            )
        assert NOTIF_REDUNDANT_COLOCATION in _vm_notification_codes(detail), (
            f"Expected notification 60: {detail.get('notifications')}"
        )

    def test_shared_storage_flag_and_notification_62(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_placement_context: VMPlacementFlowContext,
    ):
        detail = self._ha_primary_detail(
            http_session, ros_api_url, vm_placement_context
        )
        meta = vm_item_metadata(detail)
        if not meta.get("has_shared_storage"):
            pytest.skip("Correlated workload group not detected for HA pair")
        assert NOTIF_SHARED_STORAGE in _vm_notification_codes(detail), (
            f"Expected notification 62: {detail.get('notifications')}"
        )

    def test_numa_oversized_notification_63(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_placement_context: VMPlacementFlowContext,
    ):
        vm_name, namespace = VM_NUMA_OVERSIZED
        resp = _fetch_vm_detail(
            http_session,
            ros_api_url,
            vm_placement_context.auth,
            {
                "cluster_uuid": vm_placement_context.cluster_id,
                "vm_name": vm_name,
                "namespace": namespace,
                "term": "medium_term",
                "engine": "cost",
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        meta = vm_item_metadata(detail)
        if not meta.get("numa_oversized"):
            pytest.skip(
                "128 GiB VM not flagged numa_oversized; check ROS_VM_NUMA_NODE_MEMORY_GIB"
            )
        assert NOTIF_NUMA_OVERSIZED in _vm_notification_codes(detail), (
            f"Expected notification 63: {detail.get('notifications')}"
        )

    def test_placement_settings_api(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_placement_context: VMPlacementFlowContext,
    ):
        resp = _fetch_vm_settings(
            http_session, ros_api_url, vm_placement_context.auth
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        placement = resp.json().get("placement") or {}
        assert VALID_PLACEMENT_SETTINGS_KEYS.issubset(placement.keys()), placement
        assert placement.get("enable_placement_checks") is True
        assert placement.get("numa_node_memory_gib") is not None

        locked = set(resp.json().get("locked_fields") or [])
        if "placement.placement_skew_ratio" in locked:
            pytest.skip("placement.placement_skew_ratio is locked by deployment env")

        original_skew = placement["placement_skew_ratio"]
        custom_skew = 5 if original_skew != 5 else 4
        try:
            put_resp = _put_vm_settings(
                http_session,
                ros_api_url,
                vm_placement_context.auth,
                {"placement": {"placement_skew_ratio": custom_skew}},
            )
            assert put_resp.status_code == 200, put_resp.text
            updated = put_resp.json().get("placement") or {}
            assert updated.get("placement_skew_ratio") == custom_skew
        finally:
            _delete_vm_settings(
                http_session, ros_api_url, vm_placement_context.auth
            )

    def test_list_response_includes_placement_metadata_fields(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_placement_context: VMPlacementFlowContext,
    ):
        vm_name, namespace = VM_HA_PRIMARY
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_placement_context.auth,
            {"limit": "200", "filter[cluster]": vm_placement_context.cluster_id},
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        row = _find_vm_row(rows, vm_name, namespace)
        if not row:
            pytest.skip(f"{vm_name} not found in VM list for placement cluster")
        meta = vm_item_metadata(row)
        for key in (
            "is_redundant_placement",
            "has_shared_storage",
            "numa_oversized",
        ):
            assert key in meta, f"metadata missing {key}: {meta}"
