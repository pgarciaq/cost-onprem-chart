"""Extended E2E: ROS GPU MIG upload and recommendation API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_gpu_mig_ros.yml (--ros-ocp-info).
  3. Upload via ingress; wait for gpu_container_digests in costonprem_ros.
  4. Wait until GET /recommendations/openshift/gpu reports mig.count > 0.
  5. GET /recommendations/openshift/gpu/mig and assert actionable MIG rows.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k gpu_mig_recommendations_flow
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
_NISE_TEMPLATE = "ocp_report_gpu_mig_ros.yml"
_EXPECTED_GPU_MODEL = "NVIDIA H100 80GB HBM3"


@dataclass
class GPUMigFlowContext:
    cluster_id: str
    auth: dict[str, str]


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


def _wait_for_gpu_mig_summary_count(
    http_session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    cluster_id: str,
    timeout: int = 720,
) -> bool:
    def check():
        resp = _fetch_gpu(http_session, ros_api_url, auth)
        if resp.status_code != 200:
            return False
        mig_count = resp.json().get("mig", {}).get("count", 0)
        if mig_count <= 0:
            return False
        filtered = _fetch_gpu(
            http_session,
            ros_api_url,
            auth,
            "mig",
            {"filter[cluster]": cluster_id, "limit": 1},
        )
        if filtered.status_code != 200:
            return False
        return len(filtered.json().get("data") or []) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=25,
        description="GPU MIG recommendations for cluster",
    )


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestContainerGPUMigRecommendationsExtendedFlow:
    """Upload ROS GPU MIG NISE data and verify MIG recommendation APIs."""

    @pytest.fixture(scope="class")
    def gpu_mig_cluster_id(self) -> str:
        return f"e2e-gpu-mig-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def gpu_mig_context(
        self,
        cluster_config,
        keycloak_config,
        gpu_mig_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        e2e_http_session: requests.Session,
    ) -> GPUMigFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for GPU MIG recommendation E2E")

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
            cluster_id=gpu_mig_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-gpu-mig-{gpu_mig_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, gpu_mig_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {gpu_mig_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-gpu-mig-ros-")

        files = generate_nise_data(
            cluster_id=gpu_mig_cluster_id,
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
                "NISE did not generate ocp_ros_usage files for GPU MIG ROS template"
            )

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=gpu_mig_cluster_id,
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
            cluster_config, db_pod, gpu_mig_cluster_id
        ):
            pytest.fail("gpu_container_digests not populated within timeout")

        auth = get_fresh_token(keycloak_config, cluster_config, e2e_http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after GPU MIG upload")

        if not _wait_for_gpu_mig_summary_count(
            e2e_http_session, ros_api_url, auth, gpu_mig_cluster_id
        ):
            summary = _fetch_gpu(e2e_http_session, ros_api_url, auth)
            mig_count = 0
            if summary.status_code == 200:
                mig_count = summary.json().get("mig", {}).get("count", 0)
            pytest.fail(
                "GPU MIG recommendations not available within timeout "
                f"(summary mig.count={mig_count})"
            )

        return GPUMigFlowContext(cluster_id=gpu_mig_cluster_id, auth=auth)

    def test_gpu_summary_mig_count_positive(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_mig_context: GPUMigFlowContext,
    ):
        resp = _fetch_gpu(http_session, ros_api_url, gpu_mig_context.auth)
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("mig", {}).get("count", 0) > 0, body
        assert body.get("timeslicing", {}).get("count", 0) >= 0

    def test_mig_list_has_actionable_recommendation(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_mig_context: GPUMigFlowContext,
    ):
        params = {
            "filter[cluster]": gpu_mig_context.cluster_id,
            "limit": 20,
        }
        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_mig_context.auth,
            "mig",
            params,
        )
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text

        body = resp.json()
        assert "meta" in body
        assert "data" in body
        items = body.get("data") or []
        assert items, (
            "Expected non-empty MIG list after upload; "
            f"summary mig.count={_fetch_gpu(http_session, ros_api_url, gpu_mig_context.auth).json().get('mig', {})}"
        )

        actionable = [
            item
            for item in items
            if (item.get("recommended_gpu_profile") or "").strip()
            and item.get("recommended_gpu_profile") != "full_gpu"
        ]
        assert actionable, (
            "Expected at least one MIG profile recommendation (not full_gpu); "
            f"got {items[:2]}"
        )

        item = actionable[0]
        assert item.get("cluster_uuid") == gpu_mig_context.cluster_id
        assert item.get("namespace")
        assert item.get("container")
        assert item.get("gpu_model")
        assert _EXPECTED_GPU_MODEL in (item.get("gpu_model") or "")
        assert item.get("recommended_gpu_profile")
        assert item.get("gpu_classification")
        assert item.get("confidence") is not None

    def test_mig_filter_by_cluster_uuid_alias(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        gpu_mig_context: GPUMigFlowContext,
    ):
        resp = _fetch_gpu(
            http_session,
            ros_api_url,
            gpu_mig_context.auth,
            "mig",
            {"cluster_uuid": gpu_mig_context.cluster_id, "limit": 10},
        )
        if resp.status_code == 404:
            pytest.skip("GPU recommendations plugin not enabled")
        assert resp.status_code == 200, resp.text
        for item in resp.json().get("data") or []:
            assert item.get("cluster_uuid") == gpu_mig_context.cluster_id
            profile = item.get("recommended_gpu_profile") or ""
            assert profile and profile != "full_gpu"
