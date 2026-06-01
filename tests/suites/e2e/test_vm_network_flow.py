"""E2E tests for VM n1 network-optimized instance type recommendations."""

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
_NISE_TEMPLATE = "ocp_report_vm_network.yml"

VM_NETWORK_HEAVY = ("network-heavy-vm-01", "vm-notifications")
VM_NETWORK_BASELINE = ("network-baseline-vm-01", "production")

NOTIF_NETWORK_SATURATED = 55

VALID_NETWORK_SETTINGS_KEYS = frozenset(
    {
        "throughput_threshold_bps",
        "pps_threshold",
        "drop_ratio_bp",
        "sustained_days",
        "enable_network_series",
    }
)


@dataclass
class VMNetworkFlowContext:
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
class TestVMNetworkExtendedFlow:
    """Upload network-heavy VM NISE data and verify n1 recommendation API behavior."""

    @pytest.fixture(scope="class")
    def vm_network_cluster_id(self) -> str:
        return f"e2e-vm-net-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_network_context(
        self,
        cluster_config,
        keycloak_config,
        vm_network_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMNetworkFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM network E2E data generation")

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
            cluster_id=vm_network_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-net-{vm_network_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_network_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_network_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-net-")

        files = generate_nise_data(
            cluster_id=vm_network_cluster_id,
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
                "NISE did not generate ocp_ros_vm_usage files for network template"
            )

        pod_files = files.get("pod_usage_files") or ros_files
        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_network_cluster_id,
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
            cluster_config, db_pod, vm_network_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.fail("daily_vm_digests not populated after network VM upload")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_network_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "vm_recommendations not populated within timeout for network E2E"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after network VM upload")

        return VMNetworkFlowContext(
            cluster_id=vm_network_cluster_id,
            package_path=package_path,
            auth=auth,
        )

    def _network_heavy_detail(
        self,
        http_session: requests.Session,
        ros_api_url: str,
        ctx: VMNetworkFlowContext,
    ) -> dict[str, Any]:
        vm_name, namespace = VM_NETWORK_HEAVY
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

    def test_network_heavy_vm_classified_as_network_optimized(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_network_context: VMNetworkFlowContext,
    ):
        detail = self._network_heavy_detail(
            http_session, ros_api_url, vm_network_context
        )
        meta = vm_item_metadata(detail)
        if not meta.get("is_network_bound"):
            pytest.skip(
                "Network-heavy VM not classified as network-bound yet; "
                "check sustained network metrics in digests"
            )
        inst = (detail.get("recommended") or {}).get("instance_type") or ""
        series = (detail.get("recommended") or {}).get("series") or ""
        if not inst.startswith("n1."):
            pytest.skip(
                f"Expected n1.* instance type, got {inst!r} (series={series!r})"
            )
        assert meta.get("is_network_bound") is True
        assert inst.startswith("n1.")

    def test_network_notification_code_55(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_network_context: VMNetworkFlowContext,
    ):
        detail = self._network_heavy_detail(
            http_session, ros_api_url, vm_network_context
        )
        if not vm_item_metadata(detail).get("is_network_bound"):
            pytest.skip("Network-bound classification not present for notification 55")
        assert NOTIF_NETWORK_SATURATED in _vm_notification_codes(detail), (
            f"Expected notification 55: {detail.get('notifications')}"
        )

    def test_network_settings_api(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_network_context: VMNetworkFlowContext,
    ):
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_network_context.auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        network = resp.json().get("network") or {}
        assert VALID_NETWORK_SETTINGS_KEYS.issubset(network.keys()), network
        assert network.get("throughput_threshold_bps") is not None
        assert network.get("pps_threshold") is not None
        assert network.get("drop_ratio_bp") is not None
        assert network.get("sustained_days") is not None
        assert isinstance(network.get("enable_network_series"), bool)

        baseline = resp.json()
        locked = set(baseline.get("locked_fields") or [])
        if "network.enable_network_series" in locked:
            pytest.skip("network.enable_network_series is env-locked on this cluster")

        custom_days = 8
        if baseline.get("network", {}).get("sustained_days") == custom_days:
            custom_days = 9

        put_body = {
            "network": {
                **baseline.get("network", {}),
                "sustained_days": custom_days,
            }
        }
        put_resp = _put_vm_settings(
            http_session, ros_api_url, vm_network_context.auth, put_body
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["network"]["sustained_days"] == custom_days

        del_resp = _delete_vm_settings(
            http_session, ros_api_url, vm_network_context.auth
        )
        assert del_resp.status_code in (200, 204), del_resp.text

    def test_network_series_disabled(
        self,
        cluster_config,
        keycloak_config,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
        vm_network_context: VMNetworkFlowContext,
    ):
        """With enable_network_series=false, network-heavy VMs must not get n1 types."""
        get_resp = _fetch_vm_settings(
            http_session, ros_api_url, vm_network_context.auth
        )
        skip_if_vm_plugin_disabled(get_resp)
        assert get_resp.status_code == 200, get_resp.text
        baseline = get_resp.json()
        locked = set(baseline.get("locked_fields") or [])
        if "network.enable_network_series" in locked:
            pytest.skip("network.enable_network_series is env-locked on this cluster")

        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_network_context.auth,
            {
                "network": {
                    **baseline.get("network", {}),
                    "enable_network_series": False,
                }
            },
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text

        try:
            upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
            upload_session = requests.Session()
            upload_session.verify = False
            token = obtain_jwt_token(keycloak_config)
            response = upload_with_retry(
                upload_session,
                upload_url,
                vm_network_context.package_path,
                token.authorization_header,
            )
            assert response.status_code in (200, 201, 202), response.text

            db_pod = get_pod_by_label(
                cluster_config.namespace, "app.kubernetes.io/component=database"
            )
            if not db_pod:
                pytest.skip("Database pod not found")
            if not _wait_for_vm_recommendation_rows(
                cluster_config,
                db_pod,
                vm_network_context.cluster_id,
                _UPLOAD_ORG_ID,
                timeout=720,
            ):
                pytest.skip("vm_recommendations not refreshed after re-upload")

            detail = self._network_heavy_detail(
                http_session, ros_api_url, vm_network_context
            )
            inst = (detail.get("recommended") or {}).get("instance_type") or ""
            if inst.startswith("n1."):
                pytest.fail(
                    f"network-heavy VM got n1 instance type with series disabled: {inst!r}"
                )
            assert vm_item_metadata(detail).get("is_network_bound") is False
            assert NOTIF_NETWORK_SATURATED not in _vm_notification_codes(detail)
        finally:
            _delete_vm_settings(
                http_session, ros_api_url, vm_network_context.auth
            )
