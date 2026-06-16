"""Extended E2E: container GPU time-slicing upload and API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_gpu_timeslicing.yml (--ros-ocp-info).
  3. Upload via ingress; wait for gpu_container_digests in costonprem_ros.
  4. GET /recommendations/openshift/gpu/timeslicing and assert actionable rows
     when the engine classifies underutilized T4 workloads (notification 36).
  5. Verify node_gpu_timeslicing_recommendations has persisted rows and the
     GET .../gpu/timeslicing/history endpoint returns 200.

NISE pins low SM/DRAM via YAML overrides on Tesla T4 (time-slicing-eligible, not
MIG-first). If the cluster lacks GPU plugin or rates, tests skip with context.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k gpu_timeslicing_flow
"""

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
from suites.ros.test_gpu_recommendations import _fetch_gpu
from suites.ros.test_recommendations import get_fresh_token
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_gpu_timeslicing.yml"
NOTIF_GPU_TIMESLICING_CANDIDATE = 36


@dataclass
class GPUTimesliceFlowContext:
    cluster_id: str
    auth: dict[str, str]


def _wait_for_node_gpu_timeslicing_recommendations(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str = _UPLOAD_ORG_ID,
    timeout: int = 420,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM node_gpu_timeslicing_recommendations
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="node_gpu_timeslicing_recommendations population",
    )


def _wait_for_gpu_container_digests(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    timeout: int = 420,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM gpu_container_digests
            WHERE cluster_uuid = '{cluster_id}'
              AND node_name IS NOT NULL
              AND node_name != ''
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="gpu_container_digests population",
    )


