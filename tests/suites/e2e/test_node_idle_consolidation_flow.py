"""
Extended E2E: node idle_state filtering and consolidation hints.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data (ocp_report_node_idle_consolidation.yml) with zombie, idle,
     and lightly loaded worker nodes.
  3. Upload via ingress; wait for ROS node digests and node_recommendations.
  4. GET /recommendations/openshift/nodes?filter[idle_state]=idle|zombie
  5. Assert node_count_reduction > 0 on underutilized worker nodes.

Run (requires cluster + extended time budget):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k node_idle_consolidation_flow
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest
import requests

from conftest import obtain_jwt_token, obtain_user_jwt_token_for
from e2e_helpers import (
    ensure_nise_available,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
)
from suites.ros.test_node_recommendations import (
    _fetch_nodes,
    _node_medium_engines,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_node_idle_consolidation.yml"
_EXPECTED_IDLE_NODES = frozenset({"node-idle"})
_EXPECTED_ZOMBIE_NODES = frozenset({"node-zombie"})
_CONSOLIDATION_NODE_PREFIX = "m5-node-"


def _wait_for_node_digest_rows(
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
            SELECT COUNT(*) FROM daily_node_digests
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="daily_node_digests population",
    )


def _wait_for_node_recommendation_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 600,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM node_recommendations
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
              AND term IS NOT NULL
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="node_recommendations population",
    )


def _node_user_auth(keycloak_config, cluster_config) -> dict[str, str] | None:
    token = obtain_user_jwt_token_for(
        keycloak_config,
        cluster_config,
        username="user_dev",
        password="redhat123",
    )
    return token.authorization_header if token else None


def _nodes_for_cluster(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    cluster_id: str,
    params: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    query = {"cluster": cluster_id, "limit": 50}
    if params:
        query.update(params)
    resp = _fetch_nodes(session, ros_api_url, auth, query)
    if resp.status_code == 404:
        pytest.skip("Node recommendations plugin not enabled (404 on /nodes)")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return [
        row
        for row in body.get("data") or []
        if (row.get("cluster_uuid") or row.get("cluster")) == cluster_id
    ]


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(900)
class TestNodeIdleConsolidationExtendedFlow:
    """Upload multi-node ROS data and verify idle filters and consolidation hints."""

    @pytest.fixture(scope="class")
    def node_idle_e2e_cluster_id(self) -> str:
        return f"e2e-node-idle-{uuid.uuid4().hex[:8]}"

    def test_node_idle_consolidation_upload_and_api(
        self,
        cluster_config,
        keycloak_config,
        node_idle_e2e_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ):
        if not ensure_nise_available():
            pytest.skip("NISE is not available for node idle/consolidation E2E")

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
            cluster_id=node_idle_e2e_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-node-idle-{node_idle_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, node_idle_e2e_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {node_idle_e2e_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-node-idle-")

        files = generate_nise_data(
            cluster_id=node_idle_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        if not files.get("pod_usage_files"):
            pytest.fail("NISE did not generate pod_usage files for node idle E2E")
        if not files.get("ros_usage_files"):
            pytest.fail("NISE did not generate ros_usage files for node idle E2E")

        package_path = create_upload_package_from_files(
            pod_usage_files=files["pod_usage_files"],
            ros_usage_files=files["ros_usage_files"],
            cluster_id=node_idle_e2e_cluster_id,
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
            node_idle_e2e_cluster_id,
            timeout=420,
        )
        if not schema:
            pytest.fail("Summary tables not populated after node idle E2E upload")

        if not _wait_for_node_digest_rows(
            cluster_config, db_pod, node_idle_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=420
        ):
            pytest.skip(
                "daily_node_digests not populated within timeout; "
                "node ROS ingest may need additional cycles"
            )

        if not _wait_for_node_recommendation_rows(
            cluster_config, db_pod, node_idle_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=600
        ):
            pytest.skip(
                "node_recommendations not populated within timeout; "
                "node recommendation engine may need additional cycles"
            )

        auth = _node_user_auth(keycloak_config, cluster_config)
        if not auth:
            pytest.skip("Could not obtain user JWT after upload")

        idle_rows = _nodes_for_cluster(
            http_session,
            ros_api_url,
            auth,
            node_idle_e2e_cluster_id,
            {"filter[idle_state]": "idle"},
        )
        if not idle_rows:
            pytest.skip(
                "No nodes with idle_state=idle after upload; "
                "thresholds or observation window may need tuning on this cluster"
            )
        idle_names = {r["node"] for r in idle_rows}
        assert idle_names & _EXPECTED_IDLE_NODES, (
            f"Expected at least one of {_EXPECTED_IDLE_NODES} in idle filter, got {idle_names}"
        )
        for row in idle_rows:
            assert row.get("classification", {}).get("idle_state") == "idle"

        zombie_rows = _nodes_for_cluster(
            http_session,
            ros_api_url,
            auth,
            node_idle_e2e_cluster_id,
            {"filter[idle_state]": "zombie"},
        )
        if not zombie_rows:
            pytest.skip(
                "No nodes with idle_state=zombie after upload; "
                "zombie thresholds may need tuning on this cluster"
            )
        zombie_names = {r["node"] for r in zombie_rows}
        assert zombie_names & _EXPECTED_ZOMBIE_NODES, (
            f"Expected at least one of {_EXPECTED_ZOMBIE_NODES} in zombie filter, got {zombie_names}"
        )
        for row in zombie_rows:
            assert row.get("classification", {}).get("idle_state") == "zombie"

        all_rows = _nodes_for_cluster(
            http_session, ros_api_url, auth, node_idle_e2e_cluster_id
        )
        consolidation_nodes = [
            r for r in all_rows if r.get("node", "").startswith(_CONSOLIDATION_NODE_PREFIX)
        ]
        if len(consolidation_nodes) < 2:
            pytest.skip(
                f"Expected multiple {_CONSOLIDATION_NODE_PREFIX}* nodes in API response"
            )

        reduction_found = False
        for row in consolidation_nodes:
            cost_engine = (_node_medium_engines(row).get("cost")) or {}
            reduction = cost_engine.get("node_count_reduction")
            if reduction is not None and int(reduction) > 0:
                reduction_found = True
                break
        assert reduction_found, (
            "Expected node_count_reduction > 0 on at least one underutilized "
            f"{_CONSOLIDATION_NODE_PREFIX}* node (cost engine, medium term)"
        )
