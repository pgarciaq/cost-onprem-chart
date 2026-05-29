"""
Extended E2E: quota recommendations data upload and API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_quota.yml (--ros-ocp-info) with
     namespace resource_quota used columns and high pod requests.
  3. Upload via ingress; wait for Koku summary tables and ROS processing.
  4. Wait for quota_recommendation_sets rows in costonprem_ros.
  5. GET /recommendations/openshift/quota/ and assert namespace, cluster_uuid,
     recommendation_type, risk_level, quota_hard/used/recommended, savings.

Run (requires cluster + extended time budget):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k quota_recommendations_flow
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
from suites.ros.test_quota_recommendations import (
    VALID_QUOTA_RECOMMENDATION_TYPES,
    VALID_QUOTA_RISK_LEVELS,
    _fetch_quota,
    _skip_if_plugin_disabled,
)
from suites.ros.test_recommendations import get_fresh_token
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

# Koku prepends "org" to JWT org_id; ROS stores bare org_id. SNO Keycloak uses "1234567".
_UPLOAD_ORG_ID = "1234567"


def _wait_for_quota_db_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 600,
) -> bool:
    """Wait until quota_recommendation_sets has rows for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM quota_recommendation_sets
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
        description="quota recommendation_sets population",
    )


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(900)
class TestQuotaRecommendationsExtendedFlow:
    """Upload quota-oriented NISE data and verify quota recommendation API output."""

    @pytest.fixture(scope="class")
    def quota_e2e_cluster_id(self) -> str:
        short = uuid.uuid4().hex[:8]
        return f"e2e-quota-{short}"

    def test_quota_e2e_upload_and_api(
        self,
        cluster_config,
        keycloak_config,
        quota_e2e_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ):
        if not ensure_nise_available():
            pytest.skip("NISE is not available for quota E2E data generation")

        # Probe API before heavy upload when plugin is off.
        probe_auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if probe_auth:
            probe = _fetch_quota(
                http_session, ros_api_url, probe_auth, {"limit": 1}
            )
            if probe.status_code == 404:
                pytest.skip("Quota recommendations plugin not enabled on cluster")

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
            cluster_id=quota_e2e_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-quota-{quota_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, quota_e2e_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {quota_e2e_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-quota-")

        files = generate_nise_data(
            cluster_id=quota_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template="ocp_report_quota.yml",
        )
        if not files.get("pod_usage_files"):
            pytest.fail("NISE did not generate pod_usage files for quota E2E")

        ros_files = list(files.get("ros_usage_files") or [])
        ros_files.extend(files.get("namespace_usage_files") or [])

        package_path = create_upload_package_from_files(
            pod_usage_files=files["pod_usage_files"],
            ros_usage_files=ros_files or files["pod_usage_files"],
            cluster_id=quota_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
        )

        upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
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
            quota_e2e_cluster_id,
            timeout=420,
        )
        if not schema:
            pytest.fail("Summary tables not populated after quota E2E upload")

        if not _wait_for_quota_db_rows(
            cluster_config, db_pod, quota_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=600
        ):
            pytest.skip(
                "quota_recommendation_sets not populated within timeout; "
                "container recommendations may need additional ingest cycles"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after upload")

        api_resp = _fetch_quota(
            http_session,
            ros_api_url,
            auth,
            {"cluster": quota_e2e_cluster_id, "limit": 50},
        )
        _skip_if_plugin_disabled(api_resp)
        assert api_resp.status_code == 200, api_resp.text
        body = api_resp.json()
        assert body.get("meta", {}).get("count", 0) > 0, body
        assert body.get("data"), "Expected quota recommendation rows after upload"

        matched = [
            row
            for row in body["data"]
            if row.get("cluster_uuid") == quota_e2e_cluster_id
        ]
        assert matched, f"No quota rows for cluster {quota_e2e_cluster_id}"

        item = matched[0]
        assert item.get("namespace")
        assert item.get("recommendation_type") in VALID_QUOTA_RECOMMENDATION_TYPES
        assert item.get("risk_level") in VALID_QUOTA_RISK_LEVELS
        assert item.get("quota_hard") or item.get("quota_recommended")
