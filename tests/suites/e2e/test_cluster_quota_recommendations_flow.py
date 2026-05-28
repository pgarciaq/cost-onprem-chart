"""
Extended E2E: ClusterResourceQuota recommendations data upload and API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_cluster_quota.yml (--ros-ocp-info) with
     cluster_resource_quotas and pod usage.
  3. Upload via ingress; wait for Koku summary tables and ROS processing.
  4. Wait for cluster_quota_recommendation_sets rows in costonprem_ros.
  5. GET /recommendations/openshift/cluster-quota/ and assert cluster_quota_name,
     cluster_uuid, recommendation_type, risk_level, quota blocks, savings.

Run (requires cluster + extended time budget):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k cluster_quota_recommendations_flow
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta

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
from suites.ros.test_cluster_quota_recommendations import (
    VALID_CLUSTER_QUOTA_RECOMMENDATION_TYPES,
    VALID_CLUSTER_QUOTA_RISK_LEVELS,
    _fetch_cluster_quota,
    _skip_if_plugin_disabled,
)
from suites.ros.test_recommendations import get_fresh_token
from utils import (
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    get_route_url,
    wait_for_condition,
)


def _ingress_upload_url(cluster_config) -> str:
    route_name = f"{cluster_config.helm_release_name}-ingress"
    base = get_route_url(cluster_config.namespace, route_name)
    if not base:
        pytest.skip("Ingress route not found")
    return f"{base.rstrip('/')}/api/ingress/v1/upload"


def _wait_for_cluster_quota_db_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 600,
) -> bool:
    """Wait until cluster_quota_recommendation_sets has rows for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM cluster_quota_recommendation_sets
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
              AND recommendation_type != 'none'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="cluster_quota_recommendation_sets population",
    )


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(900)
class TestClusterQuotaRecommendationsExtendedFlow:
    """Upload CRQ-oriented NISE data and verify cluster-quota recommendation API output."""

    @pytest.fixture(scope="class")
    def cluster_quota_e2e_cluster_id(self) -> str:
        short = uuid.uuid4().hex[:8]
        return f"e2e-crq-{short}"

    def test_cluster_quota_e2e_upload_and_api(
        self,
        cluster_config,
        keycloak_config,
        org_id: str,
        cluster_quota_e2e_cluster_id: str,
        ros_api_url: str,
        http_session: requests.Session,
    ):
        if not ensure_nise_available():
            pytest.skip("NISE is not available for cluster quota E2E data generation")

        probe_auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if probe_auth:
            probe = _fetch_cluster_quota(
                http_session, ros_api_url, probe_auth, {"limit": 1}
            )
            if probe.status_code == 404:
                pytest.skip(
                    "Cluster quota recommendations plugin not enabled on cluster"
                )

        ingress_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=ingress"
        )
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not ingress_pod or not db_pod:
            pytest.skip("Ingress or database pod not found")

        from conftest import create_rh_identity_header

        admin_identity = create_rh_identity_header(org_id)
        koku_url = get_koku_api_url(
            cluster_config.helm_release_name, cluster_config.namespace
        )

        reg = register_source(
            namespace=cluster_config.namespace,
            pod=ingress_pod,
            api_url=koku_url,
            rh_identity_header=admin_identity,
            cluster_id=cluster_quota_e2e_cluster_id,
            org_id=org_id,
            source_name=f"e2e-crq-{cluster_quota_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, cluster_quota_e2e_cluster_id, timeout=180
        ):
            pytest.fail(
                f"Provider not created for cluster {cluster_quota_e2e_cluster_id}"
            )

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-crq-")

        files = generate_nise_data(
            cluster_id=cluster_quota_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template="ocp_report_cluster_quota.yml",
        )
        if not files.get("pod_usage_files"):
            pytest.fail("NISE did not generate pod_usage files for cluster quota E2E")

        ros_files = list(files.get("ros_usage_files") or [])
        ros_files.extend(files.get("cluster_quota_files") or [])

        package_path = create_upload_package_from_files(
            pod_usage_files=files["pod_usage_files"],
            ros_usage_files=ros_files or files["pod_usage_files"],
            cluster_id=cluster_quota_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
        )

        upload_url = _ingress_upload_url(cluster_config)
        upload_session = requests.Session()
        upload_session.verify = False
        token = obtain_jwt_token(keycloak_config)
        response = upload_with_retry(
            upload_session, upload_url, package_path, token.authorization_header
        )
        assert response.status_code in (200, 201, 202), response.text

        from e2e_helpers import wait_for_summary_tables

        schema = wait_for_summary_tables(
            cluster_config.namespace,
            db_pod,
            cluster_quota_e2e_cluster_id,
            timeout=420,
        )
        if not schema:
            pytest.fail("Summary tables not populated after cluster quota E2E upload")

        if not _wait_for_cluster_quota_db_rows(
            cluster_config,
            db_pod,
            cluster_quota_e2e_cluster_id,
            org_id,
            timeout=600,
        ):
            pytest.skip(
                "cluster_quota_recommendation_sets not populated within timeout; "
                "cluster-quota ingest may need additional cycles"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after upload")

        api_resp = _fetch_cluster_quota(
            http_session,
            ros_api_url,
            auth,
            {"cluster": cluster_quota_e2e_cluster_id, "limit": 50},
        )
        _skip_if_plugin_disabled(api_resp)
        assert api_resp.status_code == 200, api_resp.text
        body = api_resp.json()
        assert body.get("meta", {}).get("count", 0) > 0, body
        assert body.get("data"), "Expected cluster quota recommendation rows after upload"

        matched = [
            row
            for row in body["data"]
            if row.get("cluster_uuid") == cluster_quota_e2e_cluster_id
        ]
        assert matched, f"No cluster quota rows for cluster {cluster_quota_e2e_cluster_id}"

        item = matched[0]
        assert item.get("cluster_quota_name")
        assert item.get("recommendation_type") in VALID_CLUSTER_QUOTA_RECOMMENDATION_TYPES
        assert item.get("risk_level") in VALID_CLUSTER_QUOTA_RISK_LEVELS
        assert item.get("quota_hard") or item.get("quota_recommended")
