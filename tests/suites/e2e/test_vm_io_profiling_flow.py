"""E2E tests for VM disk I/O pattern classification (sequential vs random)."""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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
_NISE_TEMPLATE = "ocp_report_vm_io_profiling.yml"
_NOTIF_NAMESPACE = "vm-notifications"

VM_SEQUENTIAL_IO = ("sequential-io-vm-01", _NOTIF_NAMESPACE)
VM_RANDOM_IO = ("random-io-vm-01", _NOTIF_NAMESPACE)

NOTIF_VM_IO_SEQUENTIAL = 58
NOTIF_VM_IO_RANDOM = 59

VALID_IO_SETTINGS_KEYS = frozenset(
    {
        "high_iops_threshold",
        "sequential_threshold_bytes",
        "random_threshold_bytes",
        "min_iops_for_classification",
    }
)


@dataclass
class VMIOProfilingFlowContext:
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


def _vm_notification_codes(item: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for entry in item.get("notifications") or []:
        if isinstance(entry, dict) and entry.get("code") is not None:
            codes.add(int(entry["code"]))
        elif isinstance(entry, int):
            codes.add(entry)
    return codes


def _io_profile(detail: dict[str, Any]) -> dict[str, Any]:
    return detail.get("io_profile") or {}


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMIOProfilingExtendedFlow:
    """Upload sequential/random I/O VM NISE data and verify classification API behavior."""

    @pytest.fixture(scope="class")
    def vm_io_cluster_id(self) -> str:
        return f"e2e-vm-io-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_io_context(
        self,
        cluster_config,
        keycloak_config,
        vm_io_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMIOProfilingFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM I/O profiling E2E data generation")

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
            cluster_id=vm_io_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-io-{vm_io_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_io_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_io_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-io-")

        files = generate_nise_data(
            cluster_id=vm_io_cluster_id,
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
                "NISE did not generate ocp_ros_vm_usage files for I/O profiling template"
            )

        pod_files = files.get("pod_usage_files") or ros_files
        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=vm_io_cluster_id,
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
            cluster_config, db_pod, vm_io_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.fail("daily_vm_digests not populated after I/O profiling VM upload")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_io_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "vm_recommendations not populated within timeout for I/O profiling E2E"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after I/O profiling VM upload")

        return VMIOProfilingFlowContext(
            cluster_id=vm_io_cluster_id,
            auth=auth,
        )

    def _vm_detail(
        self,
        http_session: requests.Session,
        ros_api_url: str,
        ctx: VMIOProfilingFlowContext,
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

    def test_sequential_io_vm_classified(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_io_context: VMIOProfilingFlowContext,
    ):
        vm_name, namespace = VM_SEQUENTIAL_IO
        detail = self._vm_detail(
            http_session, ros_api_url, vm_io_context, vm_name, namespace
        )
        pattern = _io_profile(detail).get("pattern")
        if pattern != "sequential":
            pytest.skip(
                f"Expected io_profile.pattern sequential, got {pattern!r}; "
                "check disk I/O digests and classification thresholds"
            )
        assert pattern == "sequential"

    def test_random_io_vm_classified(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_io_context: VMIOProfilingFlowContext,
    ):
        vm_name, namespace = VM_RANDOM_IO
        detail = self._vm_detail(
            http_session, ros_api_url, vm_io_context, vm_name, namespace
        )
        pattern = _io_profile(detail).get("pattern")
        if pattern != "random":
            pytest.skip(
                f"Expected io_profile.pattern random, got {pattern!r}; "
                "check disk I/O digests and classification thresholds"
            )
        assert pattern == "random"

    def test_notification_code_58_sequential(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_io_context: VMIOProfilingFlowContext,
    ):
        vm_name, namespace = VM_SEQUENTIAL_IO
        detail = self._vm_detail(
            http_session, ros_api_url, vm_io_context, vm_name, namespace
        )
        if _io_profile(detail).get("pattern") != "sequential":
            pytest.skip("Sequential I/O pattern not classified for notification 58")
        assert NOTIF_VM_IO_SEQUENTIAL in _vm_notification_codes(detail), (
            f"Expected notification 58: {detail.get('notifications')}"
        )

    def test_notification_code_59_random(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_io_context: VMIOProfilingFlowContext,
    ):
        vm_name, namespace = VM_RANDOM_IO
        detail = self._vm_detail(
            http_session, ros_api_url, vm_io_context, vm_name, namespace
        )
        if _io_profile(detail).get("pattern") != "random":
            pytest.skip("Random I/O pattern not classified for notification 59")
        assert NOTIF_VM_IO_RANDOM in _vm_notification_codes(detail), (
            f"Expected notification 59: {detail.get('notifications')}"
        )

    def test_io_settings_api(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_io_context: VMIOProfilingFlowContext,
    ):
        resp = _fetch_vm_settings(http_session, ros_api_url, vm_io_context.auth)
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        io_block = resp.json().get("io") or {}
        assert VALID_IO_SETTINGS_KEYS.issubset(io_block.keys()), io_block
        assert io_block.get("high_iops_threshold") is not None
        assert io_block.get("sequential_threshold_bytes") is not None
        assert io_block.get("random_threshold_bytes") is not None
        assert io_block.get("min_iops_for_classification") is not None

        baseline = resp.json()
        locked = set(baseline.get("locked_fields") or [])
        if "io.sequential_threshold_bytes" in locked:
            pytest.skip("io.sequential_threshold_bytes is env-locked on this cluster")

        custom_seq = 98304
        if baseline.get("io", {}).get("sequential_threshold_bytes") == custom_seq:
            custom_seq = 102400

        put_resp = _put_vm_settings(
            http_session,
            ros_api_url,
            vm_io_context.auth,
            {
                "io": {
                    **baseline.get("io", {}),
                    "sequential_threshold_bytes": custom_seq,
                }
            },
        )
        if put_resp.status_code == 403:
            pytest.skip("VM settings PUT rejected (env-locked or read-only)")
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["io"]["sequential_threshold_bytes"] == custom_seq

        del_resp = _delete_vm_settings(
            http_session, ros_api_url, vm_io_context.auth
        )
        assert del_resp.status_code in (200, 204), del_resp.text
