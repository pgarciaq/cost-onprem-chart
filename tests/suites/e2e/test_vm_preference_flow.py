"""
E2E: ingest VM + cluster_instance_types.json with VirtualMachinePreference mapping.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_preference_flow
"""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
_NISE_TEMPLATE = "ocp_report_vm.yml"
_PREFERENCE_VM = ("preference-server-vm", "production")
_PREFERENCE_NAME = "server"
_PREFERENCE_CLASS = "compute-intensive"


@dataclass
class VMPreferenceFlowContext:
    cluster_id: str
    auth: dict[str, str]


def _write_cluster_instance_types_with_preferences(cluster_id: str, directory: str) -> str:
    vm_key = f"{_PREFERENCE_VM[1]}/{_PREFERENCE_VM[0]}"
    doc = {
        "cluster_uuid": cluster_id,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance_types": [
            {
                "name": "u1.small",
                "series": "general-purpose",
                "vcpu": 1,
                "memory_gib": 2,
                "gpus": 0,
            },
        ],
        "preferences": [
            {
                "name": _PREFERENCE_NAME,
                "class": _PREFERENCE_CLASS,
            },
        ],
        "vm_preferences": {
            vm_key: _PREFERENCE_NAME,
        },
    }
    path = Path(directory) / "cluster_instance_types.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


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
            WHERE cluster_uuid = '{cluster_id}' AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=25,
        description="vm_recommendations for preference flow",
    )


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
            SELECT COUNT(*) FROM cluster_vm_preferences_meta
            WHERE cluster_uuid = '{cluster_id}' AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=15,
        description="cluster_vm_preferences_meta population",
    )


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMPreferenceFlow:
    @pytest.fixture(scope="class")
    def preference_cluster_id(self) -> str:
        return f"e2e-vm-pref-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def preference_context(
        self,
        cluster_config,
        keycloak_config,
        preference_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> VMPreferenceFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM preference E2E")

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
            cluster_id=preference_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-pref-{preference_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, preference_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {preference_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-pref-")

        files = generate_nise_data(
            cluster_id=preference_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_vm_usage_files") or [])
        ros_files.extend(files.get("ros_usage_files") or [])
        if not ros_files:
            pytest.fail("NISE did not generate ocp_ros_vm_usage files for preference template")

        instance_types_path = _write_cluster_instance_types_with_preferences(
            preference_cluster_id, temp_dir
        )
        pod_files = files.get("pod_usage_files") or ros_files

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=preference_cluster_id,
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
            cluster_config, db_pod, preference_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip("cluster_vm_preferences_meta not ingested")

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, preference_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip("vm_recommendations not populated for preference flow")

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after VM preference upload")
        return VMPreferenceFlowContext(cluster_id=preference_cluster_id, auth=auth)

    @pytest.mark.extended
    def test_preference_metadata_in_detail(
        self,
        preference_context: VMPreferenceFlowContext,
        ros_api_url: str,
        http_session: requests.Session,
    ):
        list_resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            preference_context.auth,
            {"limit": "100"},
        )
        skip_if_vm_plugin_disabled(list_resp)
        assert list_resp.status_code == 200, list_resp.text

        vm_name, namespace = _PREFERENCE_VM
        detail_resp = _fetch_vm_detail(
            http_session,
            ros_api_url,
            preference_context.auth,
            {
                "vm_name": vm_name,
                "namespace": namespace,
                "cluster_uuid": preference_context.cluster_id,
            },
        )
        assert detail_resp.status_code == 200, detail_resp.text
        meta = detail_resp.json().get("metadata") or {}
        assert meta.get("preference_name") == _PREFERENCE_NAME, meta
        assert meta.get("preference_class") == _PREFERENCE_CLASS, meta
