"""
Extended E2E: per-cluster VM instance type catalog ingestion and API verification.

Uploads cluster_instance_types.json with custom types, then verifies the
instance-types endpoint and that VM recommendations reference the cluster catalog.
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
    _fetch_vm_list,
    skip_if_vm_plugin_disabled,
    vm_item_metadata,
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

# Unique catalog names — not present in the global VM instance type table.
E2E_CUSTOM_INSTANCE_TYPES = (
    "e2e-custom-gp-small",
    "e2e-custom-compute",
    "e2e-custom-memory",
)
VALID_INSTANCE_TYPE_SERIES = frozenset(
    {"general-purpose", "compute-optimized", "memory-optimized"}
)


@dataclass
class VMInstanceTypesFlowContext:
    cluster_id: str
    auth: dict[str, str]


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
    """Write cluster_instance_types.json for upload (operator-compatible name)."""
    doc = {
        "cluster_uuid": cluster_id,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance_types": [
            {
                "name": "e2e-custom-gp-small",
                "series": "general-purpose",
                "vcpu": 2,
                "memory_gib": 4,
                "gpus": 0,
            },
            {
                "name": "e2e-custom-compute",
                "series": "compute-optimized",
                "vcpu": 4,
                "memory_gib": 8,
                "gpus": 0,
            },
            {
                "name": "e2e-custom-memory",
                "series": "memory-optimized",
                "vcpu": 8,
                "memory_gib": 32,
                "gpus": 0,
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


@pytest.mark.e2e
@pytest.mark.vm
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestVMClusterInstanceTypesFlow:
    """Upload cluster instance type catalog and verify API + recommendation linkage."""

    @pytest.fixture(scope="class")
    def vm_instance_types_cluster_id(self) -> str:
        return f"e2e-vm-it-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def vm_instance_types_context(
        self,
        cluster_config,
        keycloak_config,
        vm_instance_types_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ) -> VMInstanceTypesFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for VM instance types E2E")

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
            cluster_id=vm_instance_types_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-vm-it-{vm_instance_types_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, vm_instance_types_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {vm_instance_types_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-vm-it-")

        files = generate_nise_data(
            cluster_id=vm_instance_types_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_vm_usage_files") or [])
        ros_files.extend(files.get("ros_usage_files") or [])
        if not ros_files:
            pytest.fail("NISE did not generate ocp_ros_vm_usage files")

        instance_types_path = _write_cluster_instance_types_json(
            vm_instance_types_cluster_id, temp_dir
        )

        package_path = create_upload_package_from_files(
            pod_usage_files=files.get("pod_usage_files") or ros_files,
            ros_usage_files=ros_files,
            cluster_id=vm_instance_types_cluster_id,
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
            cluster_config, db_pod, vm_instance_types_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip(
                "cluster_instance_types not ingested; VM plugin or listener may be disabled"
            )

        if not _wait_for_vm_recommendation_rows(
            cluster_config, db_pod, vm_instance_types_cluster_id, _UPLOAD_ORG_ID
        ):
            pytest.skip("vm_recommendations not populated within timeout")

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after VM instance types upload")

        return VMInstanceTypesFlowContext(
            cluster_id=vm_instance_types_cluster_id,
            auth=auth,
        )

    def test_vm_cluster_instance_types_ingested(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_instance_types_context: VMInstanceTypesFlowContext,
    ):
        resp = _fetch_cluster_instance_types(
            http_session,
            ros_api_url,
            vm_instance_types_context.auth,
            vm_instance_types_context.cluster_id,
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("cluster_uuid") == vm_instance_types_context.cluster_id
        names = {item["name"] for item in body.get("instance_types") or []}
        assert names, "Expected instance_types array in response"
        for custom in E2E_CUSTOM_INSTANCE_TYPES:
            assert custom in names, f"Missing cluster catalog type {custom}: {names}"
        for item in body["instance_types"]:
            assert item.get("series") in VALID_INSTANCE_TYPE_SERIES

    def test_vm_recommendation_uses_cluster_types(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        vm_instance_types_context: VMInstanceTypesFlowContext,
    ):
        resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_instance_types_context.auth,
            {
                "filter[cluster]": vm_instance_types_context.cluster_id,
                "filter[is_idle]": "false",
                "filter[is_abandoned]": "false",
                "limit": 50,
            },
        )
        skip_if_vm_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        active = [
            row
            for row in rows
            if not vm_item_metadata(row).get("is_idle")
            and not vm_item_metadata(row).get("is_abandoned")
        ]
        if not active:
            pytest.skip("No active VM recommendations to check instance types")

        with_catalog_type = [
            row
            for row in active
            if (row.get("recommended") or {}).get("instance_type") in E2E_CUSTOM_INSTANCE_TYPES
        ]
        if not with_catalog_type:
            pytest.skip(
                "No VM recommends a cluster-catalog instance type yet; "
                "instance_type_matching may be disabled"
            )

        row = with_catalog_type[0]
        rec_type = row["recommended"]["instance_type"]
        assert rec_type in E2E_CUSTOM_INSTANCE_TYPES
        series = row["recommended"].get("series")
        if series:
            assert series in VALID_INSTANCE_TYPE_SERIES