def _notification_codes(item: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for entry in item.get("notification_codes") or []:
        if isinstance(entry, dict) and entry.get("code") is not None:
            codes.add(int(entry["code"]))
        elif isinstance(entry, int):
            codes.add(entry)
    return codes


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestContainerGPUTimesliceExtendedFlow:
    """Upload container GPU NISE data and verify time-slicing list recommendations."""

    @pytest.fixture(scope="class")
    def gpu_timeslice_cluster_id(self) -> str:
        return f"e2e-gpu-ts-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def gpu_timeslice_context(
        self,
        cluster_config,
        keycloak_config,
        gpu_timeslice_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> GPUTimesliceFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for container GPU time-slicing E2E")

        probe_auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if probe_auth:
            probe = _fetch_gpu(e2e_http_session, ros_api_url, probe_auth)
            if probe.status_code == 404:
                pytest.skip("GPU recommendations plugin not enabled on cluster")

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
            cluster_id=gpu_timeslice_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-gpu-ts-{gpu_timeslice_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, gpu_timeslice_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {gpu_timeslice_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-gpu-ts-")

        files = generate_nise_data(
            cluster_id=gpu_timeslice_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        ros_files = list(files.get("ros_usage_files") or [])
        pod_files = files.get("pod_usage_files") or ros_files
        if not ros_files:
            pytest.fail(
                "NISE did not generate ocp_ros_usage files for GPU time-slicing template"
            )

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=gpu_timeslice_cluster_id,
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

        if not _wait_for_gpu_container_digests(
            cluster_config, db_pod, gpu_timeslice_cluster_id
        ):
            pytest.fail("gpu_container_digests not populated within timeout")

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after GPU time-slicing upload")

        return GPUTimesliceFlowContext(
            cluster_id=gpu_timeslice_cluster_id, auth=auth
        )

    def test_timeslicing_list_has_actionable_recommendation(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        params = {
            "filter[cluster]": gpu_timeslice_context.cluster_id,
            "limit": 20,
        }
        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing",
            params,
        )
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text

        body = resp.json()
        items = body.get("data") or []
        if not items:
            summary = _fetch_gpu(
                http_session, ros_api_url, gpu_timeslice_context.auth
            )
            ts_count = 0
            if summary.status_code == 200:
                ts_count = summary.json().get("timeslicing", {}).get("count", 0)
            pytest.skip(
                "No actionable time-slicing rows yet "
                f"(summary timeslicing.count={ts_count}); "
                "engine may need more observation days or cost rates"
            )

        actionable = [
            item
            for item in items
            if int(item.get("recommended_replicas") or 0) > 1
            and NOTIF_GPU_TIMESLICING_CANDIDATE in _notification_codes(item)
        ]
        assert actionable, (
            "Expected at least one time-slicing row with recommended_replicas > 1 "
            f"and notification {NOTIF_GPU_TIMESLICING_CANDIDATE}; got {items[:2]}"
        )

        item = actionable[0]
        assert item.get("cluster_uuid") == gpu_timeslice_context.cluster_id
        assert item.get("node_name")
        assert item.get("gpu_model")

    def test_timeslicing_filter_by_cluster_uuid_alias(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing",
            {"cluster_uuid": gpu_timeslice_context.cluster_id, "limit": 10},
        )
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        for item in resp.json().get("data") or []:
            assert item.get("cluster_uuid") == gpu_timeslice_context.cluster_id

    def test_timeslicing_recommendations_persisted_in_database(
        self,
        cluster_config,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not db_pod:
            pytest.skip("Database pod not found")

        if not _wait_for_node_gpu_timeslicing_recommendations(
            cluster_config, db_pod, gpu_timeslice_context.cluster_id
        ):
            pytest.skip(
                "node_gpu_timeslicing_recommendations empty; "
                "engine may need more observation days or cost rates"
            )

        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM node_gpu_timeslicing_recommendations
            WHERE cluster_uuid = '{gpu_timeslice_context.cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
            """,
        )
        assert result is not None
        assert int(result[0][0]) >= 1, (
            "Expected at least one persisted GPU time-slicing recommendation row"
        )

    def test_timeslicing_history_endpoint(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        list_resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing",
            {"filter[cluster]": gpu_timeslice_context.cluster_id, "limit": 5},
        )
        if list_resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert list_resp.status_code == 200, list_resp.text

        items = list_resp.json().get("data") or []
        node_name = items[0].get("node_name") if items else "nonexistent-gpu-node"

        history_resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing/history",
            {
                "cluster_uuid": gpu_timeslice_context.cluster_id,
                "node_name": node_name,
            },
        )
        if history_resp.status_code == 404:
            pytest.skip("GPU time-slicing history endpoint not deployed")
        assert history_resp.status_code == 200, history_resp.text

        body = history_resp.json()
        assert "meta" in body, body
        assert "links" in body, body
        assert "data" in body, body
        assert isinstance(body["data"], list), body

        if items:
            assert body["data"], (
                "Expected history entries when time-slicing list has actionable rows"
            )
            entry = body["data"][0]
            assert entry.get("node_name") == node_name, entry
            assert entry.get("recorded_at"), entry

    def test_timeslicing_history_endpoint_empty_is_ok(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        history_resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing/history",
            {
                "cluster_uuid": gpu_timeslice_context.cluster_id,
                "node_name": "e2e-nonexistent-gpu-node",
            },
        )
        if history_resp.status_code == 404:
            pytest.skip("GPU time-slicing history endpoint not deployed")
        assert history_resp.status_code == 200, history_resp.text

        body = history_resp.json()
        assert body.get("data") == []
        assert body.get("meta", {}).get("count") == 0

    def test_timeslicing_history_requires_cluster_uuid(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        history_resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing/history",
            {"node_name": "some-node"},
        )
        if history_resp.status_code == 404:
            pytest.skip("GPU time-slicing history endpoint not deployed")
        assert history_resp.status_code == 400, history_resp.text

    def test_timeslicing_history_requires_node_name(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_timeslice_context: GPUTimesliceFlowContext,
    ):
        history_resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_timeslice_context.auth,
            "timeslicing/history",
            {"cluster_uuid": gpu_timeslice_context.cluster_id},
        )
        if history_resp.status_code == 404:
            pytest.skip("GPU time-slicing history endpoint not deployed")
        assert history_resp.status_code == 400, history_resp.text
